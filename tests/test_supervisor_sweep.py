#!/usr/bin/env python3
"""Unit tests for the supervisor interval-sweep mechanism (replaces the retired
token-threshold single-peer reviews). No LLM, no Hermes — the daemon method is
exercised on a SimpleNamespace stand-in and the monitoring DB is faked.

Covers:
  1. INTERVAL GATING — sweep fires only once the per-agent interval elapses;
     clamped floor; busy / queued supervisor defers without losing the window.
  2. ONE TASK, ALL PEERS — a single queued sweep contains a section for EVERY
     linked peer, including an explicit "(no activity)" section for silent
     agents and a MID-TURN marker for busy ones.
  3. PEER SCOPING — only allowed_peers are queried/included (multi-supervisor
     teams watch disjoint subsets); self never included.
  4. WATERMARKS — advance to the latest message id only after a successful
     enqueue; first-seen peers anchor to "now" instead of dumping history.
  5. CHAR BUDGET — the per-peer slice keeps the most-recent tail and marks
     truncation; total stays bounded.
  6. LEDGER — open delegations involving watched peers (with overdue flag) and
     pending human questions appear; other agents' items are excluded.

Run:  pytest tests/test_supervisor_sweep.py -v
"""

import sys
import time
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import teams_server.agent as agent_mod  # noqa: E402
from teams_server.agent import (  # noqa: E402
    AgentDaemon,
    compose_sweep_sections,
    _age_short,
)
from teams_server.prompts import SUPERVISOR_SWEEP_PROMPT  # noqa: E402


# ---------------------------------------------------------------------------
# Harness: a minimal stand-in for the daemon + a fake monitor_db
# ---------------------------------------------------------------------------

class FakeQueue:
    def __init__(self, pending=0):
        self.pending = pending

    def get_pending_count(self):
        return self.pending


class FakeDB:
    """Per-peer canned messages; records which peers were queried."""

    def __init__(self, msgs_by_peer=None, open_delegations=None):
        self.msgs = msgs_by_peer or {}
        self.dels = open_delegations or []
        self.queried = []
        self.events = []

    def get_new_activity(self, peer, after_id, team_id=None):
        rows = [m for m in self.msgs.get(peer, []) if m["id"] > after_id]
        return {"count": len(rows),
                "tokens": sum(m.get("tokens", 0) for m in rows),
                "max_id": max([m["id"] for m in rows], default=after_id)}

    def get_messages_since(self, peer, after_id, team_id=None, limit=400):
        self.queried.append(peer)
        return [m for m in self.msgs.get(peer, []) if m["id"] > (after_id or 0)]

    def get_open_delegations(self, to_agent=None, from_agent=None,
                             team_id=None, limit=50):
        return list(self.dels)

    def log_event(self, *a, **k):
        self.events.append((a, k))


def make_supervisor(peers, interval_minutes=None, state="idle", pending=0,
                    last_sweep_ago=None):
    """A SimpleNamespace that AgentDaemon's sweep methods can run against."""
    sup = types.SimpleNamespace()
    sup.name = "overseer"
    sup.cfg = {"is_supervisor": True, "allowed_peers": list(peers),
               "team_id": "t1"}
    if interval_minutes is not None:
        sup.cfg["supervisor_interval_minutes"] = interval_minutes
    sup.state = state
    sup._stop_requested = False
    sup.queue = FakeQueue(pending)
    sup._sup_watermark = {}
    sup._last_sweep_ts = time.time() - (last_sweep_ago
                                        if last_sweep_ago is not None else 10 ** 6)
    sup._last_sweep_check_ts = 0.0
    sup._sup_idle_skips = 0
    sup._last_delegation_force_ts = 0.0
    sup.ingested = []
    sup.ingest_task = lambda frm, payload: sup.ingested.append((frm, payload))
    # bind the real methods under test
    for meth in ("_maybe_feed_supervisor", "_supervisor_interval_seconds",
                 "_render_feed_transcript", "_assess_peer_progress",
                 "_peer_runtime_state", "_sweep_ledger",
                 "_oldest_open_delegation_age"):
        setattr(sup, meth, getattr(AgentDaemon, meth).__get__(sup))
    sup._norm_msg = AgentDaemon._norm_msg  # staticmethod — do not bind
    sup._NONACTION_TOOLS = AgentDaemon._NONACTION_TOOLS
    sup._READONLY_TOOLS = AgentDaemon._READONLY_TOOLS
    return sup


@pytest.fixture()
def no_registry(monkeypatch):
    """Peers have no live daemons → state '?', 0 queued; and no human inbox."""
    import teams_server.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_daemon_registry", {}, raising=False)
    monkeypatch.setattr(agent_mod, "_daemon_registry", {}, raising=False)
    monkeypatch.setattr(tools_mod, "get_pending_questions", lambda: [])
    return tools_mod


def msg(i, content, tokens=40, role="assistant"):
    return {"id": i, "timestamp": time.time(), "role": role,
            "content": content, "tokens": tokens}


# ---------------------------------------------------------------------------
# 1. Interval gating
# ---------------------------------------------------------------------------

def test_interval_default_override_and_floor():
    assert make_supervisor([])._supervisor_interval_seconds() == max(
        120, int(agent_mod.SUPERVISOR_SWEEP_INTERVAL_MINUTES * 60))
    assert make_supervisor([], interval_minutes=5)._supervisor_interval_seconds() == 300
    assert make_supervisor([], interval_minutes=0.5)._supervisor_interval_seconds() == 120
    assert make_supervisor([], interval_minutes="bogus")._supervisor_interval_seconds() == max(
        120, int(agent_mod.SUPERVISOR_SWEEP_INTERVAL_MINUTES * 60))


def test_sweep_waits_for_interval(monkeypatch, no_registry):
    db = FakeDB({"a": [msg(1, "did things")]})
    monkeypatch.setattr(agent_mod, "monitor_db", db)
    sup = make_supervisor(["a"], interval_minutes=10, last_sweep_ago=60)
    sup._sup_watermark = {"a": 0}      # known peer with unseen activity
    sup._maybe_feed_supervisor()
    assert sup.ingested == []          # only 60s elapsed of a 600s interval

    sup._last_sweep_ts = time.time() - 700
    sup._maybe_feed_supervisor()
    assert len(sup.ingested) == 1      # now due → exactly one sweep task


def test_busy_or_queued_supervisor_defers_without_losing_window(monkeypatch, no_registry):
    db = FakeDB({"a": [msg(1, "x")]})
    monkeypatch.setattr(agent_mod, "monitor_db", db)
    before = time.time() - 10 ** 5
    sup = make_supervisor(["a"], interval_minutes=2, state="busy")
    sup._last_sweep_ts = before
    sup._maybe_feed_supervisor()
    assert sup.ingested == [] and sup._last_sweep_ts == before  # window intact

    sup = make_supervisor(["a"], interval_minutes=2, pending=3)
    sup._last_sweep_ts = before
    sup._maybe_feed_supervisor()
    assert sup.ingested == [] and sup._last_sweep_ts == before


# ---------------------------------------------------------------------------
# 2 + 3. One task covering all linked peers — and only them
# ---------------------------------------------------------------------------

def test_sweep_covers_all_peers_in_one_task(monkeypatch, no_registry):
    db = FakeDB({
        "alpha": [msg(1, "🛠️ terminal(deploy.sh) → ok"), msg(2, "deployed")],
        "beta": [],                      # silent peer
        "gamma": [msg(3, "🛠️ write_file(notes.md) → ok")],
        "outsider": [msg(9, "should never appear")],
    })
    monkeypatch.setattr(agent_mod, "monitor_db", db)
    sup = make_supervisor(["alpha", "beta", "gamma", "overseer"])  # self filtered
    # watermarks preset → not a first-sight sweep, so all history is in-window
    sup._sup_watermark = {"alpha": 0, "beta": 0, "gamma": 0}
    sup._maybe_feed_supervisor()

    assert len(sup.ingested) == 1
    sender, prompt = sup.ingested[0]
    assert sender == "supervisor-sweep"
    assert "=== alpha — 2 message(s)" in prompt
    assert "=== beta — NO ACTIVITY this window" in prompt
    assert "=== gamma — 1 message(s)" in prompt
    assert "outsider" not in prompt                      # scoping
    assert set(db.queried) == {"alpha", "beta", "gamma"}  # self never queried
    assert "deploy.sh" in prompt                          # transcript present
    assert "PROGRESS SIGNAL" in prompt                    # computed signal present
    # the sweep event was recorded with coverage stats
    ev = [a for a, k in db.events if a[1] == "supervisor_sweep"]
    assert ev, "supervisor_sweep event not logged"


def test_first_seen_peer_anchors_to_now_not_history(monkeypatch, no_registry):
    db = FakeDB({"a": [msg(1, "ancient history"), msg(2, "also old")]})
    monkeypatch.setattr(agent_mod, "monitor_db", db)
    sup = make_supervisor(["a"])
    sup._maybe_feed_supervisor()
    # First sight anchors the watermark and SKIPS — no all-idle LLM turn.
    assert sup.ingested == []
    assert sup._sup_watermark["a"] == 2              # anchored, not dumped
    assert [a for a, k in db.events if a[1] == "supervisor_sweep_skipped"]

    db.msgs["a"].append(msg(3, "fresh work after anchor"))
    sup._last_sweep_ts = time.time() - 10 ** 6
    sup._last_sweep_check_ts = 0.0
    sup._maybe_feed_supervisor()
    prompt = sup.ingested[0][1]
    assert "fresh work after anchor" in prompt
    assert "ancient history" not in prompt           # pre-anchor never dumped
    assert sup._sup_watermark["a"] == 3              # advanced after enqueue


def test_mid_turn_busy_marker(monkeypatch, no_registry):
    import teams_server.tools as tools_mod

    db = FakeDB({"a": [msg(1, "partial work so far")]})
    monkeypatch.setattr(agent_mod, "monitor_db", db)
    busy_peer = types.SimpleNamespace(state="busy", queue=FakeQueue(2))
    monkeypatch.setattr(agent_mod, "_daemon_registry", {"a": busy_peer}, raising=False)
    sup = make_supervisor(["a"])
    sup._sup_watermark = {"a": 0}
    sup._maybe_feed_supervisor()
    _, prompt = sup.ingested[0]
    assert "MID-TURN" in prompt
    assert "partial work so far" in prompt


# ---------------------------------------------------------------------------
# 5. Char budgeting
# ---------------------------------------------------------------------------

def test_sections_budget_keeps_recent_tail():
    big = "\n\n".join(f"[ASSISTANT] step {i}" for i in range(400))
    data = [
        {"peer": "loud", "state": "idle", "pending": 0, "transcript": big,
         "signal": "PROGRESS SIGNAL: ...", "messages": 400, "tokens": 9000},
        {"peer": "quiet", "state": "idle", "pending": 0, "transcript": "",
         "signal": "", "messages": 0, "tokens": 0},
    ]
    out = compose_sweep_sections(data, char_cap=2000, per_peer_floor=500)
    assert "truncated" in out
    assert "step 399" in out          # most-recent kept
    assert "step 1\n" not in out      # oldest dropped
    assert "NO ACTIVITY" in out       # silent peer still present
    assert len(out) < 2000 + 1500     # bounded (cap + headers/markers slack)


def test_sections_floor_prevents_starvation():
    data = [{"peer": f"p{i}", "state": "idle", "pending": 0,
             "transcript": "x" * 5000, "signal": "s", "messages": 1, "tokens": 1}
            for i in range(8)]
    out = compose_sweep_sections(data, char_cap=4000, per_peer_floor=2000)
    # 4000/8 = 500 < floor → every peer still gets up to the 2000-char floor
    for i in range(8):
        assert f"=== p{i} " in out
    assert out.count("truncated") == 8


# ---------------------------------------------------------------------------
# 6. Ledger
# ---------------------------------------------------------------------------

def test_ledger_scopes_delegations_and_questions(monkeypatch, no_registry):
    import teams_server.tools as tools_mod

    now = time.time()
    db = FakeDB(open_delegations=[
        {"msg_id": "abcd1234", "from_agent": "founder", "to_agent": "alpha",
         "summary": "publish the post", "timestamp": now - 3 * 3600},
        {"msg_id": "ffff0000", "from_agent": "other1", "to_agent": "other2",
         "summary": "not ours", "timestamp": now - 3600},
    ])
    monkeypatch.setattr(agent_mod, "monitor_db", db)
    monkeypatch.setattr(tools_mod, "get_pending_questions", lambda: [
        {"agent_name": "alpha", "question": "Need SMTP credentials",
         "status": "pending", "timestamp": now - 1800},
        {"agent_name": "other2", "question": "ignore me",
         "status": "pending", "timestamp": now},
        {"agent_name": "alpha", "question": "already done",
         "status": "answered", "timestamp": now},
    ])

    sup = make_supervisor(["alpha", "beta"])
    ledger = sup._sweep_ledger(["alpha", "beta"],
                               {"alpha": {"state": "idle", "pending": 1},
                                "beta": {"state": "busy", "pending": 0}}, now)
    assert "alpha: IDLE (1 queued)" in ledger and "beta: BUSY" in ledger
    assert "abcd1234" in ledger and "⚠ overdue" in ledger   # 3h-old → flagged
    assert "not ours" not in ledger                          # other agents' item
    assert "Need SMTP credentials" in ledger
    assert "ignore me" not in ledger and "already done" not in ledger


def test_age_short():
    assert _age_short(59) == "0m"
    assert _age_short(47 * 60) == "47m"
    assert _age_short(3 * 3600 + 12 * 60) == "3h12m"


# ---------------------------------------------------------------------------
# 7. Idle-skip — no peer activity → no sweep turn (but never silence-blind)
# ---------------------------------------------------------------------------

def _due(sup):
    """Make both sweep clocks long overdue so the next call probes."""
    sup._last_sweep_ts = time.time() - 10 ** 6
    sup._last_sweep_check_ts = 0.0


def test_idle_sweep_skipped_logs_event_and_preserves_window(monkeypatch, no_registry):
    db = FakeDB({"a": [msg(1, "old, already seen")]})
    monkeypatch.setattr(agent_mod, "monitor_db", db)
    sup = make_supervisor(["a"])
    sup._sup_watermark = {"a": 1}                  # nothing new since
    before = sup._last_sweep_ts
    sup._maybe_feed_supervisor()
    assert sup.ingested == []                      # no LLM turn queued
    assert sup._last_sweep_ts == before            # window anchor intact
    assert sup._sup_watermark == {"a": 1}          # watermark untouched
    assert sup._sup_idle_skips == 1
    skips = [(a, k) for a, k in db.events if a[1] == "supervisor_sweep_skipped"]
    assert len(skips) == 1
    assert skips[0][1]["data"]["consecutive_skips"] == 1
    assert db.queried == []                        # no transcript content pulled


def test_skip_sets_check_clock_no_immediate_recheck(monkeypatch, no_registry):
    db = FakeDB({"a": []})
    monkeypatch.setattr(agent_mod, "monitor_db", db)
    sup = make_supervisor(["a"])
    sup._sup_watermark = {"a": 0}
    sup._maybe_feed_supervisor()
    assert sup._sup_idle_skips == 1
    sup._maybe_feed_supervisor()                   # immediately again
    assert sup._sup_idle_skips == 1                # gated — no second skip yet


def test_idle_backstop_forces_nth_sweep(monkeypatch, no_registry):
    db = FakeDB({"a": []})
    monkeypatch.setattr(agent_mod, "monitor_db", db)
    sup = make_supervisor(["a"])
    sup._sup_watermark = {"a": 0}
    max_skips = agent_mod.SUPERVISOR_SWEEP_MAX_IDLE_SKIPS
    for i in range(max_skips):
        _due(sup)
        sup._maybe_feed_supervisor()
    # Skips 1..N-1 were silent; the Nth idle interval forced a full review.
    assert len(sup.ingested) == 1
    assert "NO ACTIVITY" in sup.ingested[0][1]
    assert sup._sup_idle_skips == 0                # counter reset
    forced = [k["data"].get("forced") for a, k in db.events
              if a[1] == "supervisor_sweep"]
    assert forced == ["idle_backstop"]


def test_stale_delegation_forces_once_per_window(monkeypatch, no_registry):
    now = time.time()
    db = FakeDB({"a": []}, open_delegations=[
        {"msg_id": "deadbeef", "from_agent": "overseer", "to_agent": "a",
         "summary": "stuck task", "timestamp":
             now - agent_mod.SUPERVISOR_SWEEP_FORCE_OPEN_AGE_SECONDS - 60}])
    monkeypatch.setattr(agent_mod, "monitor_db", db)
    sup = make_supervisor(["a"])
    sup._sup_watermark = {"a": 0}
    sup._maybe_feed_supervisor()
    # The overdue delegation forces an early review despite zero activity.
    assert len(sup.ingested) == 1
    forced = [k["data"].get("forced") for a, k in db.events
              if a[1] == "supervisor_sweep"]
    assert forced == ["stale_delegation"]
    # Still idle next interval: the same parked item must NOT force again.
    _due(sup)
    sup._maybe_feed_supervisor()
    assert len(sup.ingested) == 1                  # skipped this time
    assert sup._sup_idle_skips >= 1


def test_activity_after_skips_is_gapless(monkeypatch, no_registry):
    db = FakeDB({"a": [msg(1, "seen already")]})
    monkeypatch.setattr(agent_mod, "monitor_db", db)
    sup = make_supervisor(["a"])
    sup._sup_watermark = {"a": 1}
    sup._maybe_feed_supervisor()                   # idle → skip
    assert sup.ingested == []

    db.msgs["a"].append(msg(2, "woke up and did things"))
    _due(sup)
    sup._maybe_feed_supervisor()
    assert len(sup.ingested) == 1
    assert "woke up and did things" in sup.ingested[0][1]
    assert sup._sup_idle_skips == 0                # reset by the real sweep
    assert sup._sup_watermark["a"] == 2


# ---------------------------------------------------------------------------
# Prompt sanity
# ---------------------------------------------------------------------------

def test_sweep_prompt_placeholders_resolve():
    rendered = SUPERVISOR_SWEEP_PROMPT.format(
        window_minutes=20, peer_count=3, ledger="L", sections="S")
    assert "{" not in rendered.replace("{}", "")  # no unresolved placeholders
    assert "MID-TURN" in rendered and "log_decision" in rendered


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
