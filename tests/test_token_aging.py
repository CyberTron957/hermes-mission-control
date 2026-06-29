#!/usr/bin/env python3
"""Unit tests for tool-result aging at history replay
(prompts.age_stale_tool_results). Pure function — no LLM, no Hermes.

The aging pass replaces OLD, LARGE role="tool" message contents with one-line
stubs while protecting a recent working set, quantizing the cutoff so the
request prefix stays byte-stable between steps, and never touching what the
agent itself said or decided.

Run:  pytest tests/test_token_aging.py -v
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from teams_server.prompts import (  # noqa: E402
    TOOL_RESULT_ELIDED_PREFIX,
    age_stale_tool_results,
)


def _tool(content, name="browser_snapshot"):
    return {"role": "tool", "tool_call_id": "tc-1", "name": name, "content": content}


def _hist(n_old_tools=30, tail=10, big="X" * 2000):
    """n_old_tools large tool results, then `tail` small recent messages."""
    msgs = []
    for i in range(n_old_tools):
        msgs.append({"role": "assistant", "content": f"step {i}"})
        msgs.append(_tool(big))
    for i in range(tail):
        msgs.append({"role": "user", "content": f"recent {i}"})
    return msgs


def test_old_large_tool_results_stubbed_name_preserved():
    msgs = _hist()
    out = age_stale_tool_results(msgs, keep_recent=10, min_chars=600, quantum=20)
    aged = [m for m in out if str(m.get("content", "")).startswith(TOOL_RESULT_ELIDED_PREFIX)]
    assert aged, "expected at least one stubbed tool result"
    stub = aged[0]
    assert stub["role"] == "tool"
    assert stub["tool_call_id"] == "tc-1"          # pairing survives
    assert stub["name"] == "browser_snapshot"
    assert "browser_snapshot" in stub["content"]   # model can tell what was there
    assert "2000 chars" in stub["content"]
    assert "re-run" in stub["content"]


def test_protected_tail_untouched():
    msgs = _hist(n_old_tools=10, tail=0)
    # Put one big tool result inside the protected window.
    msgs.append(_tool("Y" * 5000, name="read_file"))
    out = age_stale_tool_results(msgs, keep_recent=5, min_chars=600, quantum=4)
    assert out[-1]["content"] == "Y" * 5000


def test_small_results_and_other_roles_untouched():
    msgs = [
        {"role": "system", "content": "S" * 5000},
        {"role": "user", "content": "U" * 5000},
        {"role": "assistant", "content": "A" * 5000},
        _tool("short"),
    ] * 10
    out = age_stale_tool_results(msgs, keep_recent=4, min_chars=600, quantum=4)
    for m in out:
        assert not str(m.get("content", "")).startswith(TOOL_RESULT_ELIDED_PREFIX)
    # Nothing changed -> the original list object is returned (cheap no-op).
    assert out is msgs


def test_dict_content_skipped_safely():
    msgs = [
        {"role": "tool", "content": [{"type": "text", "text": "Z" * 5000}]}
        for _ in range(30)
    ] + [{"role": "user", "content": "tail"}]
    out = age_stale_tool_results(msgs, keep_recent=1, min_chars=600, quantum=10)
    assert out[0]["content"] == [{"type": "text", "text": "Z" * 5000}]


def test_idempotent():
    msgs = _hist()
    once = age_stale_tool_results(msgs, keep_recent=10, min_chars=600, quantum=20)
    twice = age_stale_tool_results(once, keep_recent=10, min_chars=600, quantum=20)
    assert twice is once  # second pass changes nothing, returns same object


def test_quantized_boundary_stable_across_growth():
    """Appending fewer than `quantum` messages must NOT move the cutoff —
    the already-aged prefix stays byte-identical (prompt-cache friendly)."""
    base = _hist(n_old_tools=30, tail=10)  # 70 messages
    out1 = age_stale_tool_results(list(base), keep_recent=10, min_chars=600, quantum=20)
    # Simulate the next few turns: history grows by 6 messages (< quantum).
    grown = list(base) + [
        {"role": "assistant", "content": f"new {i}"} for i in range(6)
    ]
    out2 = age_stale_tool_results(grown, keep_recent=10, min_chars=600, quantum=20)
    # cutoff1 = (70-10)//20*20 = 60 ; cutoff2 = (76-10)//20*20 = 60 -> same
    assert [m["content"] for m in out2[:60]] == [m["content"] for m in out1[:60]]


def test_short_history_returned_unchanged():
    msgs = _hist(n_old_tools=3, tail=2)
    out = age_stale_tool_results(msgs, keep_recent=40, min_chars=600, quantum=20)
    assert out is msgs
