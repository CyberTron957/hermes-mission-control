#!/usr/bin/env python3
"""Tests for GET /teams/{team_id}/costs — aggregation of REAL provider token
counts from token_usage events, teams-side pricing, cache-hit %, legacy-row
differencing, team scoping, and the sweep queued/skipped counters.

Uses a real MonitoringDB on a temp path + FastAPI TestClient; no LLM, no
Hermes (server import keeps Hermes lazy).

Run:  pytest tests/test_costs_endpoint.py -v
"""

import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import teams_server.server as server_mod  # noqa: E402
from teams_server.monitoring import MonitoringDB  # noqa: E402


TEAM_CFG = {
    "agents": {
        "worker1": {"team_id": "t1", "model": "deepseek-v4-flash"},
        "worker2": {"team_id": "t1", "model": "mystery-model"},
        "boss": {"team_id": "t1", "model": "deepseek-v4-flash",
                 "is_supervisor": True},
        "outsider": {"team_id": "other", "model": "deepseek-v4-flash"},
    }
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = MonitoringDB(tmp_path / "mon.db")
    monkeypatch.setattr(server_mod, "monitor_db", db)
    monkeypatch.setattr(server_mod, "load_agents_config", lambda: TEAM_CFG)
    return TestClient(server_mod.app), db


def turn_event(db, agent, t_in, t_out, t_cache=0, model="deepseek-v4-flash"):
    db.log_event(agent, "token_usage", data={
        "model": model, "delta_tokens": t_in + t_out,
        "total_tokens": 0, "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0,
        "turn_input_tokens": t_in, "turn_output_tokens": t_out,
        "turn_cache_read_tokens": t_cache,
    })


def legacy_event(db, agent, cum_in, cum_out, cum_cache, cum_total):
    """Pre-deploy row shape: cumulative counters only, no model, no turn_*."""
    db.log_event(agent, "token_usage", data={
        "delta_tokens": 0, "total_tokens": cum_total,
        "input_tokens": cum_in, "output_tokens": cum_out,
        "cache_read_tokens": cum_cache, "estimated_cost_usd": 0.0,
    })


def test_turn_rows_aggregate_price_and_cache_pct(client):
    tc, db = client
    turn_event(db, "worker1", 80_000, 1_000)
    turn_event(db, "worker1", 20_000, 500, t_cache=100_000)
    turn_event(db, "outsider", 999_999, 999)        # other team — excluded

    body = tc.get("/teams/t1/costs?hours=1").json()
    w = body["agents"]["worker1"]
    assert w["turns"] == 2
    assert w["input_tokens"] == 100_000
    assert w["output_tokens"] == 1_500
    assert w["cache_read_tokens"] == 100_000
    assert w["cache_hit_pct"] == 50.0               # 100k / (100k + 100k)
    # 100k in * .19/M + 1.5k out * .51/M + 100k cache * .019/M
    assert w["est_cost_usd"] == round(
        (100_000 * 0.19 + 1_500 * 0.51 + 100_000 * 0.019) / 1e6, 4)
    assert "outsider" not in body["agents"]
    assert body["totals"]["input_tokens"] == 100_000


def test_legacy_rows_differenced_with_reset(client):
    tc, db = client
    # Baseline row (turn happened before the window) → contributes nothing.
    legacy_event(db, "worker1", 50_000, 700, 0, 50_700)
    # Next row: +30k in, +300 out.
    legacy_event(db, "worker1", 80_000, 1_000, 0, 81_000)
    # Session rotated (total shrank) → counters restart from zero.
    legacy_event(db, "worker1", 10_000, 100, 0, 10_100)

    body = tc.get("/teams/t1/costs?hours=1").json()
    w = body["agents"]["worker1"]
    assert w["turns"] == 2                          # baseline row not counted
    assert w["input_tokens"] == 30_000 + 10_000     # baseline excluded
    assert w["output_tokens"] == 300 + 100


def test_unknown_model_flags_unpriced_not_wrong(client):
    tc, db = client
    turn_event(db, "worker2", 10_000, 100, model="mystery-model")
    body = tc.get("/teams/t1/costs?hours=1").json()
    w = body["agents"]["worker2"]
    assert w["unpriced"] is True
    assert w["est_cost_usd"] == 0.0                 # never a made-up number
    assert w["input_tokens"] == 10_000


def test_legacy_row_model_falls_back_to_config(client):
    tc, db = client
    legacy_event(db, "worker1", 0, 0, 0, 0)         # baseline
    legacy_event(db, "worker1", 1_000_000, 0, 0, 1_000_000)
    body = tc.get("/teams/t1/costs?hours=1").json()
    w = body["agents"]["worker1"]
    assert w["model"] == "deepseek-v4-flash"        # from agents config
    assert w["est_cost_usd"] == 0.19                # priced via the fallback


def test_sweep_counters_from_supervisor_events(client):
    tc, db = client
    db.log_event("boss", "supervisor_sweep", data={"peers": 2})
    db.log_event("boss", "supervisor_sweep_skipped", data={"consecutive_skips": 1})
    db.log_event("boss", "supervisor_sweep_skipped", data={"consecutive_skips": 2})
    db.log_event("outsider", "supervisor_sweep_skipped", data={})  # not ours

    body = tc.get("/teams/t1/costs?hours=1").json()
    assert body["sweeps"] == {"queued": 1, "skipped": 2}


def test_window_excludes_old_events(client, monkeypatch):
    tc, db = client
    turn_event(db, "worker1", 5_000, 50)
    # Age the row well past the 1h window.
    with db._conn() as conn:
        conn.execute("UPDATE events SET timestamp = ?", (time.time() - 7200,))
        conn.commit()
    body = tc.get("/teams/t1/costs?hours=1").json()
    assert body["agents"] == {}
    assert body["totals"]["turns"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
