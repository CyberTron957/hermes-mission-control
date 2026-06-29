#!/usr/bin/env python3
"""
P2P Multi-Agent Teams Testbed
==============================
2-agent cluster (Editor + Researcher) using the Hermes Agent SDK.

Architecture:
  - Each agent has its own persistent Hermes session (single session per agent).
  - Agents communicate via a local SQLite task queue.
  - A background sweep every 10s drains pending tasks and injects them into
    the session history when the agent is idle.
  - Connects to a real LiteLLM proxy on port 4000.

Run:
    PYTHONPATH=$HERMES_AGENT_PATH python3 test_teams.py
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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("teams")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
LITELLM_API_BASE = f"http://{SERVER_HOST}:4000/v1"
SWEEP_INTERVAL_SECONDS = 10

WORKSPACE_ROOT = Path(__file__).parent.parent / "data"
AGENTS_CONFIG_PATH = WORKSPACE_ROOT / "agents_config.json"

DEFAULT_SOUL_TEMPLATE = (
    "You are the {agent_display_name}.\n"
    "Use your tools to complete tasks delegated to you.\n"
    "If you ever need human clarification, feedback, or intervention, you MUST call the 'ask_human' tool.\n"
    "To send a message, task, result, or response to another agent, you MUST use the 'send_peer_message' tool.\n"
    "CRITICAL: After you call the 'send_peer_message' tool, you MUST immediately stop calling tools and end your turn "
    "by outputting a text response summarizing what you sent. Do NOT call send_peer_message again or execute other tools in the same turn."
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

# Load configs dynamically
AGENTS = load_agents_config()


# ---------------------------------------------------------------------------
# Event Bus — Real-Time Monitoring Infrastructure
# ---------------------------------------------------------------------------
class TeamsEventBus:
    """In-memory event pub/sub for real-time dashboard updates."""

    def __init__(self):
        self._subscribers: List[asyncio.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=500)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event_type: str, data: dict):
        """Thread-safe publish — safe to call from sync code on the event loop thread."""
        event = {"type": event_type, "data": data, "timestamp": time.time()}
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except:
                    pass

event_bus = TeamsEventBus()

# Rolling event history (last 500 events — for dashboard initial load)
EVENT_HISTORY_MAX = 500
event_history: List[dict] = []


def record_event(event_type: str, data: dict):
    """Publish + persist in rolling history."""
    event = {"type": event_type, "data": data, "timestamp": time.time()}
    event_history.append(event)
    if len(event_history) > EVENT_HISTORY_MAX:
        event_history[:100] = []
    event_bus.publish(event_type, data)


# ---------------------------------------------------------------------------
# Log Capture Handler — streams log records as teams events
# ---------------------------------------------------------------------------
class TeamsLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            record_event("log", {
                "level": record.levelname.lower(),
                "message": msg,
                "logger": record.name,
            })
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SQLite Task Queue
# ---------------------------------------------------------------------------
class TaskQueue:
    """Per-agent SQLite-backed task queue."""

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

    def enqueue(self, from_agent: str, payload: str, target_agent: str = "") -> str:
        task_id = str(uuid.uuid4())
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO tasks (id, from_agent, payload, status, created_at) VALUES (?,?,?,?,?)",
                (task_id, from_agent, payload, "pending", time.time()),
            )
            conn.commit()
        log.info("[Queue] Enqueued task %s from '%s'", task_id[:8], from_agent)
        record_event("task_enqueued", {
            "task_id": task_id, "from_agent": from_agent, "payload": payload,
            "target_agent": target_agent,
        })
        return task_id

    def drain_pending(self) -> List[Dict[str, Any]]:
        """Return all pending tasks and mark them as 'processing'."""
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
        result = [{"id": r[0], "from_agent": r[1], "payload": r[2]} for r in rows]
        if result:
            record_event("task_draining", {
                "agent": self.db_path.stem.replace("_queue", ""),
                "task_ids": [r["id"] for r in result],
            })
        return result

    def mark_done(self, task_id: str):
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
            conn.commit()
        record_event("task_processed", {"task_id": task_id, "status": "done"})

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        """Return all tasks (pending, processing, done) for dashboard display."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT id, from_agent, payload, status, created_at, processed_at "
                "FROM tasks ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        return [
            {
                "id": r[0], "from_agent": r[1], "payload": r[2],
                "status": r[3], "created_at": r[4], "processed_at": r[5],
            }
            for r in rows
        ]

# ---------------------------------------------------------------------------
# Custom send_peer_message Tool (registered into the Hermes tool registry)
# ---------------------------------------------------------------------------

# OpenAI-format schema shown to the LLM for the send_peer_message tool
_SEND_PEER_MESSAGE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_peer_message",
        "description": (
            "Send a message to another agent in the teams. The target agent will pick it up "
            "on its next sweep (within 10 seconds) and process it. Use this to chat, pass "
            "results, or delegate work between agents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to_agent": {
                    "type": "string",
                    "description": "Name of the target agent (e.g. 'researcher' or 'editor').",
                },
                "message": {
                    "type": "string",
                    "description": "The message to send to the target agent.",
                },
            },
            "required": ["to_agent", "message"],
        },
    },
}

# Registry for daemon references (populated at startup)
_daemon_registry: Dict[str, "AgentDaemon"] = {}


def _send_peer_message_handler(args: dict, **kwargs) -> str:
    """
    Sync handler for the send_peer_message tool.

    Called by the Hermes registry dispatcher as handler(args_dict, **kwargs).
    Writes directly to the target agent's SQLite queue (in-process, no HTTP).
    """
    to_agent = args.get("to_agent", "")
    message = args.get("message", "")
    
    # Safely extract caller from task_id (formatted as "agent_name:<caller>")
    task_id_arg = kwargs.get("task_id", "")
    caller = "unknown"
    if task_id_arg and task_id_arg.startswith("agent_name:"):
        caller = task_id_arg.split(":", 1)[1]

    target = _daemon_registry.get(to_agent)
    if target is None:
        known = list(_daemon_registry.keys())
        return json.dumps({
            "success": False,
            "error": f"Unknown agent '{to_agent}'. Known agents: {known}",
        })
    task_id = target.ingest_task(from_agent=caller, payload=message)
    log.info("[send_peer_message] %s → %s | task_id=%s | payload=%r",
             caller, to_agent, task_id[:8], message[:80])
    record_event("agent_message_sent", {
        "from_agent": caller, "to_agent": to_agent,
        "task_id": task_id, "payload": message[:300],
    })
    return json.dumps({
        "success": True,
        "task_id": task_id,
        "message": f"Message enqueued to '{to_agent}' successfully. You MUST now stop calling tools and end your turn immediately.",
    })


def _register_send_peer_message_tool() -> None:
    """
    Register the send_peer_message tool in the Hermes tool registry.
    """
    try:
        sys.path.insert(0, os.environ.get("HERMES_AGENT_PATH", os.path.expanduser("~/.hermes/hermes-agent")))
        from tools.registry import registry

        if "send_peer_message" not in (registry.get_tool_to_toolset_map() or {}):
            registry.register(
                name="send_peer_message",
                toolset="custom",
                schema=_SEND_PEER_MESSAGE_TOOL_SCHEMA["function"],
                handler=_send_peer_message_handler,
                description="Send a message to another teams agent.",
            )
            log.info("[send_peer_message] Tool registered in Hermes registry")
    except Exception as exc:
        log.warning("[send_peer_message] Could not register in Hermes registry: %s — "
                    "falling back to schema-only injection", exc)


# ---------------------------------------------------------------------------
# Custom ask_human Tool (registered into the Hermes tool registry)
# ---------------------------------------------------------------------------

# OpenAI-format schema shown to the LLM for the ask_human tool
_ASK_HUMAN_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_human",
        "description": (
            "Ask a human for clarification, feedback, or input when a task is unclear. "
            "This call is synchronous and will block until the human responds."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question or prompt to present to the human.",
                },
            },
            "required": ["question"],
        },
    },
}


def _ask_human_handler(args: dict, **kwargs) -> str:
    """
    Sync handler for the ask_human tool.
    Blocks until the human sends response via FastAPI endpoint.
    """
    question = args.get("question", "")
    task_id_arg = kwargs.get("task_id", "")
    caller = "unknown"
    if task_id_arg and task_id_arg.startswith("agent_name:"):
        caller = task_id_arg.split(":", 1)[1]

    daemon = _daemon_registry.get(caller)
    if daemon is None:
        return json.dumps({"error": f"Caller agent '{caller}' not registered."})

    log.info("[%s] [ask_human] Question to human: %s", daemon.name, question)

    with daemon._lock:
        daemon.state = "asking_human"

    daemon.human_event.clear()
    daemon.human_response = None

    # Sync wait until set
    daemon.human_event.wait()

    with daemon._lock:
        daemon.state = AGENT_STATE_BUSY

    log.info("[%s] [ask_human] Response from human: %s", daemon.name, daemon.human_response)
    return json.dumps({
        "success": True,
        "response": daemon.human_response,
    })


def _register_ask_human_tool() -> None:
    """
    Register the ask_human tool in the Hermes tool registry.
    """
    try:
        sys.path.insert(0, os.environ.get("HERMES_AGENT_PATH", os.path.expanduser("~/.hermes/hermes-agent")))
        from tools.registry import registry

        if "ask_human" not in (registry.get_tool_to_toolset_map() or {}):
            registry.register(
                name="ask_human",
                toolset="custom",
                schema=_ASK_HUMAN_TOOL_SCHEMA["function"],
                handler=_ask_human_handler,
                description="Ask a human for clarification.",
            )
            log.info("[ask_human] Tool registered in Hermes registry")
    except Exception as exc:
        log.warning("[ask_human] Could not register in Hermes registry: %s", exc)


# ---------------------------------------------------------------------------
# Agent State Machine
# ---------------------------------------------------------------------------
AGENT_STATE_IDLE = "idle"
AGENT_STATE_BUSY = "busy"



class AgentDaemon:
    """
    Wraps one Hermes AIAgent with:
      - A persistent Hermes session (single session_id)
      - A SQLite task queue
      - A 10-second sweep loop that drains pending tasks when idle
    """

    def __init__(self, name: str, cfg: Dict[str, Any]) -> None:
        self.name = name
        self.cfg = cfg
        self.state = AGENT_STATE_IDLE
        self._lock = threading.Lock()

        workspace_dir = WORKSPACE_ROOT / cfg["workspace"]
        workspace_dir.mkdir(parents=True, exist_ok=True)
        db_path = workspace_dir / f"{name}_queue.db"
        self.queue = TaskQueue(db_path)

        self._ai_agent = None  # lazy-loaded in _ensure_agent()
        self._sweep_task: Optional[asyncio.Task] = None
        self.conversation_history: List[Dict[str, Any]] = []

        self.human_event = threading.Event()
        self.human_response = None

    # ------------------------------------------------------------------
    # Hermes agent bootstrap + custom tool injection
    # ------------------------------------------------------------------
    def _ensure_agent(self):
        if self._ai_agent is not None:
            return
        try:
            sys.path.insert(0, os.environ.get("HERMES_AGENT_PATH", os.path.expanduser("~/.hermes/hermes-agent")))
            from run_agent import AIAgent

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

            # --- Inject custom send_peer_message tool into this agent's tool surface ---
            _register_send_peer_message_tool()
            _schema = _SEND_PEER_MESSAGE_TOOL_SCHEMA
            existing_names = {
                t.get("function", {}).get("name")
                for t in (self._ai_agent.tools or [])
            }
            if "send_peer_message" not in existing_names:
                self._ai_agent.tools = list(self._ai_agent.tools or [])
                self._ai_agent.tools.append(_schema)
                self._ai_agent.valid_tool_names.add("send_peer_message")

            # --- Inject custom ask_human tool into this agent's tool surface ---
            _register_ask_human_tool()
            _ask_human_schema = _ASK_HUMAN_TOOL_SCHEMA
            existing_names = {
                t.get("function", {}).get("name")
                for t in (self._ai_agent.tools or [])
            }
            if "ask_human" not in existing_names:
                self._ai_agent.tools = list(self._ai_agent.tools or [])
                self._ai_agent.tools.append(_ask_human_schema)
                self._ai_agent.valid_tool_names.add("ask_human")

            log.info("[%s] Hermes AIAgent initialised (session=%s)", self.name, self.cfg["session_id"])
        except Exception as exc:
            log.error("[%s] Failed to init AIAgent: %s", self.name, exc)
            raise

    # ------------------------------------------------------------------
    # Task ingestion (called by peer via HTTP)
    # ------------------------------------------------------------------
    def ingest_task(self, from_agent: str, payload: str) -> str:
        task_id = self.queue.enqueue(from_agent, payload, target_agent=self.name)
        log.info("[%s] Task queued from '%s': %s", self.name, from_agent, payload[:80])
        return task_id

    # ------------------------------------------------------------------
    # Sweep loop — runs every SWEEP_INTERVAL_SECONDS
    # ------------------------------------------------------------------
    async def sweep_loop(self):
        log.info("[%s] Sweep loop started (interval=%ds)", self.name, SWEEP_INTERVAL_SECONDS)
        record_event("agent_state_changed", {"agent": self.name, "state": self.state})
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await self._sweep()

    async def _sweep(self):
        with self._lock:
            if self.state != AGENT_STATE_IDLE:
                log.info("[%s] Sweep skipped — agent is %s", self.name, self.state)
                return
            self.state = AGENT_STATE_BUSY
            record_event("agent_state_changed", {"agent": self.name, "state": self.state})

        try:
            tasks = self.queue.drain_pending()
            if not tasks:
                with self._lock:
                    self.state = AGENT_STATE_IDLE
                    record_event("agent_state_changed", {"agent": self.name, "state": self.state})
                return

            log.info("[%s] Sweep: processing %d task(s)", self.name, len(tasks))
            for task in tasks:
                await self._process_task(task)
        finally:
            with self._lock:
                self.state = AGENT_STATE_IDLE
                record_event("agent_state_changed", {"agent": self.name, "state": self.state})

    async def _process_task(self, task: Dict[str, Any]):
        task_id = task["id"]
        from_agent = task["from_agent"]
        payload = task["payload"]
        log.info("[%s] Processing task %s from '%s'", self.name, task_id[:8], from_agent)

        prompt = f"[MESSAGE from {from_agent}]\n{payload}"

        # Register task environment overrides for this agent's workspace
        try:
            from tools.terminal_tool import register_task_env_overrides
            from tools.file_tools import _get_file_ops, clear_file_ops_cache
            workspace_dir = WORKSPACE_ROOT / self.cfg["workspace"]
            register_task_env_overrides(f"agent_name:{self.name}", {"cwd": str(workspace_dir)})
            clear_file_ops_cache(f"agent_name:{self.name}")
            # Pre-initialize environment to populate active envs cache with workspace
            _get_file_ops(f"agent_name:{self.name}")
        except Exception as exc:
            log.warning("[%s] Failed to register task env overrides: %s", self.name, exc)

        try:
            self._ensure_agent()
            record_event("conversation_entry", {
                "agent": self.name, "role": "user", "content": payload,
                "task_id": task_id, "from_agent": from_agent,
            })
            response = await asyncio.to_thread(
                self._ai_agent.run_conversation,
                user_message=prompt,
                task_id=f"agent_name:{self.name}",
                conversation_history=self.conversation_history
            )
            self.conversation_history = response.get("messages", [])
            final = response.get("final_response", "")
            log.info("[%s] Task %s complete. Response: %s", self.name, task_id[:8], str(final)[:200])
            record_event("conversation_entry", {
                "agent": self.name, "role": "assistant", "content": str(final)[:500],
                "task_id": task_id,
            })
            self.queue.mark_done(task_id)
        except Exception as exc:
            log.error("[%s] Task %s failed: %s", self.name, task_id[:8], exc)
            record_event("task_processed", {"task_id": task_id, "status": "failed", "error": str(exc)})
        finally:
            try:
                from tools.terminal_tool import clear_task_env_overrides
                from tools.file_tools import clear_file_ops_cache
                clear_task_env_overrides(f"agent_name:{self.name}")
                clear_file_ops_cache(f"agent_name:{self.name}")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Start the background sweep task
    # ------------------------------------------------------------------
    def start_sweep(self, loop: asyncio.AbstractEventLoop):
        self._sweep_task = loop.create_task(self.sweep_loop())


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(title="Teams Testbed", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared registry of daemons (populated at startup)
daemons: Dict[str, AgentDaemon] = {}


# Serve the dashboard HTML
DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    if DASHBOARD_PATH.exists():
        return DASHBOARD_PATH.read_text(encoding="utf-8")
    return HTMLResponse("<h1>Dashboard not found. Run from project root.</h1>", status_code=404)


# ---------------------------------------------------------------------------
# P2P task ingestion endpoint
# ---------------------------------------------------------------------------
async def _agent_ingest_handler(request: Request, agent_name: str):
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


@app.post("/agent/{agent_name}/task")
async def agent_ingest(agent_name: str, request: Request):
    return await _agent_ingest_handler(request, agent_name)


@app.get("/agent/{agent_name}/status")
async def agent_status(agent_name: str):
    daemon = daemons.get(agent_name)
    if daemon is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"agent": agent_name, "state": daemon.state})


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
        return JSONResponse({"error": f"Agent is not currently asking human (current state: {daemon.state})"}, status_code=400)

    daemon.human_response = response_text
    daemon.human_event.set()
    return {"status": "ok", "message": "Response sent to agent."}


def register_agent_daemon(agent_name: str, cfg: Dict[str, Any], loop: asyncio.AbstractEventLoop):
    daemon = AgentDaemon(agent_name, cfg)
    daemons[agent_name] = daemon
    _daemon_registry[agent_name] = daemon  # expose to send_peer_message handler
    daemon.start_sweep(loop)
    log.info("[Dynamic Registry] Registered agent '%s' daemon", agent_name)


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
        "name": name,
        "session_id": session_id,
        "workspace": workspace,
        "port": port,
        "peer_name": peer_name,
        "peer_port": peer_port,
        "soul": soul,
    }

    # Save configuration to local file
    save_agent_config(agent_name, cfg)

    # Register daemon if not exists, or update configuration of existing daemon
    loop = asyncio.get_running_loop()
    if agent_name in daemons:
        daemon = daemons[agent_name]
        with daemon._lock:
            daemon.cfg = cfg
            daemon._ai_agent = None  # Force re-creation on next task
        log.info("[Dynamic Registry] Updated configuration for existing agent '%s'", agent_name)
    else:
        register_agent_daemon(agent_name, cfg, loop)

    return JSONResponse({"status": "success", "message": f"Agent '{agent_name}' registered/updated successfully."})


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
            daemon._ai_agent = None  # Force re-creation on next task
        log.info("[Dynamic Registry] Soul updated for agent '%s'", agent_name)

    return JSONResponse({"status": "success", "message": f"Soul for '{agent_name}' updated successfully."})


@app.get("/health")
async def health():
    return {"status": "ok", "agents": list(daemons.keys())}


# ---------------------------------------------------------------------------
# Monitoring Endpoints (SSE, Queue Inspection, History, Logs)
# ---------------------------------------------------------------------------

@app.get("/events")
async def sse_events(request: Request):
    """Server-Sent Events stream for real-time dashboard updates."""
    q = event_bus.subscribe()

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield f": keepalive\n\n"
        finally:
            event_bus.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/events/history")
async def event_history_endpoint(limit: int = 100, event_type: str = None):
    """Return recent event history (up to EVENT_HISTORY_MAX)."""
    events = list(event_history)
    if event_type:
        events = [e for e in events if e.get("type") == event_type]
    return JSONResponse(events[-limit:])


@app.get("/agent/{agent_name}/queue")
async def agent_queue(agent_name: str):
    """Return full task queue contents for an agent."""
    daemon = daemons.get(agent_name)
    if daemon is None:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    tasks = daemon.queue.get_all_tasks()
    return JSONResponse({
        "agent": agent_name,
        "tasks": tasks,
        "pending_count": sum(1 for t in tasks if t["status"] == "pending"),
        "processing_count": sum(1 for t in tasks if t["status"] == "processing"),
        "done_count": sum(1 for t in tasks if t["status"] == "done"),
    })


@app.get("/agent/{agent_name}/history")
async def agent_history(agent_name: str):
    """Return the conversation history for an agent."""
    daemon = daemons.get(agent_name)
    if daemon is None:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    return JSONResponse({
        "agent": agent_name,
        "history": daemon.conversation_history[-100:],
    })


@app.get("/agent/{agent_name}/logs")
async def agent_logs(agent_name: str):
    """Return recent log events for an agent."""
    events = [
        e for e in event_history
        if e.get("type") == "log"
        and agent_name in e.get("data", {}).get("message", "")
    ]
    return JSONResponse({
        "agent": agent_name,
        "logs": [e["data"] for e in events[-200:]],
    })


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup():
    # Attach TeamsLogHandler to root logger for live log streaming
    root_logger = logging.getLogger()
    teams_handler = TeamsLogHandler()
    teams_handler.setLevel(logging.INFO)
    teams_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    ))
    root_logger.addHandler(teams_handler)

    # Clean up previous queue databases for a clean test run
    for agent_name, cfg in AGENTS.items():
        db_path = WORKSPACE_ROOT / cfg["workspace"] / f"{agent_name}_queue.db"
        if db_path.exists():
            try:
                db_path.unlink()
                log.info("[Startup] Cleaned up previous database for '%s'", agent_name)
            except Exception as e:
                log.warning("[Startup] Could not delete DB %s: %s", db_path, e)

    loop = asyncio.get_event_loop()

    for agent_name, cfg in AGENTS.items():
        register_agent_daemon(agent_name, cfg, loop)

    log.info("[Startup] All agents running. LiteLLM proxy at %s", LITELLM_API_BASE)


@app.on_event("shutdown")
async def on_shutdown():
    for daemon in daemons.values():
        if daemon._sweep_task:
            daemon._sweep_task.cancel()
    log.info("[Shutdown] All sweep tasks cancelled")


# ---------------------------------------------------------------------------
# Human manager trigger — kick off the teams scenario
# ---------------------------------------------------------------------------
async def _trigger_editor_after_startup(delay: float = 3.0):
    """After server is up, inject the initial task into the Editor queue."""
    await asyncio.sleep(delay)
    log.info("[Manager] Triggering Editor with initial task...")
    editor_daemon = daemons.get("editor")
    if editor_daemon:
        editor_daemon.ingest_task(
            from_agent="human_manager",
            payload=(
                "Here is a secret code: 42. "
                "You MUST call the 'ask_human' tool to ask the human for their favorite fruit. "
                "When you receive the human's response, output 'The secret code is 42 and the human's favorite fruit is <response>' and stop."
            ),
        )
        log.info("[Manager] Initial task enqueued to Editor. "
                 "First sweep in ~%ds.", SWEEP_INTERVAL_SECONDS)


@app.on_event("startup")
async def schedule_trigger():
    loop = asyncio.get_event_loop()
    loop.create_task(_trigger_editor_after_startup())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  P2P Multi-Agent Teams Testbed")
    log.info("  Workspace: %s", WORKSPACE_ROOT)
    log.info("  LiteLLM  : %s", LITELLM_API_BASE)
    log.info("  Sweep    : every %ds", SWEEP_INTERVAL_SECONDS)
    log.info("=" * 60)

    uvicorn.run(
        "test_teams:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level="info",
        reload=False,
    )
