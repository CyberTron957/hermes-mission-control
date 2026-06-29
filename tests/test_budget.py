#!/usr/bin/env python3
"""Tests for per-team daily spend guardrails (teams_server/budget).

Covers the tracker (metering, one-shot exceeded latch, override, limit-raise,
UTC rollover, unpriced-model token fallback, thread-safety), rebuild from
monitoring.db, the config helpers, and the REST endpoints.

A fresh tracker is built per test (the module singleton is shared, so we
construct TeamBudgetTracker directly) with the team-limit lookup monkeypatched
so no real config file is touched. Broadcasts are captured via a stubbed
_broadcast.

Run:  pytest tests/test_budget.py -v
"""

import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import teams_server.budget as budget_mod  # noqa: E402
from teams_server.budget import TeamBudgetTracker  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import teams_server.server as server_mod  # noqa: E402
from teams_server.monitoring import MonitoringDB  # noqa: E402


# Priced model in the teams price map; "mystery-model" is intentionally unpriced.
PRICED = "deepseek-v4-flash"


@pytest.fixture()
def tracker(monkeypatch):
    """A clean tracker whose limit lookup we control, with broadcasts captured."""
    t = TeamBudgetTracker()
    limits = {}

    def fake_limits(team_id):
        # Mirror the real cache-first lookup so set_limits' cache write is
        # honored (the real _limits reads self._limits_cache); fall back to the
        # test-controlled `limits` dict instead of hitting the config file.
        if team_id in t._limits_cache:
            return t._limits_cache[team_id]
        return limits.get(team_id, {"daily_usd": 0.0, "daily_tokens": 0})

    monkeypatch.setattr(t, "_limits", fake_limits)

    events = []
    monkeypatch.setattr(budget_mod, "_broadcast",
                        lambda evt, payload: events.append((evt, payload)), raising=False)
    # _emit imports _broadcast lazily from websocket; patch there too.
    import teams_server.websocket as ws_mod
    monkeypatch.setattr(ws_mod, "_broadcast",
                        lambda evt, payload: events.append((evt, payload)))
    return t, limits, events


# ---------------------------------------------------------------------------
# Metering + the one-shot exceeded latch
# ---------------------------------------------------------------------------
def test_under_budget_not_blocked(tracker):
    t, limits, events = tracker
    limits["t1"] = {"daily_usd": 100.0, "daily_tokens": 0}
    t.record_turn("t1", PRICED, 1000, 1000, 0)
    assert not t.is_blocked("t1")
    assert not any(e[0] == "budget_exceeded" for e in events)


def test_crossing_usd_cap_blocks_and_broadcasts_once(tracker):
    t, limits, events = tracker
    limits["t1"] = {"daily_usd": 0.0001, "daily_tokens": 0}  # tiny cap
    t.record_turn("t1", PRICED, 100000, 100000, 0)
    assert t.is_blocked("t1")
    exceeded = [e for e in events if e[0] == "budget_exceeded"]
    assert len(exceeded) == 1
    # Another turn must NOT re-broadcast (latch holds).
    t.record_turn("t1", PRICED, 100000, 100000, 0)
    assert len([e for e in events if e[0] == "budget_exceeded"]) == 1


def test_token_cap_blocks_unpriced_model(tracker):
    t, limits, events = tracker
    limits["t1"] = {"daily_usd": 5.0, "daily_tokens": 1000}
    t.record_turn("t1", "mystery-model", 800, 800, 0)  # 1600 tokens > 1000
    st = t.status("t1")
    assert st["unpriced_turns"] == 1
    assert st["spent_usd"] == 0.0          # unpriced → USD meter stays flat
    assert t.is_blocked("t1")               # but token cap enforces


def test_unlimited_when_zero_caps(tracker):
    t, limits, _ = tracker
    limits["t1"] = {"daily_usd": 0.0, "daily_tokens": 0}
    for _ in range(50):
        t.record_turn("t1", PRICED, 100000, 100000, 0)
    assert not t.is_blocked("t1")


# ---------------------------------------------------------------------------
# Override + limit raise un-block
# ---------------------------------------------------------------------------
def test_override_unblocks_and_broadcasts(tracker):
    t, limits, events = tracker
    limits["t1"] = {"daily_usd": 0.0001, "daily_tokens": 0}
    t.record_turn("t1", PRICED, 100000, 100000, 0)
    assert t.is_blocked("t1")
    t.override_today("t1")
    assert not t.is_blocked("t1")
    assert any(e[0] == "budget_resumed" and e[1].get("reason") == "override" for e in events)


def test_raising_limit_unblocks(tracker):
    t, limits, events = tracker
    limits["t1"] = {"daily_usd": 0.0001, "daily_tokens": 0}
    t.record_turn("t1", PRICED, 100000, 100000, 0)
    assert t.is_blocked("t1")
    # set_limits writes the cache directly; raise the cap above current spend.
    t.set_limits("t1", 1000.0, 0)
    assert not t.is_blocked("t1")
    assert any(e[0] == "budget_resumed" and e[1].get("reason") == "limit_raised" for e in events)


# ---------------------------------------------------------------------------
# UTC rollover resets spend and resumes paused teams
# ---------------------------------------------------------------------------
def test_rollover_resets_and_resumes(tracker, monkeypatch):
    t, limits, events = tracker
    limits["t1"] = {"daily_usd": 0.0001, "daily_tokens": 0}
    t.record_turn("t1", PRICED, 100000, 100000, 0)
    assert t.is_blocked("t1")
    # Simulate the clock crossing midnight: force a different day string.
    monkeypatch.setattr(budget_mod, "_utc_day", lambda: "2999-01-01")
    assert not t.is_blocked("t1")          # rollover cleared the latch + spend
    assert t.status("t1")["spent_usd"] == 0.0
    assert any(e[0] == "budget_resumed" and e[1].get("reason") == "rollover" for e in events)


# ---------------------------------------------------------------------------
# Thread-safety: concurrent record_turn from many workers totals exactly
# ---------------------------------------------------------------------------
def test_concurrent_record_turn(tracker):
    t, limits, _ = tracker
    limits["t1"] = {"daily_usd": 0.0, "daily_tokens": 0}  # unlimited, just count
    N = 200

    def worker():
        for _ in range(N):
            t.record_turn("t1", PRICED, 10, 5, 1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    st = t.status("t1")
    assert st["turns"] == 8 * N
    assert st["spent_tokens"] == 8 * N * 16


# ---------------------------------------------------------------------------
# rebuild() re-sums today's modern rows, skips legacy + aged-out rows
# ---------------------------------------------------------------------------
def test_rebuild_from_db(tmp_path, monkeypatch):
    db = MonitoringDB(tmp_path / "mon.db")
    cfg = {"agents": {"w1": {"team_id": "t1", "model": PRICED}},
           "teams": {"t1": {"name": "T"}}}

    # A modern per-turn row for today.
    db.log_event("w1", "token_usage", data={
        "model": PRICED, "turn_input_tokens": 1000, "turn_output_tokens": 500,
        "turn_cache_read_tokens": 0})
    # A legacy cumulative row (no turn_* keys) — must be ignored.
    db.log_event("w1", "token_usage", data={
        "model": PRICED, "input_tokens": 999999, "output_tokens": 999999,
        "total_tokens": 1999998})

    t = TeamBudgetTracker()
    monkeypatch.setattr(t, "_limits",
                        lambda team_id: {"daily_usd": 0.0, "daily_tokens": 0})
    t.rebuild(db, cfg)
    st = t.status("t1")
    assert st["turns"] == 1                 # only the modern row
    assert st["spent_tokens"] == 1500


# ---------------------------------------------------------------------------
# config helpers
# ---------------------------------------------------------------------------
def test_get_team_budget_sanitizes():
    from teams_server.config import get_team_budget
    cfg = {"teams": {"t1": {"budget": {"daily_usd": "5.5", "daily_tokens": -3}}}}
    b = get_team_budget(cfg, "t1")
    assert b["daily_usd"] == 5.5
    assert b["daily_tokens"] == 0           # negative clamped
    assert get_team_budget({"teams": {}}, "ghost") == {"daily_usd": 0.0, "daily_tokens": 0}


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server_mod, "TEAMS_API_KEY", "")
    fresh = TeamBudgetTracker()
    monkeypatch.setattr(fresh, "_limits",
                        lambda team_id: {"daily_usd": 0.0, "daily_tokens": 0})
    monkeypatch.setattr("teams_server.budget.budget_tracker", fresh)
    saved = {}
    monkeypatch.setattr("teams_server.config.set_team_budget",
                        lambda tid, usd, toks: saved.update({tid: (usd, toks)}) or {})
    return TestClient(server_mod.app), saved


def test_endpoint_get_budget(client):
    cl, _ = client
    r = cl.get("/teams/t1/budget")
    assert r.status_code == 200
    body = r.json()
    assert body["team_id"] == "t1" and body["blocked"] is False


def test_endpoint_put_budget(client):
    cl, saved = client
    r = cl.put("/teams/t1/budget", json={"daily_usd": 12.5, "daily_tokens": 0})
    assert r.status_code == 200
    assert saved["t1"] == (12.5, 0)         # persisted via set_team_budget


def test_endpoint_put_budget_rejects_garbage(client):
    cl, _ = client
    r = cl.put("/teams/t1/budget", json={"daily_usd": "abc"})
    assert r.status_code == 400


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
