"""Custom tool schemas, registry, and handlers for P2P communication."""

import json
import logging
from typing import Any, Dict

from swarm_server.monitoring import monitor_db
from swarm_server.websocket import _broadcast

log = logging.getLogger("swarm.tools")

# Maps agent_name -> AgentDaemon instance (populated at runtime by server.py)
_daemon_registry: Dict[str, Any] = {}

# ---------------------------------------------------------------------------
# Tool Schemas
# ---------------------------------------------------------------------------
_SEND_PEER_MESSAGE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_peer_message",
        "description": (
            "Send a message to another agent in the swarm. The target will pick it up "
            "on its next sweep and process it. Use this to chat, pass results, or delegate work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to_agent": {"type": "string", "description": "Name of the target agent."},
                "message": {"type": "string", "description": "The message to send."},
            },
            "required": ["to_agent", "message"],
        },
    },
}

_ASK_HUMAN_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_human",
        "description": "Ask a human for clarification. This call blocks until the human responds.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question to present to the human."},
            },
            "required": ["question"],
        },
    },
}


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------
def _send_peer_message_handler(args: dict, **kwargs) -> str:
    to_agent = args.get("to_agent", "")
    message = args.get("message", "")
    task_id_arg = kwargs.get("task_id", "")
    caller = "unknown"
    if task_id_arg and task_id_arg.startswith("agent_name:"):
        caller = task_id_arg.split(":", 1)[1]

    target = _daemon_registry.get(to_agent)
    if target is None:
        known = list(_daemon_registry.keys())
        return json.dumps({"success": False, "error": f"Unknown agent '{to_agent}'. Known: {known}"})

    task_id = target.ingest_task(from_agent=caller, payload=message)
    log.info("[send_peer_message] %s -> %s | task_id=%s", caller, to_agent, task_id[:8])

    _broadcast("message_sent", {
        "from_agent": caller,
        "to_agent": to_agent,
        "task_id": task_id,
        "message_preview": message[:120],
        "timestamp": __import__("time").time(),
    })

    return json.dumps({
        "success": True,
        "task_id": task_id,
        "message": f"Message enqueued to '{to_agent}' successfully.",
    })


def _ask_human_handler(args: dict, **kwargs) -> str:
    import time

    question = args.get("question", "")
    task_id_arg = kwargs.get("task_id", "")
    caller = "unknown"
    if task_id_arg and task_id_arg.startswith("agent_name:"):
        caller = task_id_arg.split(":", 1)[1]

    daemon = _daemon_registry.get(caller)
    if daemon is None:
        return json.dumps({"error": f"Caller agent '{caller}' not registered."})

    log.info("[%s] [ask_human] Question: %s", daemon.name, question)
    monitor_db.log_event(caller, "human_waiting", data={"question": question})
    _broadcast("human_waiting", {
        "agent_name": caller,
        "question": question,
        "timestamp": time.time(),
    })

    with daemon._lock:
        daemon.state = "asking_human"

    daemon.human_event.clear()
    daemon.human_response = None
    daemon.human_event.wait(timeout=60)  # safety: never deadlock forever

    with daemon._lock:
        daemon.state = "busy"

    if not daemon.human_event.is_set():
        log.warning("[%s] [ask_human] Timeout — no human response in 60s", daemon.name)
        daemon.state = "idle"
        return json.dumps({
            "success": False,
            "error": "No human responded within 60 seconds. Proceed with your best judgment or retry later.",
        })

    log.info("[%s] [ask_human] Response received: %s", daemon.name, daemon.human_response)
    monitor_db.log_event(caller, "human_responded", data={"question": question, "response": daemon.human_response})
    _broadcast("human_responded", {
        "agent_name": caller,
        "question": question,
        "response": daemon.human_response,
        "timestamp": time.time(),
    })

    return json.dumps({"success": True, "response": daemon.human_response})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def _register_custom_tools():
    try:
        import sys

        sys.path.insert(0, "/Users/pradhyun/.hermes/hermes-agent")
        from tools.registry import registry

        if "send_peer_message" not in (registry.get_tool_to_toolset_map() or {}):
            registry.register(
                name="send_peer_message",
                toolset="custom",
                schema=_SEND_PEER_MESSAGE_TOOL_SCHEMA["function"],
                handler=_send_peer_message_handler,
                description="Send a message to another swarm agent.",
            )
            log.info("[send_peer_message] Registered")
        if "ask_human" not in (registry.get_tool_to_toolset_map() or {}):
            registry.register(
                name="ask_human",
                toolset="custom",
                schema=_ASK_HUMAN_TOOL_SCHEMA["function"],
                handler=_ask_human_handler,
                description="Ask a human for clarification.",
            )
            log.info("[ask_human] Registered")
    except Exception as exc:
        log.warning("[Custom Tools] Could not register in Hermes registry: %s", exc)
