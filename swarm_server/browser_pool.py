"""Per-team persistent, shared browser pool.

Each team gets ONE long-lived Chrome (DevTools/CDP) bound to a stable
``--user-data-dir`` under ``data/teams/<team>/.browser-profile``. Every agent in
that team is pointed at the same ``browser.cdp_url`` (written into its
config.yaml), so they share one browser — same cookies, logins, and storage.

Durability: because the profile directory is a fixed path on disk, the browser's
state survives a server restart. On restart we relaunch Chrome against the same
profile dir; cookies/logins are still there. (Chrome itself only allows one
process per user-data-dir, which is exactly the one-browser-per-team invariant.)

Isolation: one profile per team => teams never see each other's cookies/sessions.

How a team's browser is shown — ONE simple model:
  * The browser ALWAYS renders on a dedicated, hidden Xvfb display (one per team),
    never on the host's real desktop. The agent drives it over CDP, which is
    display-independent, so it never intrudes on the user's screen.
  * A noVNC viewer (x11vnc + websockify, bound to 127.0.0.1) is brought up
    alongside the browser and STAYS up at a fixed URL for the browser's life.
    So you can open that URL any time to watch the agent browse, and a human
    "takeover" is simply: the agent hands you the same link, you act, you reply
    "done". There is no per-takeover start/stop — ``begin_takeover`` just returns
    the stable URL and ``end_takeover`` is a no-op.
  * This works on every host, including headless / SSH (forward the web port).

This connects via Hermes' CDP-override path (``browser.cdp_url``), which takes
precedence over both the cloud provider and the local launcher — so it works
without cloud credentials and reuses the Playwright Chromium we install locally.
"""

import glob
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional

from swarm_server.config import WORKSPACE_ROOT

log = logging.getLogger("swarm.browser")

# Base port for per-team CDP endpoints; each team gets the next free port up.
_BASE_CDP_PORT = 9333
# Port bases for the always-on noVNC viewer. The actual ports are derived from
# the team's display number so the URL is stable across restarts.
_BASE_VNC_PORT = 5900   # x11vnc RFB port
_BASE_WEB_PORT = 6080   # websockify/noVNC web port (what the human opens)
# noVNC web assets shipped by the distro 'novnc' package.
_NOVNC_WEB = "/usr/share/novnc"
# Hidden Xvfb virtual screen geometry — also the size seen over VNC; the browser
# window is sized to match so it fills that view exactly.
_SCREEN_W, _SCREEN_H = 1440, 900


def _find_chromium() -> Optional[str]:
    """Locate a Chromium executable from the Playwright browser cache.

    Prefers the full "Chrome for Testing" build (best site compatibility +
    persistent profile support); falls back to the lighter headless-shell.
    """
    roots = []
    pbp = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if pbp:
        roots.append(Path(pbp))
    roots += [
        Path.home() / "Library" / "Caches" / "ms-playwright",   # macOS
        Path.home() / ".cache" / "ms-playwright",                # Linux
    ]
    patterns = [
        "chromium-*/chrome-mac*/*.app/Contents/MacOS/*",         # mac full chromium
        "chromium-*/chrome-linux*/chrome",                       # linux full chromium
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    ]
    for root in roots:
        for pat in patterns:
            for hit in sorted(glob.glob(str(root / pat)), reverse=True):
                if os.path.isfile(hit) and os.access(hit, os.X_OK):
                    return hit
    return None


class TeamBrowserManager:
    """Launches and tracks one persistent Chrome per team (on a hidden Xvfb),
    with an always-on noVNC viewer."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # team_id -> {"proc": Popen, "port": int, "profile": str, "display": str}
        self._browsers: Dict[str, dict] = {}
        self._ports: Dict[str, int] = {}
        # team_id -> the dedicated hidden display its browser renders on for life.
        self._team_disp: Dict[str, str] = {}
        # team_id -> the Xvfb Popen backing that display.
        self._xvfb: Dict[str, subprocess.Popen] = {}
        # team_id -> {"x11vnc": Popen, "web": Popen, "vnc_port", "web_port", "url"}
        self._vnc: Dict[str, dict] = {}
        self._chromium = _find_chromium()
        if self._chromium:
            log.info("Team browser pool using chromium: %s", self._chromium)
        else:
            log.warning(
                "No Chromium found for team browser pool "
                "(install with: npx playwright install chromium). "
                "Team browsers disabled."
            )

    # -- port helpers -------------------------------------------------------
    @staticmethod
    def _port_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) != 0

    def _free_port(self, base: int, used: set) -> int:
        """First port >= base that is neither in `used` nor already bound."""
        port = base
        while port in used or not self._port_free(port):
            port += 1
        return port

    def _assign_port(self, team_id: str) -> int:
        if team_id in self._ports:
            return self._ports[team_id]
        port = self._free_port(_BASE_CDP_PORT, set(self._ports.values()))
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

    # -- X11 launch environment --------------------------------------------
    @staticmethod
    def _x11_env(display: str) -> dict:
        """Environment for processes that MUST talk to our Xvfb X11 display.

        On a Wayland host the session exports WAYLAND_DISPLAY / GDK_BACKEND=wayland,
        which makes both Chrome and x11vnc prefer the Wayland compositor and ignore
        our Xvfb (Chrome then renders on the real desktop; x11vnc refuses to start).
        Scrubbing those and forcing an X11 session type pins them to the Xvfb.
        """
        env = {k: v for k, v in os.environ.items()
               if k not in ("WAYLAND_DISPLAY", "GDK_BACKEND")}
        env["DISPLAY"] = display
        env["XDG_SESSION_TYPE"] = "x11"
        return env

    # -- display: a dedicated hidden Xvfb per team --------------------------
    @staticmethod
    def _display_socket_exists(disp: str) -> bool:
        """True if an X server is listening on a DISPLAY like ':100' (socket present)."""
        try:
            num = (disp or "").strip().lstrip(":").split(".")[0]
            return bool(num) and os.path.exists(f"/tmp/.X11-unix/X{num}")
        except Exception:
            return False

    def _ensure_team_xvfb(self, team_id: str) -> str:
        """Allocate (once) and keep alive a dedicated hidden Xvfb display for this
        team, returning its DISPLAY string. The browser renders here invisibly; a
        human only ever sees it through the always-on noVNC viewer.

        SWARM_BROWSER_DISPLAY overrides everything; if Xvfb is somehow missing we
        fall back to $DISPLAY/:0 so the agent still has *a* browser.
        """
        with self._lock:
            disp = self._team_disp.get(team_id)
            proc = self._xvfb.get(team_id)
        if disp and (
            disp == os.environ.get("SWARM_BROWSER_DISPLAY", "").strip()
            or (proc is not None and proc.poll() is None and self._display_socket_exists(disp))
        ):
            return disp

        override = os.environ.get("SWARM_BROWSER_DISPLAY", "").strip()
        if override:
            with self._lock:
                self._team_disp[team_id] = override
            log.info("[%s] Browser display pinned to %s (override)", team_id, override)
            return override

        xvfb = shutil.which("Xvfb")
        if not xvfb:
            fallback = os.environ.get("DISPLAY") or ":0"
            log.warning("[%s] Xvfb unavailable; falling back to %s "
                        "(browser will be visible on this desktop)", team_id, fallback)
            with self._lock:
                self._team_disp[team_id] = fallback
            return fallback

        for num in range(100, 400):
            if os.path.exists(f"/tmp/.X11-unix/X{num}"):
                continue
            disp = f":{num}"
            try:
                p = subprocess.Popen(
                    [xvfb, disp, "-screen", "0", f"{_SCREEN_W}x{_SCREEN_H}x24",
                     "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as e:
                log.error("[%s] Failed to start Xvfb: %s", team_id, e)
                break
            ok = False
            for _ in range(40):
                if os.path.exists(f"/tmp/.X11-unix/X{num}"):
                    ok = True
                    break
                if p.poll() is not None:
                    break
                time.sleep(0.1)
            if ok:
                with self._lock:
                    self._xvfb[team_id] = p
                    self._team_disp[team_id] = disp
                log.info("[%s] Hidden Xvfb display ready: %s (%dx%d)",
                         team_id, disp, _SCREEN_W, _SCREEN_H)
                return disp
            self._terminate(p)

        fallback = os.environ.get("DISPLAY") or ":0"
        log.warning("[%s] Could not allocate an Xvfb display; falling back to %s",
                    team_id, fallback)
        with self._lock:
            self._team_disp[team_id] = fallback
        return fallback

    # -- always-on noVNC viewer onto the team's hidden Xvfb -----------------
    @staticmethod
    def _derive_vnc_ports(display: str) -> tuple:
        """Stable (rfb, web) ports derived from the display number, so the viewer
        URL doesn't change across restarts (display :100 -> 5900/6080)."""
        try:
            n = int((display or "").lstrip(":").split(".")[0])
        except Exception:
            n = 100
        off = max(0, n - 100)
        return _BASE_VNC_PORT + off, _BASE_WEB_PORT + off

    def _ensure_vnc(self, team_id: str, display: str) -> Optional[str]:
        """Ensure the team's always-on noVNC viewer is running and return its URL.
        Idempotent and best-effort (returns None if the VNC tooling is missing or
        fails — the agent's browsing is unaffected either way)."""
        with self._lock:
            v = self._vnc.get(team_id)
        if v and v["x11vnc"].poll() is None and v["web"].poll() is None:
            return v["url"]
        if v:  # half-dead — clean up and restart
            self._stop_vnc(team_id)

        x11vnc = shutil.which("x11vnc")
        websockify = shutil.which("websockify")
        if not x11vnc or not websockify:
            log.error("[%s] noVNC viewer needs x11vnc + websockify but one is "
                      "missing; the browser will run but can't be shown", team_id)
            return None

        vnc_port, web_port = self._derive_vnc_ports(display)
        with self._lock:
            used_v = {x["vnc_port"] for x in self._vnc.values()}
            used_w = {x["web_port"] for x in self._vnc.values()}
        if vnc_port in used_v or not self._port_free(vnc_port):
            vnc_port = self._free_port(_BASE_VNC_PORT, used_v)
        if web_port in used_w or not self._port_free(web_port):
            web_port = self._free_port(_BASE_WEB_PORT, used_w)

        env = self._x11_env(display)
        try:
            vproc = subprocess.Popen(
                [x11vnc, "-display", display, "-rfbport", str(vnc_port),
                 "-localhost", "-nopw", "-forever", "-shared", "-noxdamage", "-quiet"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env=env,
            )
        except Exception as e:
            log.error("[%s] Failed to start x11vnc: %s", team_id, e)
            return None

        web_root = _NOVNC_WEB if os.path.isdir(_NOVNC_WEB) else None
        ws_cmd = [websockify] + (["--web", web_root] if web_root else []) + \
                 [f"127.0.0.1:{web_port}", f"127.0.0.1:{vnc_port}"]
        try:
            wproc = subprocess.Popen(
                ws_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            log.error("[%s] Failed to start websockify: %s", team_id, e)
            self._terminate(vproc)
            return None

        url = (f"http://127.0.0.1:{web_port}/vnc.html"
               f"?autoconnect=1&resize=remote&reconnect=1&path=websockify")
        with self._lock:
            self._vnc[team_id] = {
                "x11vnc": vproc, "web": wproc,
                "vnc_port": vnc_port, "web_port": web_port, "url": url,
            }
        log.info("[%s] noVNC viewer up: %s (rfb=%d display=%s)",
                 team_id, url, vnc_port, display)
        return url

    def _stop_vnc(self, team_id: str) -> None:
        """Tear down the team's noVNC viewer (used on shutdown / restart only)."""
        with self._lock:
            v = self._vnc.pop(team_id, None)
        if not v:
            return
        for key in ("web", "x11vnc"):
            try:
                self._terminate(v[key])
            except Exception:
                pass

    def takeover_url(self, team_id: str) -> Optional[str]:
        """The stable noVNC URL for this team's browser, or None if not up."""
        with self._lock:
            v = self._vnc.get(team_id)
        return v["url"] if v else None

    def begin_takeover(self, team_id: str, display: Optional[str] = None) -> Optional[str]:
        """Hand the team browser to a human: make sure the browser (+ its always-on
        viewer) is up and return the URL to open. The browser never appears on the
        host desktop — only inside the viewer. `display` is accepted for
        backward-compat and ignored."""
        self.ensure_team_browser(team_id)
        return self.takeover_url(team_id)

    def end_takeover(self, team_id: str) -> Optional[str]:
        """Hand control back. The viewer is always-on, so there is nothing to tear
        down — the browser simply keeps running hidden and the agent resumes on the
        same session. Returns the (unchanged) CDP URL."""
        with self._lock:
            info = self._browsers.get(team_id)
        return f"http://127.0.0.1:{info['port']}" if info else None

    # -- lifecycle ----------------------------------------------------------
    def ensure_team_browser(self, team_id: str) -> Optional[str]:
        """Return the team's CDP URL, launching/healing the browser (and its
        always-on noVNC viewer) as needed. Idempotent and cheap on the happy path.
        Returns None when no Chromium is available so callers fall back gracefully.
        """
        if not self._chromium:
            return None

        display = self._ensure_team_xvfb(team_id)

        with self._lock:
            info = self._browsers.get(team_id)
            if info and info["proc"].poll() is None and self._healthy(info["port"]):
                port = info["port"]
            else:
                # Reuse the team's port across relaunches so the cdp_url written
                # into agent configs stays valid within a server run.
                port = info["port"] if info else self._assign_port(team_id)

                # Reap a dead/stale process holding this slot.
                if info and info["proc"].poll() is None:
                    self._terminate(info["proc"])

                profile = WORKSPACE_ROOT / team_id / ".browser-profile"
                profile.mkdir(parents=True, exist_ok=True)
                # A stale lock from an unclean shutdown blocks relaunch; clear it.
                for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                    try:
                        (profile / lock_name).unlink()
                    except OSError:
                        pass

                # Headful (NO --headless) so the browser passes headless
                # fingerprinting (webdriver=false via AutomationControlled off).
                # --ozone-platform=x11 + the scrubbed env pin it to the team's
                # hidden Xvfb (never the host's Wayland/X desktop). The window is
                # sized to fill the virtual screen so the noVNC view shows the whole
                # browser, and the anti-throttle flags keep it painting with no
                # client attached so agent screenshots work.
                args = [
                    self._chromium,
                    f"--remote-debugging-port={port}",
                    "--remote-debugging-address=127.0.0.1",
                    f"--user-data-dir={profile}",
                    "--ozone-platform=x11",
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-dev-shm-usage",
                    "--disable-background-networking",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling",
                    "--window-position=0,0",
                    f"--window-size={_SCREEN_W},{_SCREEN_H}",
                    "about:blank",
                ]
                try:
                    # start_new_session=True puts Chrome (and its renderer/gpu
                    # helper children) in its own process group so we can reap the
                    # WHOLE tree on shutdown instead of orphaning helpers.
                    proc = subprocess.Popen(
                        args,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                        env=self._x11_env(display),
                    )
                except Exception as e:
                    log.error("[%s] Failed to launch team browser: %s", team_id, e)
                    return None

                self._browsers[team_id] = {
                    "proc": proc, "port": port, "profile": str(profile),
                    "display": display,
                }
                self._ports[team_id] = port

                # Wait (≤10s) for the CDP endpoint to come up.
                ready = False
                for _ in range(50):
                    if proc.poll() is not None:
                        log.error("[%s] Team browser exited during startup", team_id)
                        return None
                    if self._healthy(port):
                        log.info("[%s] Team browser ready on port %d display=%s (profile=%s)",
                                 team_id, port, display, profile)
                        ready = True
                        break
                    time.sleep(0.2)
                if not ready:
                    log.error("[%s] Team browser did not become healthy on port %d",
                              team_id, port)
                    return None

        # Outside the lock: ensure the always-on viewer (idempotent, best-effort).
        self._ensure_vnc(team_id, display)
        return f"http://127.0.0.1:{port}"

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        """Terminate a process and its whole process group (helpers/children)."""
        if proc is None or proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
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
        """Terminate all team browsers, noVNC viewers and Xvfb displays (profiles
        persist on disk for next run)."""
        with self._lock:
            for team_id in list(self._vnc.keys()):
                self._stop_vnc(team_id)
            for team_id, info in self._browsers.items():
                log.info("[%s] Stopping team browser (port %d)", team_id, info["port"])
                self._terminate(info["proc"])
            self._browsers.clear()
            for team_id, proc in self._xvfb.items():
                self._terminate(proc)
            self._xvfb.clear()


# Process-wide singleton.
team_browser_manager = TeamBrowserManager()
