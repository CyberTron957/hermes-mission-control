"""Per-team persistent, shared browser pool — consumer-local, cross-platform.

Each team gets ONE long-lived Chrome (DevTools/CDP) bound to a stable, dedicated
``--user-data-dir`` under ``data/teams/<team>/.browser-profile``. Every agent in
that team is pointed at the same ``browser.cdp_url`` (written into its
config.yaml), so they share one browser — same cookies, logins, and storage.
The profile is a fixed path on disk, so logins survive a server restart, and one
profile per team keeps teams isolated.

Two-mode design (Windows / macOS / Linux, no system packages):

  * AGENT WORK runs the browser **headless** (``--headless=new``). It never puts
    a window on the user's desktop, and — because there is no window to throttle —
    CDP screenshots (how agents perceive the page) always render. This is also the
    exact mode a future headless remote server will use.

  * HUMAN LOGIN (takeover) relaunches the SAME profile as a **real, visible Chrome
    window** on the user's own screen, opened on the page the agent was blocked on.
    The human signs in (a real browser + real display + real human is the most
    sign-in-friendly thing possible for Google/Microsoft/social), replies "done",
    and we relaunch headless. Cookies persist across the relaunch, so the agent
    resumes already authenticated.

We prefer the user's **installed Google Chrome / Edge** over Playwright's
"Chrome for Testing" build (real Chrome branding passes provider sign-in checks
far more reliably), while still using a dedicated profile so we never touch the
user's personal browsing.

This connects via Hermes' CDP-override path (``browser.cdp_url``), which takes
precedence over both the cloud provider and the local launcher.
"""

import glob
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional

from teams_server.config import WORKSPACE_ROOT

log = logging.getLogger("teams.browser")

# Base port for per-team CDP endpoints; each team gets the next free port up.
_BASE_CDP_PORT = 9333


def _find_playwright_chromium() -> Optional[str]:
    """Locate a Chromium executable from the Playwright browser cache (fallback
    when no real Chrome/Edge is installed). Prefers the full "Chrome for Testing"
    build; falls back to the headless-shell."""
    roots = []
    pbp = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if pbp:
        roots.append(Path(pbp))
    roots += [
        Path.home() / "Library" / "Caches" / "ms-playwright",        # macOS
        Path.home() / ".cache" / "ms-playwright",                     # Linux
        Path.home() / "AppData" / "Local" / "ms-playwright",          # Windows
    ]
    patterns = [
        "chromium-*/chrome-mac*/*.app/Contents/MacOS/*",              # mac full chromium
        "chromium-*/chrome-linux*/chrome",                            # linux full chromium
        "chromium-*/chrome-win*/chrome.exe",                          # windows full chromium
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    ]
    for root in roots:
        for pat in patterns:
            for hit in sorted(glob.glob(str(root / pat)), reverse=True):
                if os.path.isfile(hit) and os.access(hit, os.X_OK):
                    return hit
    return None


def _find_browser() -> Optional[str]:
    """Find a browser to drive, preferring the user's INSTALLED Chrome/Edge
    (best provider sign-in success) over Playwright's bundled Chromium.
    OS-aware: Chrome -> Edge -> Playwright Chromium."""
    system = platform.system()
    cands = []
    if system == "Darwin":
        cands += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    elif system == "Windows":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pfx86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        lad = os.environ.get("LOCALAPPDATA", "")
        for base in (pf, pfx86, lad):
            if base:
                cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        for base in (pf, pfx86, lad):
            if base:
                cands.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    else:  # Linux / other
        for name in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "microsoft-edge", "microsoft-edge-stable"):
            w = shutil.which(name)
            if w:
                cands.append(w)
        cands += ["/opt/google/chrome/chrome", "/usr/bin/google-chrome",
                  "/snap/bin/chromium"]

    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return _find_playwright_chromium()


class TeamBrowserManager:
    """Launches and tracks one persistent Chrome per team: headless for agent
    work, relaunched as a real visible window for a human login."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # team_id -> {"proc": Popen, "port": int, "profile": str, "headful": bool}
        self._browsers: Dict[str, dict] = {}
        self._ports: Dict[str, int] = {}
        self._browser_path = _find_browser()
        self._ua = self._compute_clean_ua() if self._browser_path else None
        if self._browser_path:
            log.info("Team browser pool using: %s", self._browser_path)
        else:
            log.warning(
                "No browser found for team browser pool (install Google Chrome, "
                "or run: npx playwright install chromium). Team browsers disabled."
            )

    # -- browser identity ---------------------------------------------------
    def _compute_clean_ua(self) -> Optional[str]:
        """A desktop User-Agent WITHOUT the 'HeadlessChrome' token, so headless
        agent browsing on an authenticated session isn't trivially flagged. The
        human LOGIN runs headful with the real UA, so sign-in is unaffected.
        Best-effort: returns None if the version can't be determined."""
        ver = None
        try:
            out = subprocess.run([self._browser_path, "--version"],
                                 capture_output=True, text=True, timeout=5).stdout or ""
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
            if m:
                ver = m.group(1)
        except Exception:
            pass
        if not ver:
            return None
        system = platform.system()
        if system == "Darwin":
            plat = "Macintosh; Intel Mac OS X 10_15_7"
        elif system == "Windows":
            plat = "Windows NT 10.0; Win64; x64"
        else:
            plat = "X11; Linux x86_64"
        return (f"Mozilla/5.0 ({plat}) AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{ver} Safari/537.36")

    # -- port helpers -------------------------------------------------------
    @staticmethod
    def _port_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) != 0

    def _assign_port(self, team_id: str) -> int:
        if team_id in self._ports:
            return self._ports[team_id]
        port = _BASE_CDP_PORT
        used = set(self._ports.values())
        while port in used or not self._port_free(port):
            port += 1
        self._ports[team_id] = port
        return port

    def _healthy(self, port: int) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=2
            ) as r:
                return r.status == 200
        except Exception:
            return False

    def _healthy_retry(self, port: int, attempts: int = 3, delay: float = 0.2) -> bool:
        """A few quick CDP probes before concluding a browser is dead, so a single
        slow response (heavy page / swap) doesn't trigger a kill. Returns True as
        soon as any probe succeeds. Kept short (delay is tiny) since this runs
        under the pool lock."""
        for i in range(attempts):
            if self._healthy(port):
                return True
            if i < attempts - 1:
                time.sleep(delay)
        return False

    def _capture_active_url(self, port: int) -> Optional[str]:
        """The URL of the team browser's current real page (so a takeover opens
        the human directly on the page the agent was blocked on). Skips blank /
        internal pages."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as r:
                data = json.load(r)
        except Exception:
            return None
        for p in data:
            if p.get("type") != "page":
                continue
            u = p.get("url", "") or ""
            if u and not u.startswith(("about:", "chrome:", "devtools:", "chrome-extension:")):
                return u
        return None

    # -- launch -------------------------------------------------------------
    def _launch(self, team_id: str, headful: bool,
                start_url: Optional[str] = None) -> Optional[str]:
        """Launch the team browser in the requested mode on its stable port and
        dedicated profile, wait until the CDP endpoint is healthy, and record it.
        Returns the CDP url, or None on failure."""
        if not self._browser_path:
            return None
        port = self._assign_port(team_id)
        profile = WORKSPACE_ROOT / team_id / ".browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        # A stale singleton lock from an unclean exit (or the previous mode) blocks
        # relaunch on the same profile; clear it.
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (profile / lock_name).unlink()
            except OSError:
                pass

        args = [
            self._browser_path,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        if headful:
            # Real, visible window on the user's own desktop for the human login.
            # Inherit the environment as-is so it appears on their native display
            # (X11 or Wayland) — we WANT them to see it.
            args += ["--new-window", start_url or "about:blank"]
        else:
            # Invisible everywhere; always paints so agent screenshots work.
            args.append("--headless=new")
            if self._ua:
                args.append(f"--user-agent={self._ua}")
            args.append("about:blank")

        try:
            # start_new_session puts Chrome + its helpers in their own process
            # group so we can reap the whole tree (POSIX; harmless on Windows).
            proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            log.error("[%s] Failed to launch team browser (%s): %s",
                      team_id, "headful" if headful else "headless", e)
            return None

        for _ in range(50):  # ≤10s
            if proc.poll() is not None:
                log.error("[%s] Team browser (%s) exited during startup",
                          team_id, "headful" if headful else "headless")
                return None
            if self._healthy(port):
                with self._lock:
                    self._browsers[team_id] = {
                        "proc": proc, "port": port,
                        "profile": str(profile), "headful": headful,
                    }
                    self._ports[team_id] = port
                log.info("[%s] %s browser ready on port %d (%s)", team_id,
                         "Headful (login)" if headful else "Headless",
                         port, self._browser_path)
                return f"http://127.0.0.1:{port}"
            time.sleep(0.2)

        log.error("[%s] Team browser (%s) did not become healthy on port %d",
                  team_id, "headful" if headful else "headless", port)
        self._terminate(proc)
        return None

    def ensure_team_browser(self, team_id: str) -> Optional[str]:
        """Return the team's CDP URL, launching/healing a HEADLESS browser as
        needed. Idempotent and cheap on the happy path; if a takeover put the
        browser in headful mode, that healthy browser is returned as-is. Returns
        None when no browser binary is available so callers fall back gracefully.
        """
        if not self._browser_path:
            return None
        with self._lock:
            info = self._browsers.get(team_id)
            if info and info["proc"].poll() is None:
                # The process is alive — only restart if the CDP endpoint is
                # genuinely dead, not on a single slow probe. A transient timeout
                # (heavy page / swap) while ANOTHER agent is mid-session must NOT
                # hard-kill the shared browser and race its cookie flush. Retry a
                # couple of times with a short delay (total well under ~1s, safe
                # under the lock) before declaring it dead.
                if self._healthy_retry(info["port"]):
                    return f"http://127.0.0.1:{info['port']}"
                # Genuinely unhealthy: close gracefully so cookies flush, falling
                # back to a hard terminate only if the graceful close fails.
                self._quit_browser(info, team_id)
            return self._launch(team_id, headful=False)

    # -- human takeover (mode switch) --------------------------------------
    def begin_takeover(self, team_id: str) -> bool:
        """Put the team browser on the human's screen for a login: relaunch the
        same profile as a real visible window, opened on the page the agent was
        blocked on. Returns True if a window is now on screen, False if it
        couldn't be shown (e.g. a headless host with no display)."""
        self.ensure_team_browser(team_id)
        with self._lock:
            info = self._browsers.get(team_id)
            port = info["port"] if info else None
            already_headful = bool(info and info.get("headful")
                                   and info["proc"].poll() is None)
        if already_headful:
            return True
        start_url = self._capture_active_url(port) if port else None
        with self._lock:
            info = self._browsers.get(team_id)
            if info and info["proc"].poll() is None:
                self._quit_browser(info, team_id)   # flush session before relaunch
            url = self._launch(team_id, headful=True, start_url=start_url)
        if url:
            log.info("[%s] Takeover: visible browser opened on %s",
                     team_id, start_url or "about:blank")
        return url is not None

    def end_takeover(self, team_id: str) -> Optional[str]:
        """Hand control back after a login: relaunch the same profile headless so
        agents resume invisibly on the now-authenticated session. Idempotent —
        a no-op if already headless. Returns the CDP url."""
        with self._lock:
            info = self._browsers.get(team_id)
            if (info and not info.get("headful")
                    and info["proc"].poll() is None and self._healthy(info["port"])):
                return f"http://127.0.0.1:{info['port']}"
            if info and info["proc"].poll() is None:
                self._quit_browser(info, team_id)   # flush login cookies before relaunch
            return self._launch(team_id, headful=False)

    # -- lifecycle ----------------------------------------------------------
    def _quit_browser(self, info: dict, team_id: str) -> None:
        """Close Chrome GRACEFULLY (CDP ``Browser.close``) so cookies/sessions are
        flushed to the profile before we relaunch in the other mode — a plain
        SIGTERM races the network service and loses just-written login cookies.
        Falls back to a hard terminate if the graceful close doesn't land."""
        proc = info.get("proc")
        port = info.get("port")
        if not proc or proc.poll() is not None:
            return
        closed = False
        try:
            v = json.load(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=2))
            ws_url = v.get("webSocketDebuggerUrl")
            if ws_url:
                from websockets.sync.client import connect as _wsc
                with _wsc(ws_url, open_timeout=3, close_timeout=3) as ws:
                    ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                    try:
                        ws.recv(timeout=3)
                    except Exception:
                        pass
                for _ in range(30):  # ≤3s for the flushed, clean exit
                    if proc.poll() is not None:
                        closed = True
                        break
                    time.sleep(0.1)
        except Exception as e:
            log.debug("[%s] graceful Browser.close failed (%s); hard-terminating",
                      team_id, e)
        if not closed:
            self._terminate(proc)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        """Terminate a process and its whole process group (helpers/children)."""
        if proc is None or proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)          # POSIX only
        except Exception:
            pgid = None
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                if pgid is not None:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except Exception:
                        pass
                else:
                    proc.kill()
        except Exception:
            pass

    def shutdown_all(self) -> None:
        """Stop all team browsers (profiles persist on disk for next run).

        Close GRACEFULLY (``_quit_browser``) so cookies/sessions flush to each
        profile before exit — a plain SIGTERM races Chrome's network service and
        can lose just-written login cookies, so a restart after a login takeover
        would otherwise drop the session. ``_quit_browser`` falls back to a hard
        terminate if the graceful close doesn't land.
        """
        with self._lock:
            for team_id, info in self._browsers.items():
                log.info("[%s] Stopping team browser (port %d)", team_id, info["port"])
                self._quit_browser(info, team_id)
            self._browsers.clear()


# Process-wide singleton.
team_browser_manager = TeamBrowserManager()
