"""Embedded browser handover — stream the headless team Chrome into the
dashboard and inject the human's input back, over a single WebSocket.

This is how a human completes an interactive login / CAPTCHA / 2FA on a
**display-less VPS**: instead of relaunching Chrome as a visible window on the
host (browser_pool's "window" mode), we keep the team browser HEADLESS and
relay it to the operator's own browser via Chrome DevTools Protocol (CDP):

  * CDP ``Page.startScreencast`` pushes JPEG frames of the live page; we forward
    each to the dashboard, which paints it on a <canvas>, then ACK it.
  * The dashboard sends back mouse / keyboard / scroll / navigation events; we
    translate them into CDP ``Input.*`` / ``Page.navigate`` commands.

No relaunch, no host display, no cookie-flush race — the agent's own headless
session is driven directly, so when the human finishes, the agent resumes on the
exact authenticated session.

The pure functions here (``select_page_target``, ``translate_client_message``)
are unit-tested with a fake CDP socket; ``relay`` wires them to live sockets.
"""

import asyncio
import json
import logging
import urllib.request
from typing import Any, Dict, List, Optional

log = logging.getLogger("teams.browser.stream")

# Screencast tuning — JPEG keeps frames small enough to stream smoothly over a
# WAN; the cap bounds bandwidth on large viewports.
_SCREENCAST_PARAMS = {
    "format": "jpeg",
    "quality": 60,
    "maxWidth": 1280,
    "maxHeight": 1280,
    "everyNthFrame": 1,
}


def _cdp_targets(base_url: str) -> List[Dict[str, Any]]:
    """All CDP targets from ``http://127.0.0.1:<port>/json`` (synchronous —
    callers run it in an executor)."""
    with urllib.request.urlopen(f"{base_url}/json", timeout=3) as r:
        return json.load(r)


def select_page_target(targets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick the page the human should drive: the first real page target
    (prefer a non-blank, non-internal URL — that's the page the agent was
    blocked on), falling back to any page. Pure → unit-testable."""
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    for t in pages:
        u = t.get("url", "") or ""
        if u and not u.startswith(("about:", "chrome:", "devtools:", "chrome-extension:")):
            return t
    return pages[0]


def translate_client_message(msg: Dict[str, Any], next_id) -> Optional[Dict[str, Any]]:
    """Map one dashboard→server message to a CDP command dict (or None to
    ignore). ``next_id`` is a zero-arg callable returning the next CDP message
    id. Pure apart from the id source → unit-testable.

    Client message shapes:
      {type:'mouse', action:'pressed'|'released'|'moved'|'wheel',
       x,y, button?, deltaX?, deltaY?, clickCount?, modifiers?}
      {type:'key', action:'down'|'up'|'char', key,code,text?,
       windowsVirtualKeyCode?, modifiers?}
      {type:'text', text}                         # paste / insert
      {type:'navigate', url}
    """
    t = msg.get("type")

    if t == "mouse":
        action = msg.get("action")
        cdp_type = {
            "pressed": "mousePressed", "released": "mouseReleased",
            "moved": "mouseMoved", "wheel": "mouseWheel",
        }.get(action)
        if not cdp_type:
            return None
        params: Dict[str, Any] = {
            "type": cdp_type,
            "x": float(msg.get("x", 0)),
            "y": float(msg.get("y", 0)),
            "modifiers": int(msg.get("modifiers", 0)),
        }
        if cdp_type in ("mousePressed", "mouseReleased"):
            params["button"] = msg.get("button", "left")
            params["clickCount"] = int(msg.get("clickCount", 1))
        if cdp_type == "mouseWheel":
            params["deltaX"] = float(msg.get("deltaX", 0))
            params["deltaY"] = float(msg.get("deltaY", 0))
        return {"id": next_id(), "method": "Input.dispatchMouseEvent", "params": params}

    if t == "key":
        cdp_type = {"down": "keyDown", "up": "keyUp", "char": "char"}.get(msg.get("action"))
        if not cdp_type:
            return None
        params = {
            "type": cdp_type,
            "modifiers": int(msg.get("modifiers", 0)),
            "key": msg.get("key", ""),
            "code": msg.get("code", ""),
        }
        if msg.get("text"):
            params["text"] = msg["text"]
        if msg.get("windowsVirtualKeyCode") is not None:
            params["windowsVirtualKeyCode"] = int(msg["windowsVirtualKeyCode"])
        return {"id": next_id(), "method": "Input.dispatchKeyEvent", "params": params}

    if t == "text":
        return {"id": next_id(), "method": "Input.insertText",
                "params": {"text": str(msg.get("text", ""))}}

    if t == "navigate":
        url = str(msg.get("url", "")).strip()
        if not url:
            return None
        if not url.startswith(("http://", "https://", "about:", "file:")):
            url = "https://" + url
        return {"id": next_id(), "method": "Page.navigate", "params": {"url": url}}

    return None


async def relay(client_ws, team_id: str) -> None:
    """Bridge a dashboard control socket to the team's headless Chrome.

    ``client_ws`` is an already-accepted (and authenticated) FastAPI WebSocket.
    Runs until either side disconnects, then cleans up the screencast and the
    CDP socket. Errors are reported to the client as a ``{type:'error'}`` frame.
    """
    from websockets.asyncio.client import connect as ws_connect
    from teams_server.browser_pool import team_browser_manager

    loop = asyncio.get_running_loop()

    cdp_url = await loop.run_in_executor(None, team_browser_manager.ensure_team_browser, team_id)
    if not cdp_url:
        await client_ws.send_text(json.dumps({"type": "error",
            "payload": {"message": "No browser available on this host."}}))
        return

    try:
        targets = await loop.run_in_executor(None, _cdp_targets, cdp_url)
    except Exception as e:
        await client_ws.send_text(json.dumps({"type": "error",
            "payload": {"message": f"Could not list browser tabs: {e}"}}))
        return

    target = select_page_target(targets)
    if not target:
        await client_ws.send_text(json.dumps({"type": "error",
            "payload": {"message": "No page open in the team browser."}}))
        return

    _id = 0

    def next_id() -> int:
        nonlocal _id
        _id += 1
        return _id

    # The page being driven can change mid-session: an OAuth login often opens a
    # popup as a NEW target, so the human needs to switch tabs and drive that one.
    # We reconnect the CDP socket to the requested target and keep the SAME client
    # socket. `switch_to` carries the next target's debugger URL out of the input
    # pump; `client_gone` ends the outer loop for good.
    current_ws_url = target["webSocketDebuggerUrl"]
    switch_to = {"url": None}
    client_gone = {"v": False}
    # Last viewport the client asked for, reapplied after a tab switch / reconnect.
    viewport = {"w": 1280, "h": 800}

    async def _resolve_target_ws(target_id: str) -> Optional[str]:
        tabs = await loop.run_in_executor(None, _cdp_targets, cdp_url)
        for t in tabs:
            if t.get("id") == target_id and t.get("webSocketDebuggerUrl"):
                return t["webSocketDebuggerUrl"]
        return None

    try:
        while not client_gone["v"]:
            switch_to["url"] = None
            async with ws_connect(current_ws_url, max_size=64 * 1024 * 1024,
                                  open_timeout=10) as cdp:
                await cdp.send(json.dumps({"id": next_id(), "method": "Page.enable"}))
                await cdp.send(json.dumps({"id": next_id(), "method": "DOM.enable"}))
                # Make the page lay out at the panel's size so it isn't clipped.
                # The client sends a 'resize' as soon as it has measured the
                # canvas; until then use the last known size (or a sane default).
                await cdp.send(json.dumps({"id": next_id(),
                    "method": "Emulation.setDeviceMetricsOverride",
                    "params": {"width": viewport["w"], "height": viewport["h"],
                               "deviceScaleFactor": 1, "mobile": False}}))
                await cdp.send(json.dumps({"id": next_id(),
                    "method": "Page.startScreencast", "params": _SCREENCAST_PARAMS}))

                async def pump_cdp_to_client() -> None:
                    """Forward screencast frames to the dashboard, ACK each one."""
                    async for raw in cdp:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue
                        if evt.get("method") == "Page.screencastFrame":
                            p = evt.get("params", {})
                            md = p.get("metadata", {})
                            await client_ws.send_text(json.dumps({
                                "type": "frame",
                                "payload": {
                                    "data": p.get("data", ""),
                                    "deviceWidth": md.get("deviceWidth"),
                                    "deviceHeight": md.get("deviceHeight"),
                                    "offsetTop": md.get("offsetTop", 0),
                                    "pageScaleFactor": md.get("pageScaleFactor", 1),
                                },
                            }))
                            sid = p.get("sessionId")
                            if sid is not None:
                                await cdp.send(json.dumps({"id": next_id(),
                                    "method": "Page.screencastFrameAck",
                                    "params": {"sessionId": sid}}))

                async def pump_client_to_cdp() -> None:
                    """Translate dashboard input → CDP; handle tab list/switch locally.
                    Returns when the client disconnects or asks to switch tabs."""
                    while True:
                        try:
                            raw = await client_ws.receive_text()
                        except Exception:
                            client_gone["v"] = True
                            return
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        mtype = msg.get("type")
                        if mtype == "tabs":
                            tabs = await loop.run_in_executor(None, _cdp_targets, cdp_url)
                            await client_ws.send_text(json.dumps({"type": "tabs", "payload": {
                                "tabs": [{"targetId": t.get("id"), "title": t.get("title"),
                                          "url": t.get("url")}
                                         for t in tabs if t.get("type") == "page"]}}))
                            continue
                        if mtype == "switch_tab":
                            url = await _resolve_target_ws(msg.get("targetId", ""))
                            if url:
                                switch_to["url"] = url
                                return  # break out to reconnect on the new target
                            continue
                        if mtype == "resize":
                            w = int(msg.get("width") or 0)
                            h = int(msg.get("height") or 0)
                            if w > 0 and h > 0:
                                viewport["w"], viewport["h"] = w, h
                                await cdp.send(json.dumps({"id": next_id(),
                                    "method": "Emulation.setDeviceMetricsOverride",
                                    "params": {"width": w, "height": h,
                                               "deviceScaleFactor": 1, "mobile": False}}))
                            continue
                        if mtype == "ping":
                            continue
                        cmd = translate_client_message(msg, next_id)
                        if cmd is not None:
                            await cdp.send(json.dumps(cmd))

                done, pending = await asyncio.wait(
                    [asyncio.create_task(pump_cdp_to_client()),
                     asyncio.create_task(pump_client_to_cdp())],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                # If the CDP side ended (tab closed) with no explicit switch, stop.
                if not switch_to["url"]:
                    client_gone["v"] = True
                try:
                    await cdp.send(json.dumps({"id": next_id(), "method": "Page.stopScreencast"}))
                except Exception:
                    pass
            if switch_to["url"]:
                current_ws_url = switch_to["url"]
    except Exception as e:
        log.warning("[%s] [browser-stream] relay ended: %s", team_id, e)
        try:
            await client_ws.send_text(json.dumps({"type": "error",
                "payload": {"message": f"Browser stream error: {e}"}}))
        except Exception:
            pass
