#!/usr/bin/env python3
"""Unit tests for the 2026-06-12 run-analysis fixes (docs/teams-run-analysis-
20260612.md). No LLM, no Hermes.

Covers:
  1. SWEEP-PAYLOAD AGING — old [SUPERVISOR SWEEP …] user payloads are stubbed
     from the marker onward (any prefix preserved); newest K kept; idempotent.
  2. DISMISSIBLE LEDGER — close_delegation_manual closes by unambiguous
     msg_id prefix; ambiguous/missing/short prefixes error; participant gate.
  3. TURN-OUTPUT GUARDS — missing-RESULT handoff fires once per delegated
     task that ends without send_peer_message; notification-only turns
     (heartbeat / turn-guard / RESULT-ack) never get the text-only nudge;
     plain investigate-then-summarize turns still do.
  4. READ_FILES — several files in one call with per-file headers; missing
     files report inline; count cap enforced.
  5. DIRECTIVES — set_directive arms/validates; expired directive clears in
     the heartbeat and the directive prompt formats.

Run:  pytest tests/test_run_fixes.py -v
"""

import sys
import time
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import teams_server.agent as agent_mod  # noqa: E402
import teams_server.tools as tools_mod  # noqa: E402
from teams_server.agent import AgentDaemon  # noqa: E402
from teams_server.monitoring import MonitoringDB  # noqa: E402
from teams_server.prompts import (  # noqa: E402
    DIRECTIVE_HEARTBEAT_PROMPT,
    MISSING_RESULT_NUDGE,
    SWEEP_PAYLOAD_ELIDED_STUB,
    SWEEP_PAYLOAD_MARKER,
    TEXT_ONLY_TURN_NUDGE,
    age_stale_sweep_payloads,
)


# ---------------------------------------------------------------------------
# 1. Sweep-payload aging
# ---------------------------------------------------------------------------

def _sweep_msg(i, payload_chars=2000):
    return {"role": "user",
            "content": f"<<<LIVE-CONTEXT>>> ctx{i}\n{SWEEP_PAYLOAD_MARKER} — "
                       f"window {i}]\n" + ("x" * payload_chars)}


def test_old_sweep_payloads_stubbed_newest_kept():
    msgs = [_sweep_msg(i) for i in range(5)]
    msgs.insert(2, {"role": "assistant", "content": "verdict A"})
    aged = age_stale_sweep_payloads(msgs, keep_recent=2, min_chars=500)
    payloads = [m for m in aged if m.get("role") == "user"]
    stubbed = [m for m in payloads if m["content"].endswith(SWEEP_PAYLOAD_ELIDED_STUB)]
    intact = [m for m in payloads if SWEEP_PAYLOAD_MARKER in m["content"]]
    assert len(stubbed) == 3 and len(intact) == 2
    # Prefix before the marker (live context — handled elsewhere) is preserved.
    assert stubbed[0]["content"].startswith("<<<LIVE-CONTEXT>>> ctx0")
    # Verdicts untouched.
    assert any(m.get("content") == "verdict A" for m in aged)


def test_sweep_aging_idempotent_and_small_skipped():
    msgs = [_sweep_msg(0), _sweep_msg(1, payload_chars=100),
            _sweep_msg(2), _sweep_msg(3), _sweep_msg(4)]
    once = age_stale_sweep_payloads(msgs, keep_recent=2, min_chars=500)
    twice = age_stale_sweep_payloads(once, keep_recent=2, min_chars=500)
    # Stubbed payloads no longer carry the marker → second pass keeps the
    # NEWEST 2 marker-bearing messages and finds nothing else big enough.
    assert twice == once
    # The small payload (idx 1) was below min_chars → left intact.
    assert SWEEP_PAYLOAD_MARKER in once[1]["content"]


def test_sweep_aging_no_sweeps_returns_same_object():
    msgs = [{"role": "user", "content": "plain"},
            {"role": "assistant", "content": "reply"}]
    assert age_stale_sweep_payloads(msgs, keep_recent=2, min_chars=500) is msgs


# ---------------------------------------------------------------------------
# 2. Dismissible ledger entries
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    return MonitoringDB(tmp_path / "mon.db")


def test_close_by_prefix_and_event(db):
    db.open_delegation("aabbccdd-1111", "founder", "product", "TASK",
                       summary="verify activation", team_id="t1")
    res = db.close_delegation_manual("aabbccdd", by_agent="founder",
                                     reason="done via other chain", team_id="t1")
    assert res["success"] and res["msg_id"] == "aabbccdd-1111"
    assert db.get_open_delegations(team_id="t1") == []
    evs = db.get_events_since("ledger_entry_closed", since_ts=0)
    assert evs, "closure must be auditable"


def test_close_prefix_too_short_ambiguous_and_missing(db):
    db.open_delegation("aabb11-x", "a", "b", "TASK", team_id="t1")
    db.open_delegation("aabb22-y", "a", "c", "TASK", team_id="t1")
    assert not db.close_delegation_manual("aabb", by_agent="a")["success"]
    res = db.close_delegation_manual("aabb11", by_agent="a", team_id="t1")
    assert res["success"]
    assert not db.close_delegation_manual("zzzzzz", by_agent="a",
                                          team_id="t1")["success"]


def test_close_participant_gate(db):
    db.open_delegation("ffee9988-1", "founder", "product", "TASK", team_id="t1")
    res = db.close_delegation_manual("ffee9988", by_agent="growth",
                                     team_id="t1", require_participant=True)
    assert not res["success"] and "supervisor" in res["error"]
    # Still open; a participant may close it.
    res2 = db.close_delegation_manual("ffee9988", by_agent="product",
                                      team_id="t1", require_participant=True)
    assert res2["success"]


def test_close_ledger_entry_handler_requires_reason(db, monkeypatch):
    import json
    monkeypatch.setattr(tools_mod, "monitor_db", db)
    out = json.loads(tools_mod._close_ledger_entry_handler(
        {"msg_id": "aabbccdd", "reason": "  "},
        task_id="agent_name:founder"))
    assert not out["success"] and "reason" in out["error"]


# ---------------------------------------------------------------------------
# 3. Turn-output guards
# ---------------------------------------------------------------------------

class GuardFake:
    """Minimal stand-in exposing exactly what _apply_turn_output_guards uses."""

    _READONLY_TOOLS = AgentDaemon._READONLY_TOOLS
    _apply_turn_output_guards = AgentDaemon._apply_turn_output_guards

    def __init__(self, is_supervisor=False):
        self.name = "w1"
        self.cfg = {"is_supervisor": is_supervisor}
        self._stop_requested = False
        self._text_only_nudged = False
        self._result_nudged_ids = set()
        self.ingested = []

    def ingest_task(self, from_agent, payload):
        self.ingested.append((from_agent, payload))


class NullDB:
    def log_event(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def _null_monitor(monkeypatch):
    monkeypatch.setattr(agent_mod, "monitor_db", NullDB())


def _call(name):
    return {"function": {"name": name}}


def _turn(*, tools=(), final="done. " + "x" * 60):
    msgs = [{"role": "assistant", "tool_calls": [_call(n)], "content": ""}
            for n in tools]
    msgs.append({"role": "assistant", "content": final})
    return msgs


TASK_PAYLOAD = "[TASK · id=af917491 · from founder2 — when done, reply…] fix it"


def test_missing_result_nudge_fires_once_per_task():
    f = GuardFake()
    tasks = [{"id": "q1", "from_agent": "founder2", "payload": TASK_PAYLOAD}]
    f._apply_turn_output_guards(tasks, _turn(tools=("read_file", "patch")))
    assert len(f.ingested) == 1
    src, payload = f.ingested[0]
    assert src == "turn-guard" and "af917491" in payload and "founder2" in payload
    # Same task again (e.g. a later turn also without a RESULT) → no second
    # missing-RESULT nudge (the text-only guard may still apply on its own).
    f._apply_turn_output_guards(tasks, _turn(tools=("read_file",)))
    assert sum("WITHOUT A RESULT" in p for _, p in f.ingested) == 1


def test_no_nudge_when_result_was_sent():
    f = GuardFake()
    tasks = [{"id": "q1", "from_agent": "founder2", "payload": TASK_PAYLOAD}]
    f._apply_turn_output_guards(
        tasks, _turn(tools=("patch", "send_peer_message")))
    assert f.ingested == []


def test_notification_turns_exempt_from_text_only_guard():
    for payload, src in (
        ("[IDLE HEARTBEAT — automated check-in…]", "autonomous"),
        (TEXT_ONLY_TURN_NUDGE, "turn-guard"),
        ("[RESULT · from devops2 · re a78f2583] Deploy complete", "devops2"),
        ("[STATUS · from growth2] draft progressing", "growth2"),
    ):
        f = GuardFake()
        tasks = [{"id": "q1", "from_agent": src, "payload": payload}]
        # Read-only turn ending in a summary — the old guard's false-positive.
        f._apply_turn_output_guards(tasks, _turn(tools=("read_file",)))
        assert f.ingested == [], f"false nudge on {payload[:20]!r}"


def test_plain_museful_turn_still_nudged_and_acting_resets():
    f = GuardFake()
    tasks = [{"id": "q1", "from_agent": "human", "payload": "look into signups"}]
    f._apply_turn_output_guards(tasks, _turn(tools=("read_file",)))
    assert f.ingested and f.ingested[0][1] == TEXT_ONLY_TURN_NUDGE
    # An acting turn clears the once-per-occurrence latch.
    f._apply_turn_output_guards(tasks, _turn(tools=("terminal",)))
    assert f._text_only_nudged is False


def test_missing_result_takes_precedence_over_text_only():
    f = GuardFake()
    tasks = [{"id": "q1", "from_agent": "founder2", "payload": TASK_PAYLOAD}]
    f._apply_turn_output_guards(tasks, _turn(tools=("read_file",)))
    nudges = [p for _, p in f.ingested]
    assert len(nudges) == 1 and "WITHOUT A RESULT" in nudges[0]


def test_supervisor_and_truncated_empty_final():
    sup = GuardFake(is_supervisor=True)
    tasks = [{"id": "q1", "from_agent": "founder2", "payload": TASK_PAYLOAD}]
    sup._apply_turn_output_guards(tasks, _turn(tools=("read_file",)))
    assert sup.ingested == []
    # Iteration-budget truncation: last assistant message still has tool_calls,
    # no final text at all — the silent-stall shape. Nudge fires.
    f = GuardFake()
    msgs = [{"role": "assistant", "tool_calls": [_call("patch")], "content": ""}]
    f._apply_turn_output_guards(tasks, msgs)
    assert len(f.ingested) == 1 and "WITHOUT A RESULT" in f.ingested[0][1]


# ---------------------------------------------------------------------------
# 4. read_files
# ---------------------------------------------------------------------------

def test_read_files_batch_and_missing(tmp_path, monkeypatch):
    import teams_server.config as cfg_mod
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")
    monkeypatch.setattr(cfg_mod, "load_agents_config",
                        lambda: {"agents": {"w1": {"team_id": "t1"}}})
    monkeypatch.setattr(cfg_mod, "_derive_workspace_path",
                        lambda team, name: tmp_path)
    out = tools_mod._read_files_handler(
        {"paths": ["a.txt", "b.txt", "missing.txt"]},
        task_id="agent_name:w1")
    assert "=== a.txt" in out and "alpha" in out
    assert "=== b.txt" in out and "beta" in out
    assert "[unreadable:" in out


def test_read_files_count_cap():
    import json
    out = json.loads(tools_mod._read_files_handler(
        {"paths": [f"f{i}" for i in range(9)]}, task_id="agent_name:w1"))
    assert "error" in out and "8" in out["error"]


# ---------------------------------------------------------------------------
# 5. Directives
# ---------------------------------------------------------------------------

def make_directive_daemon():
    d = types.SimpleNamespace()
    d.name = "founder1"
    d.cfg = {}
    d._directive = None
    d.set_directive = types.MethodType(AgentDaemon.set_directive, d)
    return d


def test_set_directive_arms_and_validates():
    d = make_directive_daemon()
    d.set_directive("push to first paying user", 240, from_agent="human")
    assert d._directive and d._directive["from_agent"] == "human"
    assert d._directive["until_ts"] > time.time() + 200 * 60
    d2 = make_directive_daemon()
    d2.set_directive("x", 0)
    d2.set_directive("x", -5)
    d2.set_directive("x", "garbage")
    assert d2._directive is None


def test_directive_prompt_formats():
    p = DIRECTIVE_HEARTBEAT_PROMPT.format(
        remaining="2h05m", time="2026-06-12 03:00",
        from_agent="human", directive="reach 1K MRR")
    assert "2h05m" in p and "reach 1K MRR" in p and "queue does NOT" in p


def test_missing_result_nudge_template():
    p = MISSING_RESULT_NUDGE.format(task_id="abc123", from_agent="founder2")
    assert "abc123" in p and 'to_agent="founder2"' in p


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
