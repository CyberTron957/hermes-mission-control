"""Configuration constants and agent config management."""

import json
import logging
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger("swarm.config")

# ---------------------------------------------------------------------------
# Paths (relative to project root)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = PROJECT_ROOT / "data"
AGENTS_CONFIG_PATH = WORKSPACE_ROOT / "agents_config.json"
MONITORING_DB = WORKSPACE_ROOT / "monitoring.db"
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"

# ---------------------------------------------------------------------------
# Network / Runtime
# ---------------------------------------------------------------------------
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
LITELLM_API_BASE = f"http://{SERVER_HOST}:4000/v1"
SWEEP_INTERVAL_SECONDS = 10

# ---------------------------------------------------------------------------
# Agent defaults
# ---------------------------------------------------------------------------
DEFAULT_SOUL_TEMPLATE = (
    "You are the {agent_display_name}.\n"
    "You operate autonomously. Process tasks with your available tools without asking for permission.\n"
    "Only call 'ask_human' when you are genuinely stuck, the instructions are ambiguous, or a task explicitly requires human judgment (e.g., approvals, subjective choices).\n"
    "To send a message, task, result, or response to another agent, use the 'send_peer_message' tool.\n"
    "After calling 'send_peer_message' or 'ask_human', stop calling tools and end your turn."
)

DEFAULT_AGENTS_RAW: Dict[str, Dict[str, Any]] = {
    "editor": {
        "name": "Editor Agent",
        "session_id": "editor-master-session-v1",
        "workspace": "editor",
        "port": 8100,
        "peer_name": "researcher",
        "peer_port": 8101,
        "soul": DEFAULT_SOUL_TEMPLATE.format(agent_display_name="Editor Agent"),
    },
    "researcher": {
        "name": "Researcher Agent",
        "session_id": "researcher-master-session-v1",
        "workspace": "researcher",
        "port": 8101,
        "peer_name": "editor",
        "peer_port": 8100,
        "soul": DEFAULT_SOUL_TEMPLATE.format(agent_display_name="Researcher Agent"),
    },
    "reviewer": {
        "name": "Reviewer Agent",
        "session_id": "reviewer-master-session-v1",
        "workspace": "reviewer",
        "port": 8102,
        "peer_name": "editor",
        "peer_port": 8100,
        "soul": DEFAULT_SOUL_TEMPLATE.format(agent_display_name="Reviewer Agent"),
    },
}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def load_agents_config() -> Dict[str, Dict[str, Any]]:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    if not AGENTS_CONFIG_PATH.exists():
        with open(AGENTS_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_AGENTS_RAW, f, indent=4)
        return DEFAULT_AGENTS_RAW
    try:
        with open(AGENTS_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error("Failed to load agents config: %s. Falling back to default.", e)
        return DEFAULT_AGENTS_RAW


def save_agent_config(agent_name: str, cfg: Dict[str, Any]) -> None:
    current_config = load_agents_config()
    current_config[agent_name] = cfg
    with open(AGENTS_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current_config, f, indent=4)


# Initial load
AGENTS = load_agents_config()
