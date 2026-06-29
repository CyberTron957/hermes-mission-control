#!/usr/bin/env python3
"""Tests for the embedded-browser handover relay (teams_server/browser_stream).

Covers the pure logic — CDP page-target selection and dashboard→CDP message
translation — plus the WS endpoint's team-validation guard. The live screencast
relay needs a real Chrome, so it's exercised manually (see docs/deploy-vps.md);
here we lock down the translation table and target picking that the relay leans
on.

Run:  pytest tests/test_browser_stream.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import teams_server.server as server_mod  # noqa: E402
from teams_server import browser_stream as bs  # noqa: E402


# ---------------------------------------------------------------------------
# select_page_target
# ---------------------------------------------------------------------------
def test_select_prefers_real_page_over_blank():
    targets = [
        {"type": "background_page", "url": "chrome://x", "webSocketDebuggerUrl": "ws://a"},
        {"type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://blank"},
        {"type": "page", "url": "https://accounts.google.com", "webSocketDebuggerUrl": "ws://real"},
    ]
    assert bs.select_page_target(targets)["webSocketDebuggerUrl"] == "ws://real"


def test_select_falls_back_to_blank_page():
    targets = [{"type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://blank"}]
    assert bs.select_page_target(targets)["webSocketDebuggerUrl"] == "ws://blank"


def test_select_none_when_no_pages():
    assert bs.select_page_target([{"type": "worker", "webSocketDebuggerUrl": "ws://w"}]) is None
    assert bs.select_page_target([]) is None


def test_select_skips_pages_without_debugger_url():
    targets = [{"type": "page", "url": "https://x.com"}]  # no ws url
    assert bs.select_page_target(targets) is None


# ---------------------------------------------------------------------------
# translate_client_message
# ---------------------------------------------------------------------------
@pytest.fixture()
def ids():
    counter = {"n": 0}

    def nxt():
        counter["n"] += 1
        return counter["n"]

    return nxt


def test_mouse_press(ids):
    cmd = bs.translate_client_message(
        {"type": "mouse", "action": "pressed", "x": 12, "y": 34, "button": "left", "clickCount": 2},
        ids)
    assert cmd["method"] == "Input.dispatchMouseEvent"
    p = cmd["params"]
    assert p["type"] == "mousePressed" and p["x"] == 12 and p["y"] == 34
    assert p["button"] == "left" and p["clickCount"] == 2


def test_mouse_wheel_has_deltas(ids):
    cmd = bs.translate_client_message(
        {"type": "mouse", "action": "wheel", "x": 0, "y": 0, "deltaY": 120}, ids)
    assert cmd["params"]["type"] == "mouseWheel"
    assert cmd["params"]["deltaY"] == 120


def test_mouse_move_no_button_fields(ids):
    cmd = bs.translate_client_message({"type": "mouse", "action": "moved", "x": 5, "y": 6}, ids)
    assert cmd["params"]["type"] == "mouseMoved"
    assert "button" not in cmd["params"]


def test_key_down_carries_text_and_vk(ids):
    cmd = bs.translate_client_message(
        {"type": "key", "action": "down", "key": "a", "code": "KeyA",
         "text": "a", "windowsVirtualKeyCode": 65}, ids)
    assert cmd["method"] == "Input.dispatchKeyEvent"
    assert cmd["params"]["type"] == "keyDown"
    assert cmd["params"]["text"] == "a" and cmd["params"]["windowsVirtualKeyCode"] == 65


def test_text_insert(ids):
    cmd = bs.translate_client_message({"type": "text", "text": "hello"}, ids)
    assert cmd["method"] == "Input.insertText" and cmd["params"]["text"] == "hello"


def test_navigate_adds_scheme(ids):
    cmd = bs.translate_client_message({"type": "navigate", "url": "example.com"}, ids)
    assert cmd["method"] == "Page.navigate"
    assert cmd["params"]["url"] == "https://example.com"


def test_navigate_keeps_explicit_scheme(ids):
    cmd = bs.translate_client_message({"type": "navigate", "url": "http://x.test"}, ids)
    assert cmd["params"]["url"] == "http://x.test"


def test_navigate_empty_ignored(ids):
    assert bs.translate_client_message({"type": "navigate", "url": "  "}, ids) is None


def test_unknown_type_ignored(ids):
    assert bs.translate_client_message({"type": "bogus"}, ids) is None


def test_ids_increment(ids):
    a = bs.translate_client_message({"type": "text", "text": "x"}, ids)
    b = bs.translate_client_message({"type": "text", "text": "y"}, ids)
    assert b["id"] == a["id"] + 1


# ---------------------------------------------------------------------------
# Endpoint guard: unknown team is rejected before any relay
# ---------------------------------------------------------------------------
def test_browser_ws_unknown_team(monkeypatch):
    monkeypatch.setattr(server_mod, "TEAMS_API_KEY", "")
    monkeypatch.setattr(server_mod, "load_agents_config", lambda: {"teams": {}, "agents": {}})
    client = TestClient(server_mod.app)
    with client.websocket_connect("/teams/ghost/browser/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "ghost" in msg["payload"]["message"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
