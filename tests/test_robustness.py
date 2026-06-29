#!/usr/bin/env python3
"""Unit tests for the robustness mechanisms added for long-run stability.

Covers, with no LLM and no Hermes required:
  1. HISTORY HYGIENE   — strip_stale_live_context removes expired live-context
                         snapshots (sentinel + legacy formats) from replayed
                         turns without ever eating a task body.
  2. REPETITION GUARD  — detect_repeated_signature flags the same tool call
                         recurring across turns; within-turn retries don't count.
  3. QUEUE DEDUP       — identical pending payloads from the same sender
                         collapse to one task.
  4. PASSIVE DELIVERY  — STATUS/FYI events are queryable per-recipient past a
                         watermark, with full text, excluding waking kinds.
  5. CLOSED-DELEGATION LEDGER — answered TASK/QUESTIONs surface for the
                         RECENTLY COMPLETED block.
  6. HEARTBEAT BACKOFF — no-op heartbeats double the idle interval, capped;
                         real activity resets it.
  7. MISSION NEUTRALITY — no hardcoded business goal survives in framework
                         prompts (the brief, not the prompt, owns the mission).
  8. LIVE CONTEXT      — sentinel-wrapped, with the new ledger/human sections.

Run:  pytest tests/test_robustness.py -v
  or: python3 tests/test_robustness.py
"""

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from teams_server.prompts import (  # noqa: E402
    AUTONOMOUS_HEARTBEAT_PROMPT,
    COMMON_SOUL_TEMPLATE,
    CRON_WAKEUP_PROMPT,
    LIVE_CONTEXT_BEGIN,
    LIVE_CONTEXT_END,
    SELF_LOOP_NUDGE,
    STALE_CONTEXT_NOTE,
    SUPERVISOR_DEFAULT_SOUL,
    SUPERVISOR_SWEEP_PROMPT,
    TEXT_ONLY_TURN_NUDGE,
    compose_agent_soul,
    strip_stale_live_context,
)
from teams_server.monitoring import MonitoringDB  # noqa: E402
from teams_server.queue import TaskQueue  # noqa: E402


# ---------------------------------------------------------------------------
# 1. History hygiene
# ---------------------------------------------------------------------------
def _sentinel_msg(body: str = "do thing") -> str:
    return (
        f"{LIVE_CONTEXT_BEGIN}\n--- LIVE TEAM CONTEXT ---\nbrief v1\nledger v1\n"
        f"{LIVE_CONTEXT_END}\n\nYou have 1 new message(s) to process:\n\n"
        f"--- [1] from peer ---\n{body}\n"
    )


def test_strip_sentinel_block():
    out = strip_stale_live_context(_sentinel_msg())
    assert STALE_CONTEXT_NOTE in out
    assert "brief v1" not in out and "ledger v1" not in out
    assert "do thing" in out  # the actual task survives


def test_strip_is_idempotent():
    once = strip_stale_live_context(_sentinel_msg())
    assert strip_stale_live_context(once) == once


def test_strip_legacy_format():
    legacy = (
        "--- LIVE TEAM CONTEXT (auto-refreshed each turn) ---\nold brief\n"
        "DECISION LOG\nstuff\n\nYou have 2 new message(s) to process:\ntask body"
    )
    out = strip_stale_live_context(legacy)
    assert "old brief" not in out
    assert "task body" in out
    assert STALE_CONTEXT_NOTE in out


def test_strip_truncated_sentinel_falls_back_to_batch_anchor():
    truncated = (
        f"{LIVE_CONTEXT_BEGIN}\nbrief that got truncated…\n\n"
        "You have 1 new message(s) to process:\nthe task"
    )
    out = strip_stale_live_context(truncated)
    assert "brief that got truncated" not in out
    assert "the task" in out


def test_strip_never_eats_plain_messages():
    plain = "Just a normal task payload mentioning a brief and a ledger."
    assert strip_stale_live_context(plain) == plain
    # legacy header but NO batch anchor -> untouched (never risk a task body)
    odd = "--- LIVE TEAM CONTEXT (auto-refreshed each turn) ---\nonly context, no batch"
    assert strip_stale_live_context(odd) == odd


# ---------------------------------------------------------------------------
# 2. Repetition guard
# ---------------------------------------------------------------------------
def test_detect_repeated_signature():
    from teams_server.agent import detect_repeated_signature

    hist = [["read(a.txt)"], ["read(a.txt)", "grep(x)"], ["read(a.txt)"]]
    assert detect_repeated_signature(hist, repeats=3, window=5) == "read(a.txt)"
    assert detect_repeated_signature([["a(1)"], ["b(2)"], ["c(3)"]], 3, 5) is None
    # within-turn repetition is NOT a cross-turn loop
    assert detect_repeated_signature([["a(1)", "a(1)", "a(1)"]], 3, 5) is None
    # window slides: old repeats outside the window don't fire
    hist = [["a(1)"], ["a(1)"], ["b(2)"], ["c(3)"], ["d(4)"], ["e(5)"]]
    assert detect_repeated_signature(hist, repeats=3, window=5) is None


def test_turn_tool_signatures_normalization():
    from teams_server.agent import _turn_tool_signatures

    msgs = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "read_file", "arguments": '{"path":  "a.txt"}'}},
            {"function": {"name": "grep", "arguments": {"q": "x", "a": 1}}},
        ]},
        {"role": "tool", "content": "ignored"},
        {"role": "assistant", "content": "no tools"},
    ]
    sigs = _turn_tool_signatures(msgs)
    assert len(sigs) == 2
    assert sigs[0] == 'read_file({"path": "a.txt"})'
    # dict args serialize with sorted keys -> stable across orderings
    assert sigs[1] == 'grep({"a": 1, "q": "x"})'


# ---------------------------------------------------------------------------
# 3. Queue dedup
# ---------------------------------------------------------------------------
def test_queue_dedup_identical_pending(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    a = q.enqueue("peer", "same payload")
    b = q.enqueue("peer", "same payload")
    assert a == b
    assert q.get_pending_count() == 1
    # different sender or payload -> distinct tasks
    c = q.enqueue("other", "same payload")
    d = q.enqueue("peer", "different payload")
    assert len({a, c, d}) == 3
    assert q.get_pending_count() == 3
    # once claimed (processing), an identical re-send is a NEW task again
    q.drain_pending(limit=10)
    e = q.enqueue("peer", "same payload")
    assert e != a
    assert q.get_pending_count() == 1


# ---------------------------------------------------------------------------
# 4/5. Passive delivery + closed-delegation ledger
# ---------------------------------------------------------------------------
def test_passive_messages_and_watermark(tmp_path):
    db = MonitoringDB(tmp_path / "mon.db")
    wm = db.get_latest_event_id()
    db.log_event("alice", "message_sent", from_agent="alice", to_agent="bob",
                 data={"kind": "STATUS", "waking": False,
                       "message_preview": "deploy went fine"[:120],
                       "message_full": "deploy went fine — v2 live, 200 OK"})
    db.log_event("alice", "message_sent", from_agent="alice", to_agent="bob",
                 data={"kind": "TASK", "waking": True,
                       "message_preview": "do the thing"})
    db.log_event("alice", "message_sent", from_agent="alice", to_agent="carol",
                 data={"kind": "FYI", "waking": False, "message_preview": "x"})

    res = db.get_passive_messages_for("bob", wm)
    msgs = res["messages"]
    assert len(msgs) == 1  # waking TASK excluded; carol's FYI not bob's
    assert msgs[0]["from_agent"] == "alice"
    assert msgs[0]["text"] == "deploy went fine — v2 live, 200 OK"
    assert res["max_id"] > wm
    # watermark advanced -> nothing redelivered
    res2 = db.get_passive_messages_for("bob", res["max_id"])
    assert res2["messages"] == []


def test_recently_closed_delegations(tmp_path):
    db = MonitoringDB(tmp_path / "mon.db")
    db.open_delegation("m1", "lead", "worker", "TASK", summary="build the page", team_id="t1")
    db.open_delegation("m2", "lead", "worker", "TASK", summary="never answered", team_id="t1")
    assert db.answer_delegation("m1", by_agent="worker") is True
    closed = db.get_recent_closed_delegations(team_id="t1")
    assert [d["msg_id"] for d in closed] == ["m1"]
    assert closed[0]["summary"] == "build the page"
    # still-open one remains in the open view
    open_ = db.get_open_delegations(team_id="t1")
    assert [d["msg_id"] for d in open_] == ["m2"]


# ---------------------------------------------------------------------------
# 6. Heartbeat backoff
# ---------------------------------------------------------------------------
def test_heartbeat_backoff_doubles_and_caps():
    from teams_server.agent import AgentDaemon
    from teams_server.config import HEARTBEAT_BACKOFF_MAX_DOUBLINGS

    base = 600.0
    assert AgentDaemon.effective_heartbeat_interval(base, 0) == 600.0
    assert AgentDaemon.effective_heartbeat_interval(base, 1) == 1200.0
    assert AgentDaemon.effective_heartbeat_interval(base, 2) == 2400.0
    capped = AgentDaemon.effective_heartbeat_interval(base, 99)
    assert capped == base * 2 ** HEARTBEAT_BACKOFF_MAX_DOUBLINGS
    # reset path is just misses=0
    assert AgentDaemon.effective_heartbeat_interval(base, 0) == base


# ---------------------------------------------------------------------------
# 7. Mission neutrality of framework prompts
# ---------------------------------------------------------------------------
def test_framework_prompts_are_mission_neutral():
    banned = ("paying customer", "revenue", "north star", "marketing email",
              "linkedin", "customer-facing")
    for blob in (COMMON_SOUL_TEMPLATE, AUTONOMOUS_HEARTBEAT_PROMPT,
                 CRON_WAKEUP_PROMPT, SUPERVISOR_SWEEP_PROMPT,
                 SUPERVISOR_DEFAULT_SOUL, TEXT_ONLY_TURN_NUDGE, SELF_LOOP_NUDGE):
        low = blob.lower()
        for phrase in banned:
            assert phrase not in low, f"mission-specific phrase {phrase!r} leaked into a framework prompt"


def test_soul_compose_substitutes_placeholders():
    cfg = {"name": "tester", "agent_id": "tester", "team_id": "t1",
           "allowed_peers": ["lead"]}
    full = {"agents": {
        "tester": {"team_id": "t1", "allowed_peers": ["lead"]},
        "lead": {"team_id": "t1", "role_soul": "You are the lead."},
    }}
    soul = compose_agent_soul(cfg, full, include_role=False)
    for ph in ("{agent_name}", "{team_id}", "{allowed_peers_list}",
               "{sweep_interval}", "{project_dir}"):
        assert ph not in soul, f"unsubstituted placeholder {ph}"
    assert "tester" in soul and "lead" in soul


# ---------------------------------------------------------------------------
# 8. Live context: sentinels + new sections
# ---------------------------------------------------------------------------
def test_compose_live_context_sentinels_and_sections(tmp_path, monkeypatch):
    import teams_server.monitoring as monitoring
    import teams_server.prompts as prompts
    import teams_server.tools as tools

    db = MonitoringDB(tmp_path / "mon.db")
    db.open_delegation("o1", "lead", "tester", "TASK", summary="open item", team_id="t1")
    db.open_delegation("c1", "tester", "lead", "TASK", summary="finished item", team_id="t1")
    db.answer_delegation("c1")
    monkeypatch.setattr(monitoring, "monitor_db", db)
    monkeypatch.setattr(
        tools, "get_pending_questions",
        lambda: [{"agent_name": "tester", "status": "pending",
                  "question": "Need SMTP credentials", "timestamp": time.time() - 3600}],
    )
    monkeypatch.setattr(prompts, "_get_project_dir", lambda team_id, cfg=None: tmp_path / "proj")
    monkeypatch.setattr(prompts, "_get_team_workspace_path", lambda team_id: tmp_path / "ws")

    full = {"agents": {"tester": {"team_id": "t1", "allowed_peers": []}}}
    ctx = prompts.compose_live_context("t1", "tester", full)

    assert ctx.startswith(LIVE_CONTEXT_BEGIN) and ctx.rstrip().endswith(LIVE_CONTEXT_END)
    assert "RECENTLY COMPLETED" in ctx and "finished item" in ctx
    assert "WAITING ON HUMAN" in ctx and "Need SMTP credentials" in ctx
    assert "OUTSTANDING WORK" in ctx and "open item" in ctx
    # a freshly-composed block must strip cleanly when replayed as history
    replay = f"{ctx}\n\nYou have 1 new message(s) to process:\nfollow-up task"
    stripped = strip_stale_live_context(replay)
    assert "open item" not in stripped and "follow-up task" in stripped


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
