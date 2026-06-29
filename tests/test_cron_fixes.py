#!/usr/bin/env python3
"""Unit tests for the cron anti-loop fixes (2026-06-13).

A self-scheduled 1.7k-char step-by-step cron froze founder2 to June-10 state
every morning. Fixes under test:
  1. INSTRUCTION CAPS — agent-created cron instructions capped (goal-level),
     human-created get a looser cap; the error points at the runbook-file
     pattern. update path (dashboard) uses the human cap.
  2. WAKE-UP FRAMING — CRON_WAKEUP_PROMPT formats with scheduled_ago and
     frames the instruction as intent, not a frozen script.

Run:  pytest tests/test_cron_fixes.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import teams_server.config as cfg_mod  # noqa: E402
from teams_server.config import (  # noqa: E402
    MAX_CRON_INSTRUCTION_CHARS_AGENT,
    MAX_CRON_INSTRUCTION_CHARS_HUMAN,
    _validate_cron_instruction,
    add_agent_cron,
    update_agent_cron,
)
from teams_server.prompts import CRON_WAKEUP_PROMPT  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Instruction caps
# ---------------------------------------------------------------------------

def test_agent_instruction_over_cap_rejected_with_runbook_hint():
    long_instr = "x" * (MAX_CRON_INSTRUCTION_CHARS_AGENT + 1)
    with pytest.raises(ValueError) as ei:
        _validate_cron_instruction(long_instr, "agent")
    msg = str(ei.value)
    assert "GOAL-level" in msg
    assert "runbook" in msg
    assert str(MAX_CRON_INSTRUCTION_CHARS_AGENT) in msg


def test_agent_instruction_at_cap_accepted():
    instr = "x" * MAX_CRON_INSTRUCTION_CHARS_AGENT
    assert _validate_cron_instruction(instr, "agent") == instr


def test_human_cap_is_looser_than_agent_cap():
    assert MAX_CRON_INSTRUCTION_CHARS_HUMAN > MAX_CRON_INSTRUCTION_CHARS_AGENT
    instr = "x" * (MAX_CRON_INSTRUCTION_CHARS_AGENT + 50)
    # Same length: rejected for an agent, fine for a human.
    with pytest.raises(ValueError):
        _validate_cron_instruction(instr, "agent")
    assert _validate_cron_instruction(instr, "human") == instr
    with pytest.raises(ValueError):
        _validate_cron_instruction("x" * (MAX_CRON_INSTRUCTION_CHARS_HUMAN + 1), "human")


def test_empty_instruction_still_rejected():
    with pytest.raises(ValueError, match="needs an instruction"):
        _validate_cron_instruction("   ", "agent")


@pytest.fixture()
def fake_config(monkeypatch):
    """Route add/update through an in-memory config so no file is written."""
    full = {"agents": {"founder": {"crons": []}}}
    monkeypatch.setattr(cfg_mod, "load_agents_config", lambda: full)
    monkeypatch.setattr(cfg_mod, "_save_full_config", lambda c: None)
    return full


def test_add_agent_cron_enforces_agent_cap(fake_config):
    with pytest.raises(ValueError, match="GOAL-level"):
        add_agent_cron(
            fake_config, "founder", "0 9 * * *",
            "s" * (MAX_CRON_INSTRUCTION_CHARS_AGENT + 1), created_by="agent",
        )
    assert fake_config["agents"]["founder"]["crons"] == []


def test_add_agent_cron_human_allows_longer(fake_config):
    instr = "s" * (MAX_CRON_INSTRUCTION_CHARS_AGENT + 100)
    entry = add_agent_cron(fake_config, "founder", "0 9 * * *", instr, created_by="human")
    assert entry["instruction"] == instr
    assert fake_config["agents"]["founder"]["crons"][0]["id"] == entry["id"]


def test_update_agent_cron_uses_human_cap(fake_config):
    entry = add_agent_cron(fake_config, "founder", "0 9 * * *", "short", created_by="agent")
    longer = "u" * (MAX_CRON_INSTRUCTION_CHARS_AGENT + 100)
    updated = update_agent_cron(fake_config, "founder", entry["id"], {"instruction": longer})
    assert updated["instruction"] == longer
    with pytest.raises(ValueError):
        update_agent_cron(
            fake_config, "founder", entry["id"],
            {"instruction": "u" * (MAX_CRON_INSTRUCTION_CHARS_HUMAN + 1)},
        )


# ---------------------------------------------------------------------------
# 2. Wake-up prompt framing
# ---------------------------------------------------------------------------

def test_cron_wakeup_prompt_formats_with_scheduled_ago():
    p = CRON_WAKEUP_PROMPT.format(
        schedule="0 9 * * *",
        time="2026-06-13 09:00",
        scheduled_ago="2d",
        instruction="Daily check-in — follow docs/daily-checkin-runbook.md",
    )
    assert "2d ago" in p
    assert "not a frozen script" in p
    assert "docs/daily-checkin-runbook.md" in p
    # Anti-autopilot affordances are named explicitly.
    assert "cancel_wakeup" in p and "log_decision" in p
