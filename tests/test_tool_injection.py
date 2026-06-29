"""Phase 2 — teams tool injection.

`_inject_teams_tools` is the single place that mutates a Hermes AIAgent's
`.tools` / `.valid_tool_names` (replacing ~16 copy-pasted reach-in blocks). These
tests exercise that real code against a fake agent — the prior MockAIAgent in
test_teams.py overrode `_ensure_agent`, so this injection had NO coverage.
"""

import pytest

from teams_server.agent import _inject_teams_tools


class FakeAgent:
    """Minimal stand-in for a built Hermes AIAgent's tool surface."""
    def __init__(self, base_tool_names=()):
        self.tools = [{"function": {"name": n}} for n in base_tool_names]
        self.valid_tool_names = set(base_tool_names)

    def names(self):
        return {t["function"]["name"] for t in self.tools}


# Tools every agent must have regardless of role.
_ALWAYS = {
    "send_peer_message", "ask_human", "request_human_takeover", "log_decision",
    "recall_decisions", "log_action", "close_ledger_entry", "get_self_config",
    "request_config_change", "schedule_wakeup", "cancel_wakeup",
}


def test_worker_gets_coordination_file_and_credentials_tools():
    a = FakeAgent()
    added = _inject_teams_tools(a, is_supervisor=False)
    assert _ALWAYS <= added
    assert "read_files" in added
    assert {"get_credential", "list_credentials"} <= added
    # Worker is not a supervisor → no brake.
    assert "pause_agent" not in added and "resume_agent" not in added
    # tools list and valid_tool_names stay in lockstep.
    assert a.names() == a.valid_tool_names


def test_supervisor_gets_brake_not_worker_tools():
    a = FakeAgent()
    added = _inject_teams_tools(a, is_supervisor=True)
    assert _ALWAYS <= added
    assert {"pause_agent", "resume_agent"} <= added
    # Supervisors do no project work: no file/credential/GUI tools.
    assert "read_files" not in added
    assert "get_credential" not in added and "list_credentials" not in added


def test_gui_browser_tools_only_when_browser_live():
    without = _inject_teams_tools(FakeAgent(), is_supervisor=False)
    assert not any(n.startswith("browser_") for n in without)
    # browser_navigate present (Hermes browser toolset live) → GUI tools attach.
    with_browser = _inject_teams_tools(
        FakeAgent(base_tool_names=["browser_navigate"]), is_supervisor=False)
    assert any(n.startswith("browser_") and n != "browser_navigate"
               for n in with_browser)


def test_disabled_set_skips_optional_but_not_required():
    a = FakeAgent()
    added = _inject_teams_tools(
        a, is_supervisor=False, disabled={"ask_human", "send_peer_message"})
    # Optional tool honored...
    assert "ask_human" not in added
    # ...but send_peer_message is required and ignores the disabled set.
    assert "send_peer_message" in added


def test_idempotent_does_not_duplicate_existing():
    # A tool already on the agent (e.g. a Hermes-native one) is never re-added.
    a = FakeAgent(base_tool_names=["ask_human"])
    added = _inject_teams_tools(a, is_supervisor=False)
    assert "ask_human" not in added  # already present
    assert sum(1 for n in a.names() if n == "ask_human") == 1
