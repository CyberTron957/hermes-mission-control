#!/usr/bin/env python3
"""
Hermes Swarm Server — P2P Multi-Agent Framework with Real-Time Monitoring
===========================================================================

Backend features:
  - Multiple agents, each a full Hermes AIAgent instance
  - 10-second atomic batching loop with SQLite task queues
  - P2P messaging via custom tools (send_peer_message, ask_human)
  - Real-time WebSocket broadcasting for live monitoring
  - Full event tracking (task enqueue/dequeue, state changes, messages)
  - REST endpoints for agent management, task injection, and monitoring data
  - Serves the dashboard UI at http://localhost:8000/

Usage:
    PYTHONPATH=/Users/pradhyun/.hermes/hermes-agent python3 swarm_server.py
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("swarm")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
LITELLM_API_BASE = f"http://{SERVER_HOST}:4000/v1"
SWEEP_INTERVAL_SECONDS = 10

WORKSPACE_ROOT = Path(__file__).parent / "swarm_test_environment"
AGENTS_CONFIG_PATH = WORKSPACE_ROOT / "agents_config.json"
MONITORING_DB = WORKSPACE_ROOT / "monitoring.db"

DEFAULT_SOUL_TEMPLATE = (
    "You are the {agent_display_name}.\n"
    "Use your tools to complete tasks delegated to you.\n"
    "If you ever need human clarification, feedback, or intervention, you MUST call the 'ask_human' tool.\n"
    "To send a message, task, result, or response to another agent, you MUST use the 'send_peer_message' tool.\n"
    "After calling 'send_peer_message', immediately stop calling tools and end your turn."
)

DEFAULT_AGENTS_RAW = {
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


def save_agent_config(agent_name: str, cfg: Dict[str, Any]):
    current_config = load_agents_config()
    current_config[agent_name] = cfg
    with open(AGENTS_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current_config, f, indent=4)


AGENTS = load_agents_config()


# ---------------------------------------------------------------------------
# Monitoring Database (SQLite for events + messages)
# ---------------------------------------------------------------------------
class MonitoringDB:
    """Central SQLite database for all monitoring events and message history."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp   REAL    NOT NULL,
        agent_name  TEXT    NOT NULL,
        event_type  TEXT    NOT NULL,
        from_agent  TEXT,
        to_agent    TEXT,
        task_id     TEXT,
        data        TEXT
    );

    CREATE TABLE IF NOT EXISTS messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp   REAL    NOT NULL,
        agent_name  TEXT    NOT NULL,
        role        TEXT    NOT NULL,
        content     TEXT    NOT NULL,
        task_id     TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_events_agent     ON events(agent_name);
    CREATE INDEX IF NOT EXISTS idx_events_time      ON events(timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_events_type      ON events(event_type);
    CREATE INDEX IF NOT EXISTS idx_messages_agent   ON messages(agent_name);
    CREATE INDEX IF NOT EXISTS idx_messages_time    ON messages(timestamp DESC);
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        return sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()

    def log_event(self, agent_name: str, event_type: str,
                  from_agent: str = None, to_agent: str = None,
                  task_id: str = None, data: dict = None):
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO events (timestamp, agent_name, event_type, from_agent, to_agent, task_id, data)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (time.time(), agent_name, event_type, from_agent, to_agent, task_id,
                     json.dumps(data) if data else None)
                )
                conn.commit()
        except Exception as e:
            log.warning("[MonitorDB] Failed to log event: %s", e)

    def log_message(self, agent_name: str, role: str, content: str, task_id: str = None):
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO messages (timestamp, agent_name, role, content, task_id) VALUES (?, ?, ?, ?, ?)",
                    (time.time(), agent_name, role, content, task_id)
                )
                conn.commit()
        except Exception as e:
            log.warning("[MonitorDB] Failed to log message: %s", e)

    def get_events(self, agent_name: str = None, limit: int = 100, offset: int = 0) -> List[dict]:
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                if agent_name:
                    rows = conn.execute(
                        "SELECT * FROM events WHERE agent_name = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                        (agent_name, limit, offset)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM events ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                        (limit, offset)
                    ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.warning("[MonitorDB] Failed to get events: %s", e)
            return []

    def get_messages(self, agent_name: str, limit: int = 100, offset: int = 0) -> List[dict]:
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM messages WHERE agent_name = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                    (agent_name, limit, offset)
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.warning("[MonitorDB] Failed to get messages: %s", e)
            return []

    def get_agent_stats(self) -> Dict[str, dict]:
        stats = {}
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT agent_name, event_type, COUNT(*) as count FROM events GROUP BY agent_name, event_type"
                ).fetchall()
                for r in rows:
                    aname = r["agent_name"]
                    if aname not in stats:
                        stats[aname] = {"events": {}, "last_active": None, "total_messages": 0}
                    stats[aname]["events"][r["event_type"]] = r["count"]

                rows = conn.execute(
                    "SELECT agent_name, MAX(timestamp) as last_ts FROM events GROUP BY agent_name"
                ).fetchall()
                for r in rows:
                    if r["agent_name"] in stats:
                        stats[r["agent_name"]]["last_active"] = r["last_ts"]

                rows = conn.execute(
                    "SELECT agent_name, COUNT(*) as count FROM messages GROUP BY agent_name"
                ).fetchall()
                for r in rows:
                    if r["agent_name"] in stats:
                        stats[r["agent_name"]]["total_messages"] = r["count"]
        except Exception as e:
            log.warning("[MonitorDB] Failed to get stats: %s", e)
        return stats


monitor_db = MonitoringDB(MONITORING_DB)


# ---------------------------------------------------------------------------
# Thread-Safe WebSocket Broadcasting
# ---------------------------------------------------------------------------
_main_event_loop: Optional[asyncio.AbstractEventLoop] = None

# Global lock to serialize agent initialization (HERMES_HOME is process-scoped)
_agent_init_lock = threading.Lock()


def _broadcast(event_type: str, payload: dict):
    """Thread-safe event broadcast. Works from any thread or async context."""
    if not _main_event_loop or not _main_event_loop.is_running():
        return
    try:
        try:
            current_loop = asyncio.get_running_loop()
            if current_loop is _main_event_loop:
                asyncio.create_task(ws_broadcaster.broadcast(event_type, payload))
                return
        except RuntimeError:
            pass

        asyncio.run_coroutine_threadsafe(
            ws_broadcaster.broadcast(event_type, payload),
            _main_event_loop
        )
    except Exception as e:
        log.warning("[Broadcast] Failed (%s): %s", event_type, e)


# ---------------------------------------------------------------------------
# WebSocket Manager
# ---------------------------------------------------------------------------
class WSBroadcaster:
    def __init__(self):
        self.clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.clients.add(ws)
        log.info("[WS] Client connected. Total: %d", len(self.clients))

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self.clients.discard(ws)
        log.info("[WS] Client disconnected. Total: %d", len(self.clients))

    async def broadcast(self, event_type: str, payload: dict):
        if not self.clients:
            return
        message = json.dumps({"type": event_type, "payload": payload})
        disconnected = []
        async with self._lock:
            clients = list(self.clients)
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)
        if disconnected:
            async with self._lock:
                for ws in disconnected:
                    self.clients.discard(ws)


ws_broadcaster = WSBroadcaster()


# ---------------------------------------------------------------------------
# Task Queue
# ---------------------------------------------------------------------------
class TaskQueue:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasks (
        id          TEXT PRIMARY KEY,
        from_agent  TEXT NOT NULL,
        payload     TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'pending',
        created_at  REAL NOT NULL,
        processed_at REAL
    );
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self):
        return sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(self.SCHEMA)
            conn.commit()

    def enqueue(self, from_agent: str, payload: str) -> str:
        task_id = str(uuid.uuid4())
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO tasks (id, from_agent, payload, status, created_at) VALUES (?,?,?,?,?)",
                (task_id, from_agent, payload, "pending", time.time()),
            )
            conn.commit()
        log.info("[Queue] Enqueued task %s from '%s'", task_id[:8], from_agent)
        monitor_db.log_event(self.db_path.name.replace("_queue.db", ""), "task_enqueued",
                           from_agent=from_agent, task_id=task_id,
                           data={"payload_preview": payload[:100]})
        return task_id

    def drain_pending(self) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT id, from_agent, payload FROM tasks WHERE status='pending' ORDER BY created_at"
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE tasks SET status='processing', processed_at=? WHERE id IN ({placeholders})",
                    [time.time()] + ids,
                )
                conn.commit()
        return [{"id": r[0], "from_agent": r[1], "payload": r[2]} for r in rows]

    def mark_done(self, task_id: str):
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
            conn.commit()

    def get_pending_count(self) -> int:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()
            return row[0] if row else 0

    def get_all_tasks(self, limit: int = 50) -> List[dict]:
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tool Schemas & Registry
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

_daemon_registry: Dict[str, "AgentDaemon"] = {}


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
    log.info("[send_peer_message] %s → %s | task_id=%s", caller, to_agent, task_id[:8])

    _broadcast("message_sent", {
        "from_agent": caller,
        "to_agent": to_agent,
        "task_id": task_id,
        "message_preview": message[:120],
        "timestamp": time.time(),
    })

    return json.dumps({
        "success": True,
        "task_id": task_id,
        "message": f"Message enqueued to '{to_agent}' successfully.",
    })


def _ask_human_handler(args: dict, **kwargs) -> str:
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
    daemon.human_event.wait()

    with daemon._lock:
        daemon.state = AGENT_STATE_BUSY

    log.info("[%s] [ask_human] Response received: %s", daemon.name, daemon.human_response)
    monitor_db.log_event(caller, "human_responded", data={"question": question, "response": daemon.human_response})
    _broadcast("human_responded", {
        "agent_name": caller,
        "question": question,
        "response": daemon.human_response,
        "timestamp": time.time(),
    })

    return json.dumps({"success": True, "response": daemon.human_response})


def _register_custom_tools():
    try:
        sys.path.insert(0, "/Users/pradhyun/.hermes/hermes-agent")
        from tools.registry import registry
        if "send_peer_message" not in (registry.get_tool_to_toolset_map() or {}):
            registry.register(
                name="send_peer_message", toolset="custom",
                schema=_SEND_PEER_MESSAGE_TOOL_SCHEMA["function"],
                handler=_send_peer_message_handler,
                description="Send a message to another swarm agent.",
            )
            log.info("[send_peer_message] Registered")
        if "ask_human" not in (registry.get_tool_to_toolset_map() or {}):
            registry.register(
                name="ask_human", toolset="custom",
                schema=_ASK_HUMAN_TOOL_SCHEMA["function"],
                handler=_ask_human_handler,
                description="Ask a human for clarification.",
            )
            log.info("[ask_human] Registered")
    except Exception as exc:
        log.warning("[Custom Tools] Could not register in Hermes registry: %s", exc)


# ---------------------------------------------------------------------------
# Agent Daemon
# ---------------------------------------------------------------------------
AGENT_STATE_IDLE = "idle"
AGENT_STATE_BUSY = "busy"


class AgentDaemon:
    def __init__(self, name: str, cfg: Dict[str, Any]) -> None:
        self.name = name
        self.cfg = cfg
        self.state = AGENT_STATE_IDLE
        self._lock = threading.Lock()

        workspace_dir = WORKSPACE_ROOT / cfg["workspace"]
        workspace_dir.mkdir(parents=True, exist_ok=True)
        db_path = workspace_dir / f"{name}_queue.db"
        self.queue = TaskQueue(db_path)

        # Each agent gets its own isolated Hermes home (separate state.db, config, sessions, memory)
        self._hermes_home = workspace_dir / ".hermes"
        self._hermes_home.mkdir(parents=True, exist_ok=True)

        self._ai_agent = None
        self._sweep_task: Optional[asyncio.Task] = None

        self.human_event = threading.Event()
        self.human_response = None
        self.next_sweep_at = 0.0

    def _ensure_agent(self):
        if self._ai_agent is not None:
            return
        with _agent_init_lock:
            # Double-check after acquiring lock
            if self._ai_agent is not None:
                return
            try:
                sys.path.insert(0, "/Users/pradhyun/.hermes/hermes-agent")
                from run_agent import AIAgent

                # Isolate this agent's Hermes environment completely
                os.environ["HERMES_HOME"] = str(self._hermes_home)
                
                self._ai_agent = AIAgent(
                    base_url=LITELLM_API_BASE,
                    api_key="sk-1234",
                    model="litellm-model",
                    session_id=self.cfg["session_id"],
                    skip_memory=False,
                    skip_context_files=False,
                    quiet_mode=True,
                    ephemeral_system_prompt=self.cfg["soul"],
                )
                _register_custom_tools()

                existing_names = {t.get("function", {}).get("name") for t in (self._ai_agent.tools or [])}
                if "send_peer_message" not in existing_names:
                    self._ai_agent.tools = list(self._ai_agent.tools or [])
                    self._ai_agent.tools.append(_SEND_PEER_MESSAGE_TOOL_SCHEMA)
                    self._ai_agent.valid_tool_names.add("send_peer_message")
                if "ask_human" not in existing_names:
                    self._ai_agent.tools = list(self._ai_agent.tools or [])
                    self._ai_agent.tools.append(_ASK_HUMAN_TOOL_SCHEMA)
                    self._ai_agent.valid_tool_names.add("ask_human")

                # Eagerly init session DB while HERMES_HOME is locked to this agent.
                # Prevents later run_conversation() from lazily creating a SessionDB
                # against another agent's HERMES_HOME.
                try:
                    sd = self._ai_agent._get_session_db_for_recall()
                    if sd is None:
                        log.error("[%s] _get_session_db_for_recall() returned None", self.name)
                    else:
                        log.info("[%s] SessionDB created at %s", self.name, getattr(sd, '_db_path', '?'))
                except Exception as e:
                    log.error("[%s] _get_session_db_for_recall() failed: %s", self.name, e)
                self._ai_agent._ensure_db_session()

                log.info("[%s] Hermes AIAgent initialised (session=%s, home=%s)", self.name, self.cfg["session_id"], self._hermes_home)
            except Exception as exc:
                log.error("[%s] Failed to init AIAgent: %s", self.name, exc)
                raise

    def _load_session_from_db(self) -> List[Dict[str, Any]]:
        """Load conversation history from agent's own isolated Hermes session DB."""
        if self._ai_agent is None:
            log.debug("[%s] _load_session_from_db: _ai_agent is None", self.name)
            return []
        session_db = getattr(self._ai_agent, "_session_db", None)
        if session_db is None:
            log.warning("[%s] _load_session_from_db: _session_db is None — session DB not initialized", self.name)
            return []
        try:
            # Use the agent's CURRENT session_id (Hermes may rotate on compression)
            current_sid = getattr(self._ai_agent, "session_id", None) or self.cfg["session_id"]
            msgs = session_db.get_messages_as_conversation(current_sid, include_ancestors=True)
            log.debug("[%s] Loaded %d messages from session %s", self.name, len(msgs), current_sid)
            return msgs
        except Exception as e:
            log.warning("[%s] Failed to load session from DB: %s", self.name, e)
            return []

    def ingest_task(self, from_agent: str, payload: str) -> str:
        task_id = self.queue.enqueue(from_agent, payload)
        log.info("[%s] Task queued from '%s': %s", self.name, from_agent, payload[:80])
        _broadcast("queue_updated", {
            "agent_name": self.name,
            "pending_count": self.queue.get_pending_count(),
            "timestamp": time.time(),
        })
        return task_id

    async def sweep_loop(self):
        log.info("[%s] Sweep loop started (interval=%ds)", self.name, SWEEP_INTERVAL_SECONDS)
        while True:
            self.next_sweep_at = time.time() + SWEEP_INTERVAL_SECONDS
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await self._sweep()

    async def _sweep(self):
        with self._lock:
            if self.state != AGENT_STATE_IDLE:
                return
            old_state = self.state
            self.state = AGENT_STATE_BUSY

        if old_state != AGENT_STATE_BUSY:
            _broadcast("state_change", {
                "agent_name": self.name,
                "state": self.state,
                "timestamp": time.time(),
                "next_sweep_at": self.next_sweep_at,
            })
            monitor_db.log_event(self.name, "state_change", data={"new_state": self.state})

        try:
            tasks = self.queue.drain_pending()
            if not tasks:
                with self._lock:
                    self.state = AGENT_STATE_IDLE
                    self.next_sweep_at = time.time() + SWEEP_INTERVAL_SECONDS
                _broadcast("state_change", {
                    "agent_name": self.name,
                    "state": self.state,
                    "timestamp": time.time(),
                    "next_sweep_at": self.next_sweep_at,
                })
                monitor_db.log_event(self.name, "state_change", data={"new_state": AGENT_STATE_IDLE})
                return

            log.info("[%s] Sweep: processing %d task(s) in batch", self.name, len(tasks))
            monitor_db.log_event(self.name, "task_dequeued", data={"count": len(tasks)})
            _broadcast("task_dequeued", {
                "agent_name": self.name,
                "count": len(tasks),
                "timestamp": time.time(),
            })

            await self._process_tasks_batch(tasks)
        finally:
            with self._lock:
                if self.state == AGENT_STATE_BUSY:
                    self.state = AGENT_STATE_IDLE
                    self.next_sweep_at = time.time() + SWEEP_INTERVAL_SECONDS
                    _broadcast("state_change", {
                        "agent_name": self.name,
                        "state": self.state,
                        "timestamp": time.time(),
                        "next_sweep_at": self.next_sweep_at,
                    })
                    monitor_db.log_event(self.name, "state_change", data={"new_state": AGENT_STATE_IDLE})

    async def _process_tasks_batch(self, tasks: List[Dict[str, Any]]):
        task_ids = [t["id"] for t in tasks]
        task_preview = ", ".join([t["id"][:8] for t in tasks])
        log.info("[%s] Processing batch: %s", self.name, task_preview)
        _broadcast("conversation_start", {
            "agent_name": self.name,
            "task_count": len(tasks),
            "task_ids": task_ids,
            "timestamp": time.time(),
        })

        # Combine all payloads into one prompt
        combined = f"You have {len(tasks)} new message(s) to process:\n\n"
        for i, task in enumerate(tasks, 1):
            combined += f"--- [{i}] from {task['from_agent']} ---\n{task['payload']}\n\n"

        try:
            self._ensure_agent()
            history = self._load_session_from_db()
            response = await asyncio.to_thread(
                self._ai_agent.run_conversation,
                user_message=combined,
                task_id=f"agent_name:{self.name}",
                conversation_history=history,
            )
            new_messages = response.get("messages", [])
            final = str(response.get("final_response", ""))
            log.info("[%s] Batch complete. Response: %s", self.name, final[:200])

            # Find messages generated in THIS turn (everything after last user message)
            last_user_idx = -1
            for i, msg in enumerate(new_messages):
                if msg.get("role") == "user":
                    last_user_idx = i
            turn_messages = new_messages[last_user_idx + 1:] if last_user_idx >= 0 else new_messages

            for msg in turn_messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")

                if role == "assistant" and msg.get("tool_calls"):
                    tcs = msg["tool_calls"]
                    tool_summary = " | ".join([
                        f"{tc.get('function', {}).get('name', '?')}()"
                        for tc in tcs
                    ])
                    content = f"🛠️ Tool Calls: {tool_summary}\n\n{content or ''}"

                if role == "tool":
                    tc_id = msg.get("tool_call_id", "?")
                    content = f"📤 Tool Result [{tc_id}]: {content}"

                monitor_db.log_message(self.name, role, content, ",".join(task_ids))
                _broadcast("message_logged", {
                    "agent_name": self.name,
                    "role": role,
                    "content": content,
                    "task_id": task_preview,
                    "timestamp": time.time(),
                })

            monitor_db.log_event(self.name, "conversation_complete", data={"response_preview": final[:200]})
            _broadcast("conversation_complete", {
                "agent_name": self.name,
                "task_count": len(tasks),
                "response_preview": final[:200],
                "timestamp": time.time(),
            })
            for t in tasks:
                self.queue.mark_done(t["id"])
        except Exception as exc:
            log.error("[%s] Batch failed: %s", self.name, exc)
            monitor_db.log_event(self.name, "error", data={"error": str(exc), "task_ids": task_ids})
            _broadcast("error", {
                "agent_name": self.name,
                "task_ids": task_ids,
                "error": str(exc),
                "timestamp": time.time(),
            })

    async def _process_task(self, task: Dict[str, Any]):
        task_id = task["id"]
        from_agent = task["from_agent"]
        payload = task["payload"]
        log.info("[%s] Processing task %s from '%s'", self.name, task_id[:8], from_agent)

        prompt = f"[MESSAGE from {from_agent}]\n{payload}"
        _broadcast("conversation_start", {
            "agent_name": self.name,
            "task_id": task_id,
            "from_agent": from_agent,
            "timestamp": time.time(),
        })

        try:
            self._ensure_agent()
            history = self._load_session_from_db()
            response = await asyncio.to_thread(
                self._ai_agent.run_conversation,
                user_message=prompt,
                task_id=f"agent_name:{self.name}",
                conversation_history=history,
            )
            new_messages = response.get("messages", [])
            final = str(response.get("final_response", ""))
            log.info("[%s] Task %s complete. Response: %s", self.name, task_id[:8], final[:200])

            # Log messages generated in THIS turn (after last user message)
            last_user_idx = -1
            for i, msg in enumerate(new_messages):
                if msg.get("role") == "user":
                    last_user_idx = i
            turn_messages = new_messages[last_user_idx + 1:] if last_user_idx >= 0 else new_messages
            for msg in turn_messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")

                if role == "assistant" and msg.get("tool_calls"):
                    tcs = msg["tool_calls"]
                    tool_summary = " | ".join([
                        f"{tc.get('function', {}).get('name', '?')}()"
                        for tc in tcs
                    ])
                    content = f"🛠️ Tool Calls: {tool_summary}\n\n{content or ''}"

                if role == "tool":
                    tc_id = msg.get("tool_call_id", "?")
                    content = f"📤 Tool Result [{tc_id}]: {content}"

                monitor_db.log_message(self.name, role, content, task_id)
                _broadcast("message_logged", {
                    "agent_name": self.name,
                    "role": role,
                    "content": content,
                    "task_id": task_id,
                    "timestamp": time.time(),
                })

            monitor_db.log_event(self.name, "conversation_complete", data={"response_preview": final[:200]})
            _broadcast("conversation_complete", {
                "agent_name": self.name,
                "task_id": task_id,
                "response_preview": final[:200],
                "timestamp": time.time(),
            })
            self.queue.mark_done(task_id)
        except Exception as exc:
            log.error("[%s] Task %s failed: %s", self.name, task_id[:8], exc)
            monitor_db.log_event(self.name, "error", data={"error": str(exc), "task_id": task_id})
            _broadcast("error", {
                "agent_name": self.name,
                "task_id": task_id,
                "error": str(exc),
                "timestamp": time.time(),
            })

    def start_sweep(self, loop: asyncio.AbstractEventLoop):
        self._sweep_task = loop.create_task(self.sweep_loop())


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(title="Hermes Swarm Server", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

daemons: Dict[str, AgentDaemon] = {}


# ---------------------------------------------------------------------------
# WebSocket Endpoint
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_broadcaster.connect(ws)
    try:
        state_snapshot = {
            "type": "state_snapshot",
            "payload": {
                "agents": {
                    name: {
                        "state": d.state,
                        "pending_count": d.queue.get_pending_count(),
                        "config": d.cfg,
                        "next_sweep_at": d.next_sweep_at,
                    }
                    for name, d in daemons.items()
                },
                "timestamp": time.time(),
            }
        }
        await ws.send_text(json.dumps(state_snapshot))

        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("action") == "ping":
                    await ws.send_text(json.dumps({"type": "pong", "payload": {}}))
            except Exception:
                pass
    except WebSocketDisconnect:
        await ws_broadcaster.disconnect(ws)
    except Exception as e:
        log.warning("[WS] Error: %s", e)
        await ws_broadcaster.disconnect(ws)


# ---------------------------------------------------------------------------
# Core Agent Routes
# ---------------------------------------------------------------------------
@app.post("/agent/{agent_name}/task")
async def agent_ingest(agent_name: str, request: Request):
    body = await request.json()
    from_agent = body.get("from_agent", "unknown")
    payload = body.get("payload", "")
    if not payload:
        return JSONResponse({"error": "empty payload"}, status_code=400)
    daemon = daemons.get(agent_name)
    if daemon is None:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    task_id = daemon.ingest_task(from_agent, payload)
    return JSONResponse({"task_id": task_id, "status": "queued"})


@app.get("/agent/{agent_name}/status")
async def agent_status(agent_name: str):
    daemon = daemons.get(agent_name)
    if daemon is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({
        "agent": agent_name,
        "state": daemon.state,
        "pending_count": daemon.queue.get_pending_count(),
        "session_id": daemon.cfg.get("session_id"),
    })


@app.post("/agent/{agent_name}/human_response")
async def human_response(agent_name: str, request: Request):
    body = await request.json()
    response_text = body.get("response", "")
    if not response_text:
        return JSONResponse({"error": "empty response"}, status_code=400)
    daemon = daemons.get(agent_name)
    if daemon is None:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    if daemon.state != "asking_human":
        return JSONResponse({"error": f"Agent is not asking human (state: {daemon.state})"}, status_code=400)
    daemon.human_response = response_text
    daemon.human_event.set()
    return {"status": "ok", "message": "Response sent to agent."}


# ---------------------------------------------------------------------------
# Monitoring Routes
# ---------------------------------------------------------------------------
@app.get("/monitoring/agents")
async def monitoring_agents():
    return JSONResponse({
        "agents": {
            name: {
                "state": d.state,
                "pending_count": d.queue.get_pending_count(),
                "next_sweep_at": d.next_sweep_at,
                "workspace": d.cfg.get("workspace"),
                "session_id": d.cfg.get("session_id"),
                "soul_preview": d.cfg.get("soul", "")[:200],
            }
            for name, d in daemons.items()
        },
        "timestamp": time.time(),
    })


@app.get("/monitoring/agents/{agent_name}/events")
async def monitoring_events(agent_name: str, limit: int = 50):
    events = monitor_db.get_events(agent_name=agent_name, limit=limit)
    return JSONResponse({"agent": agent_name, "events": events})


@app.get("/monitoring/agents/{agent_name}/messages")
async def monitoring_messages(agent_name: str, limit: int = 200, offset: int = 0):
    messages = monitor_db.get_messages(agent_name=agent_name, limit=limit, offset=offset)
    messages.reverse()  # chronological order
    return JSONResponse({"agent": agent_name, "messages": messages})


@app.get("/monitoring/agents/{agent_name}/queue")
async def monitoring_queue(agent_name: str):
    daemon = daemons.get(agent_name)
    if daemon is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    tasks = daemon.queue.get_all_tasks(limit=100)
    return JSONResponse({"agent": agent_name, "pending_count": daemon.queue.get_pending_count(), "tasks": tasks})


@app.get("/monitoring/stats")
async def monitoring_stats():
    stats = monitor_db.get_agent_stats()
    for name, daemon in daemons.items():
        if name not in stats:
            stats[name] = {"events": {}, "total_messages": 0}
        stats[name]["current_state"] = daemon.state
        stats[name]["pending_count"] = daemon.queue.get_pending_count()
    return JSONResponse({"stats": stats, "timestamp": time.time()})


@app.get("/monitoring/recent_events")
async def monitoring_recent(limit: int = 100):
    events = monitor_db.get_events(limit=limit)
    return JSONResponse({"events": events})


# ---------------------------------------------------------------------------
# Agent Management Routes
# ---------------------------------------------------------------------------
@app.get("/agents")
async def get_agents():
    return JSONResponse(load_agents_config())


@app.post("/agent")
async def add_or_update_agent(request: Request):
    body = await request.json()
    agent_name = body.get("agent_name")
    name = body.get("name")
    session_id = body.get("session_id")
    workspace = body.get("workspace")
    port = body.get("port")
    peer_name = body.get("peer_name")
    peer_port = body.get("peer_port")
    soul = body.get("soul")

    if not agent_name or not name or not session_id or not workspace or not soul:
        return JSONResponse({"error": "Missing required fields"}, status_code=400)

    cfg = {
        "name": name, "session_id": session_id, "workspace": workspace,
        "port": port, "peer_name": peer_name, "peer_port": peer_port,
        "soul": soul,
    }
    save_agent_config(agent_name, cfg)
    loop = asyncio.get_running_loop()
    if agent_name in daemons:
        daemon = daemons[agent_name]
        with daemon._lock:
            daemon.cfg = cfg
            daemon._ai_agent = None
    else:
        register_agent_daemon(agent_name, cfg, loop)
    return JSONResponse({"status": "success", "message": f"Agent '{agent_name}' registered/updated."})


@app.post("/agent/{agent_name}/soul")
async def update_agent_soul(agent_name: str, request: Request):
    body = await request.json()
    soul = body.get("soul")
    if not soul:
        return JSONResponse({"error": "Missing 'soul' field"}, status_code=400)

    config = load_agents_config()
    if agent_name not in config:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    cfg = config[agent_name]
    cfg["soul"] = soul
    save_agent_config(agent_name, cfg)

    if agent_name in daemons:
        daemon = daemons[agent_name]
        with daemon._lock:
            daemon.cfg = cfg
            daemon._ai_agent = None
        log.info("[Dynamic Registry] Soul updated for agent '%s'", agent_name)

    _broadcast("soul_updated", {"agent_name": agent_name, "timestamp": time.time()})
    return JSONResponse({"status": "success", "message": f"Soul for '{agent_name}' updated."})


@app.get("/health")
async def health():
    return {"status": "ok", "agents": list(daemons.keys())}


# ---------------------------------------------------------------------------
# Dashboard Root
# ---------------------------------------------------------------------------
DASHBOARD_DIR = Path(__file__).parent / "dashboard"


@app.get("/")
async def root_dashboard():
    dashboard_file = DASHBOARD_DIR / "index.html"
    if dashboard_file.exists():
        return FileResponse(str(dashboard_file))
    return HTMLResponse(
        "<h1>Dashboard not found</h1><p>Run from project root.</p>",
        status_code=404
    )


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup():
    global _main_event_loop
    _main_event_loop = asyncio.get_running_loop()
    log.info("[Startup] Main event loop captured: %s", _main_event_loop)

    for agent_name, cfg in AGENTS.items():
        db_path = WORKSPACE_ROOT / cfg["workspace"] / f"{agent_name}_queue.db"
        if db_path.exists():
            try:
                db_path.unlink()
                log.info("[Startup] Cleaned up previous DB for '%s'", agent_name)
            except Exception as e:
                log.warning("[Startup] Could not delete DB %s: %s", db_path, e)

    loop = asyncio.get_running_loop()
    for agent_name, cfg in AGENTS.items():
        register_agent_daemon(agent_name, cfg, loop)

    log.info("[Startup] All agents running. LiteLLM at %s", LITELLM_API_BASE)
    log.info("[Startup] Dashboard at http://%s:%s/", SERVER_HOST, SERVER_PORT)


@app.on_event("shutdown")
async def on_shutdown():
    for daemon in daemons.values():
        if daemon._sweep_task:
            daemon._sweep_task.cancel()
    log.info("[Shutdown] All sweep tasks cancelled")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def register_agent_daemon(agent_name: str, cfg: Dict[str, Any], loop: asyncio.AbstractEventLoop):
    daemon = AgentDaemon(agent_name, cfg)
    daemons[agent_name] = daemon
    _daemon_registry[agent_name] = daemon
    daemon.start_sweep(loop)
    log.info("[Dynamic Registry] Registered agent '%s' daemon", agent_name)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  Hermes Swarm Server v0.2.0")
    log.info("  Dashboard:  http://%s:%s/", SERVER_HOST, SERVER_PORT)
    log.info("=" * 60)
    uvicorn.run("swarm_server:app", host=SERVER_HOST, port=SERVER_PORT, log_level="info", reload=False)
