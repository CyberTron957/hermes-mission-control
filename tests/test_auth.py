#!/usr/bin/env python3
"""Tests for the optional single-key auth (TEAMS_API_KEY).

Covers the HTTP middleware (all methods guarded, exempt allow-list, both header
forms, constant-time path), the /auth/check probe, and the WebSocket
first-message auth. Auth-disabled behaviour must be byte-identical to before.

Uses FastAPI TestClient; no LLM, no Hermes (server import keeps Hermes lazy).
The auth code reads the SERVER MODULE global TEAMS_API_KEY, so a single
monkeypatch on server_mod flips every code path.

Run:  pytest tests/test_auth.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import teams_server.server as server_mod  # noqa: E402

KEY = "sekrit-key-123"


@pytest.fixture()
def client():
    # TestClient does NOT run lifespan here, so daemons stays empty and the
    # WS snapshot is trivially small — exactly what we want.
    return TestClient(server_mod.app)


@pytest.fixture()
def auth_on(monkeypatch):
    monkeypatch.setattr(server_mod, "TEAMS_API_KEY", KEY)
    return KEY


@pytest.fixture()
def auth_off(monkeypatch):
    monkeypatch.setattr(server_mod, "TEAMS_API_KEY", "")
    return ""


# ---------------------------------------------------------------------------
# Auth DISABLED — behaves exactly as a pre-auth localhost server
# ---------------------------------------------------------------------------
def test_disabled_get_open(client, auth_off):
    assert client.get("/health").status_code == 200
    r = client.get("/auth/check").json()
    assert r == {"auth_required": False, "authorized": True}


def test_disabled_post_not_401(client, auth_off):
    # Handler may 400/404 (no such agent), but never 401.
    r = client.post("/agent/nope/task", json={"payload": "x"})
    assert r.status_code != 401


def test_disabled_ws_snapshot_immediate(client, auth_off):
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "state_snapshot"


def test_disabled_ws_ignores_stale_auth(client, auth_off):
    # A client carrying an old key talking to an auth-off server: the auth
    # message is a harmless no-op; the socket stays open and still pongs.
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # snapshot
        ws.send_json({"action": "auth", "api_key": "stale"})
        ws.send_json({"action": "ping"})
        assert ws.receive_json()["type"] == "pong"


# ---------------------------------------------------------------------------
# Auth ENABLED — HTTP
# ---------------------------------------------------------------------------
def test_enabled_get_requires_key(client, auth_on):
    assert client.get("/teams").status_code == 401
    assert client.get("/teams", headers={"X-API-Key": KEY}).status_code != 401
    assert client.get("/teams", headers={"Authorization": f"Bearer {KEY}"}).status_code != 401
    assert client.get("/teams", headers={"X-API-Key": "wrong"}).status_code == 401


def test_enabled_post_requires_key(client, auth_on):
    assert client.post("/agent/x/task", json={"payload": "y"}).status_code == 401
    r = client.post("/agent/x/task", json={"payload": "y"}, headers={"X-API-Key": KEY})
    assert r.status_code != 401


def test_enabled_exempt_paths_open(client, auth_on):
    assert client.get("/health").status_code == 200          # no header
    assert client.get("/").status_code in (200, 404)         # dashboard shell
    assert client.get("/auth/check").json() == {"auth_required": True, "authorized": False}
    assert client.get("/auth/check", headers={"X-API-Key": KEY}).json()["authorized"] is True


def test_enabled_openapi_guarded(client, auth_on):
    # Anything not on the allow-list is closed, including FastAPI's own docs.
    assert client.get("/openapi.json").status_code == 401


# ---------------------------------------------------------------------------
# Auth ENABLED — WebSocket first-message auth
# ---------------------------------------------------------------------------
def test_ws_auth_happy_path(client, auth_on):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"action": "auth", "api_key": KEY})
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "state_snapshot"


def test_ws_auth_wrong_key_closes(client, auth_on):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"action": "auth", "api_key": "wrong"})
            ws.receive_json()  # should raise on close
    assert exc.value.code == server_mod.WS_CLOSE_UNAUTHORIZED


def test_ws_auth_garbage_first_message_closes(client, auth_on):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.send_text("not json")
            ws.receive_json()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
