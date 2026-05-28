# Hermes Swarm — Engineering & Architecture Review

**Date:** 2026-05-28  
**Scope:** `swarm_server.py`, `test_swarm.py`, `test_*.py`, dashboard, config  
**Reviewer:** Claude (OpenCode)  
**Branch:** N/A (repo-level review)

---

## 0. Executive Summary

The Hermes Swarm is a P2P multi-agent orchestration system with real-time monitoring, SQLite-backed task queues, and WebSocket dashboards. It successfully demonstrates multi-agent LLM coordination, but it has **evolved organically without architectural guardrails**. There are **two nearly-identical server implementations** that have diverged, a **threading/asyncio concurrency mismatch** that will deadlock under load, **no retry or dead-letter semantics**, and **hardcoded deployment assumptions** throughout.

**Verdict:** This is a working prototype, not a production-grade system. It needs stabilization before scale. The good news: the core abstractions (AgentDaemon, TaskQueue, MonitoringDB) are sound. The fixes are incremental, not a rewrite.

**Lake Score:** 3/10 recommendations choose the complete option (most gaps are "missing entirely" rather than "shortcut taken").

---

## 1. System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           EXTERNAL WORLD                                    │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                                   │
│  │ Human    │  │ LiteLLM  │  │ Dashboard│                                   │
│  │ Operator │  │ Proxy    │  │ (Browser)│                                   │
│  │ (HTTP)   │  │ :4000    │  │ (WS/HTTP)│                                   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘                                   │
└───────┼─────────────┼─────────────┼─────────────────────────────────────────┘
        │             │             │
        ▼             │             ▼
┌──────────────┐      │      ┌──────────────────┐
│ FastAPI      │      │      │ WebSocket        │
│ Endpoints    │      │      │ Broadcaster      │
│ /agent/*     │      │      │ (WSBroadsaster)  │
│ /monitoring/*│      │      └────────┬─────────┘
│ /health      │      │               │
└──────┬───────┘      │               │
       │              │               │
       ▼              │               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATION LAYER                                 │
│                                                                             │
│   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐                     │
│   │ AgentDaemon │    │ AgentDaemon │    │ AgentDaemon │  ...                │
│   │  (editor)   │◄──►│ (researcher)│◄──►│  (reviewer) │                     │
│   │             │    │             │    │             │                     │
│   │ ┌─────────┐ │    │ ┌─────────┐ │    │ ┌─────────┐ │                     │
│   │ │SQLite   │ │    │ │SQLite   │ │    │ │SQLite   │ │                     │
│   │ │TaskQueue│ │    │ │TaskQueue│ │    │ │TaskQueue│ │                     │
│   │ │*.db     │ │    │ │*.db     │ │    │ │*.db     │ │                     │
│   │ └────┬────┘ │    │ └────┬────┘ │    │ └────┬────┘ │                     │
│   └──────┼──────┘    └──────┼──────┘    └──────┼──────┘                     │
│          │                  │                  │                            │
│          └──────────────────┴──────────────────┘                            │
│                             │                                               │
│                    ┌────────┴────────┐                                      │
│                    │ _daemon_registry│  (global mutable dict)               │
│                    └─────────────────┘                                      │
│                                                                             │
│   ┌──────────────────────────────────────────────────────────────────┐     │
│   │                    sweep_loop() — every 10s                     │     │
│   │  1. drain_pending() → fetch all 'pending' tasks                 │     │
│   │  2. batch them into ONE conversation prompt                     │     │
│   │  3. run_conversation() via asyncio.to_thread()                  │     │
│   │  4. log turn_messages to MonitoringDB + WebSocket               │     │
│   └──────────────────────────────────────────────────────────────────┘     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        EXTERNAL DEPENDENCIES                                │
│                                                                             │
│   ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐        │
│   │ Hermes AIAgent  │    │ Hermes Tool     │    │  tools.registry │        │
│   │ (run_agent.py)  │    │ Registry        │    │  (global singleton)    │
│   │  - session DB   │    │  - send_peer    │    │                 │        │
│   │  - memory       │    │  - ask_human    │    │                 │        │
│   └─────────────────┘    └─────────────────┘    └─────────────────┘        │
│                                                                             │
│   ┌──────────────────────────────────────────────────────────────────┐     │
│   │  MonitoringDB (SQLite) — events + messages, one shared DB        │     │
│   │  - events table: task_enqueued, state_change, error, etc.        │     │
│   │  - messages table: all assistant/tool/user messages              │     │
│   └──────────────────────────────────────────────────────────────────┘     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Key execution flow:**
```
Agent A wants to message Agent B
    │
    ▼
LLM calls send_peer_message(to_agent=B, message=...)
    │
    ▼
Handler looks up B in _daemon_registry, calls B.ingest_task()
    │
    ▼
Task written to B's SQLite queue as 'pending'
    │
    ▼
B's sweep_loop (every 10s) → drain_pending() → status='processing'
    │
    ▼
B's AIAgent.run_conversation() with batched tasks
    │
    ▼
Response logged → monitoring.db + WebSocket broadcast
    │
    ▼
Tasks marked 'done' (even on error — only logs error, doesn't retry)
```

---

## 2. Critical Structural Problems

### P0 — Two Divergent Server Implementations (swarm_server.py vs test_swarm.py)

**Finding:** There are **two 900+ line server files** that share ~75% identical logic: `AgentDaemon`, `TaskQueue`, tool registration, FastAPI routes, sweep loop, human-in-the-loop handling. They diverged in monitoring infrastructure:

| Feature | `swarm_server.py` | `test_swarm.py` |
|---------|------------------|-----------------|
| Monitoring | SQLite `MonitoringDB` + WebSocket | In-memory `SwarmEventBus` + SSE |
| Message logging | Persistent DB table | Rolling in-memory list |
| Dashboard | WebSocket (`/ws`) | SSE (`/events`) |
| Task batching | `_process_tasks_batch()` | `_process_task()` one-at-a-time |
| Startup | Deletes DBs unconditionally | Deletes DBs unconditionally |
| Version | 0.2.0 | 0.1.0 |

**Why this is a production blocker:** Every bug fix, security patch, or feature addition must be made twice. They will drift further. The `test_race_conditions.py` and `test_dynamic_agents.py` scripts import `test_swarm.py` — they test the *old* implementation while production runs the *new* one.

**Stakes:** A fix in swarm_server.py (e.g., race condition in human_event) is not present in test_swarm.py. Tests pass against the wrong target. Ship-confidence is zero.

**Recommendation:** Delete `test_swarm.py`. Move its unique features (SSE event stream as backup, one-at-a-time processing mode as config option) into `swarm_server.py`. Make `test_*.py` import `swarm_server`.

---

### P0 — Threading/Asyncio Concurrency Mismatch (Confirmed Deadlock Risk)

**Finding:** `AgentDaemon` mixes `threading.Lock` (line 553) and `threading.Event` (line 563) inside an `asyncio.Task` sweep loop. The `_ask_human_handler` (line 476) does a **blocking synchronous wait**:

```python
daemon.human_event.wait()  # BLOCKS FOREVER — no timeout
```

This runs inside `asyncio.to_thread(self._ai_agent.run_conversation, ...)`. The `run_conversation` call dispatches to tool handlers, and `ask_human` blocks the thread. That's fine — `to_thread` moves it off the event loop. But there are three problems:

1. **Starvation under load:** `run_conversation` can take 30-60s per LLM call. With default thread pool (~num_cpus*5), five agents asking human simultaneously exhausts the pool. The sixth agent stalls indefinitely.
2. **Race in state transition:** The `human_response` endpoint (line 929) sets `daemon.human_response` then calls `daemon.human_event.set()`. Between these two lines, the handler could observe `human_response=None` if context-switched. The `state == "asking_human"` check at line 938 is outside the lock too — a duplicate response can slip through (the test at line 88 asserts it returns 400, but this is timing-dependent).
3. **No timeout on human wait:** If the dashboard disconnects or the operator goes home, the agent waits forever. The queue behind it starves.

**Recommendation:** Replace `threading.Event` with `asyncio.Event`, or add a timeout (e.g., 5 minutes) with graceful degradation. Move the state check and response assignment into a single atomic block under the lock. Consider an `asyncio.Condition` for proper async signaling.

---

### P1 — Batch Task Processing Destroys Task Boundaries

**Finding:** `swarm_server.py` `_process_tasks_batch()` (line 684) concatenates all pending tasks into ONE prompt:

```python
combined = f"You have {len(tasks)} new message(s) to process:\n\n"
for i, task in enumerate(tasks, 1):
    combined += f"--- [{i}] from {task['from_agent']} ---\n{task['payload']}\n\n"
```

Then calls `run_conversation` once. The LLM sees N tasks as a single conversation turn. Problems:

- **No per-task failure isolation:** If task 3 of 5 causes the LLM to hit a rate limit or token limit, all 5 fail together. The error is logged but all 5 are silently marked `done` (see `mark_done` at line 753 — it runs even in the `except` block because `mark_done` is only in the `try`).
  - Wait — actually `mark_done` is at line 753 inside the `try`, not the `finally`. So on exception, tasks are NOT marked done. They stay `processing` forever. Zombie tasks.
- **Context pollution:** Task A's output bleeds into Task B's reasoning because they're in the same conversation context.
- **No task-level observability:** The monitoring DB logs one `conversation_complete` event for N tasks. You can't tell which task caused an error.

**Recommendation:** Process tasks individually. The sweep loop should: (1) drain pending, (2) for each task, run a fresh conversation or append it to history and run once. At minimum, add a per-task try/except wrapper so one failing task doesn't kill the batch.

---

### P1 — SQLite Without WAL = Write Contention and Corruption Risk

**Finding:** Every `TaskQueue` opens SQLite with `check_same_thread=False` (line 212, 350) and uses `threading.Lock` for mutex. But there is **no WAL mode**, no `PRAGMA journal_mode`, and no connection pooling. Every operation opens a brand new connection.

Under load:
- Write contention on the monitoring DB (all agents log to one DB concurrently)
- `database is locked` errors when writes collide (sqlite3 busy timeout is 10s, which just blocks — it doesn't queue)
- On crash during write, the DB journal may be left in an inconsistent state

**Recommendation:** Enable WAL mode (`PRAGMA journal_mode=WAL;`). Use a connection pool or at least persistent connections. For the monitoring DB, consider batching writes or using an async SQLite library (`aiosqlite` is already in requirements.txt but not used).

---

### P1 — Global Mutable State is Untestable and Unmockable

**Finding:** The following are module-level globals mutated at runtime:

```python
_main_event_loop: Optional[asyncio.AbstractEventLoop] = None  # line 265
ws_broadcaster = WSBroadcaster()                              # line 326
monitor_db = MonitoringDB(MONITORING_DB)                      # line 259
_daemon_registry: Dict[str, "AgentDaemon"] = {}              # line 442
daemons: Dict[str, AgentDaemon] = {}                          # line 857
AGENTS = load_agents_config()                                 # line 120
```

This means:
- **No parallel tests:** Two test files can't import the module without colliding on `daemons` and `_daemon_registry`
- **No clean shutdown:** A second `on_startup` would append to `daemons` instead of replacing it
- **No config injection:** `AGENTS` is loaded at import time. You can't mock it for testing.
- **State leak between tests:** `test_race_conditions.py` runs `test_swarm.py` as a subprocess specifically because importing it would poison the global state.

**Recommendation:** Wrap global state in a `SwarmApp` class instantiated in startup. Pass dependencies (registry, broadcaster, DB) explicitly. This is a classic "make the change easy, then make the easy change" (Beck) refactor.

---

### P1 — Startup Deletes All Data (Data Loss on Restart)

**Finding:** `on_startup()` (line 1096) unconditionally deletes all queue databases:

```python
for agent_name, cfg in AGENTS.items():
    db_path = WORKSPACE_ROOT / cfg["workspace"] / f"{agent_name}_queue.db"
    if db_path.exists():
        try:
            db_path.unlink()
            log.info("[Startup] Cleaned up previous DB for '%s'", agent_name)
```

This means **every restart loses all in-flight tasks**. In production, a deployment or crash would wipe the queue. The monitoring DB is NOT deleted, so you have orphaned event records pointing to non-existent tasks.

**Recommendation:** Remove the deletion. Add a startup migration that checks for `processing` tasks (from a previous run that crashed) and requeues them as `pending`. This is basic queue durability.

---

### P2 — No Dead-Letter Queue, No Retry, No Backpressure

**Finding:** When `_process_tasks_batch()` throws (line 754-762), the error is logged and broadcast, but:
- Tasks remain in `processing` state forever (no retry)
- There is no DLQ to inspect failed tasks
- There is no circuit breaker — a bad prompt that causes the LLM to error will be retried every 10s forever (actually no, because they stay `processing` forever... so they just stall)
- There is no backpressure — an agent can be flooded with 1000 tasks and will try to batch them all into one prompt, likely exceeding token limits

**Recommendation:** Add a `failed` status and retry counter. After N retries (e.g., 3), move to a dead-letter table. Limit batch size (e.g., max 10 tasks per sweep). Consider token-count preflight before calling the LLM.

---

### P2 — CORS allow_origins=["*"] and No Auth on Management Endpoints

**Finding:** Lines 849-855:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # ...
)
```

And `/agent/{agent_name}/soul` (line 1045) allows updating an agent's system prompt with no authentication. An attacker with network access can:
- Inject malicious tasks into any agent
- Change agent "souls" to exfiltrate data
- Delete/reconfigure agents

**Recommendation:** Restrict CORS to known origins. Add API key middleware at minimum. The `ask_human` endpoint should especially be protected.

---

### P2 — Decorative Port Configuration (peer_port, port fields unused for networking)

**Finding:** Every agent config has `port` and `peer_port` (e.g., 8100, 8101, 8102). But the actual communication is **in-process** via `_daemon_registry` — the `send_peer_message` handler does not open a TCP socket. These ports are purely decorative, yet they appear in the config schema and dashboard.

This creates false confidence about distributed deployment. If someone tries to run agents on separate machines, they will fail silently because the registry only holds in-process references.

**Recommendation:** Either implement actual HTTP-based peer communication (remove `_daemon_registry` dependency) or remove the port fields from config and add a clear comment: "agents are co-resident in this process." If distributed deployment is on the roadmap, design the message bus abstraction now — don't imply it exists.

---

### P2 — No Shutdown Grace Period (Task Mid-flight is Killed)

**Finding:** `on_shutdown()` (line 1119) cancels sweep tasks immediately:

```python
for daemon in daemons.values():
    if daemon._sweep_task:
        daemon._sweep_task.cancel()
```

A sweep in the middle of `run_conversation` (which could be a 30-second LLM call) is cancelled with no grace period. The SQLite transaction may be mid-write. The task is left in `processing` state with no recovery.

**Recommendation:** Implement graceful shutdown: (1) stop accepting new tasks, (2) wait for current batch to complete with a timeout (e.g., 60s), (3) then cancel. Use `asyncio.shield()` or a shutdown event flag.

---

## 3. Operational Risk Assessment

| Risk | Likelihood | Impact | Mitigation Priority |
|------|-----------|--------|---------------------|
| Thread pool exhaustion from human_event.wait() | High | High | P0 |
| Queue data loss on restart | High | High | P0 |
| Batch failure poisons all tasks in batch | Medium | High | P1 |
| SQLite corruption under concurrent writes | Medium | High | P1 |
| Unauthenticated admin access | High | Medium | P1 |
| Task starvation (processing tasks never retried) | Medium | Medium | P2 |
| Global state prevents horizontal scaling | Certain | Medium | P2 |
| Decorative ports mislead ops team | Medium | Low | P2 |

---

## 4. Test Coverage Analysis

**Current tests:** 4 test files, all integration tests that spawn subprocess servers.

| Test File | What It Tests | What It Misses |
|-----------|--------------|----------------|
| `test_swarm.py` | Not a test — it's the server being tested | N/A |
| `test_human.py` | Human-in-the-loop happy path | Timeout path, concurrent human responses, agent crash mid-wait |
| `test_race_conditions.py` | Queue task during asking_human, duplicate responses | The race it's named after is actually NOT tested — it relies on timing, not deterministic ordering |
| `test_dynamic_agents.py` | Add agent, update soul, verify file output | Agent removal, invalid config handling, soul injection attack |

**Critical gaps:**

1. **No unit tests.** Every test starts a subprocess server and polls HTTP. A single test takes 30+ seconds. There are no tests for `TaskQueue`, `MonitoringDB`, `WSBroadcaster`, or `AgentDaemon` in isolation.
2. **No test for batch failure isolation.** If `_process_tasks_batch()` raises, what happens to the 5 tasks? Not tested.
3. **No test for graceful shutdown.** Does the agent finish its current task before exiting? Unknown.
4. **No test for restart durability.** If the server crashes with 3 pending tasks, do they survive? No — startup deletes the DB.
5. **No load test.** What happens with 100 queued tasks? With 5 agents each processing 20 tasks? Unknown.
6. **No test for the monitoring DB.** Does `get_agent_stats()` handle empty DB? Missing agent? Not tested.

**Test Plan Requirements:**

- Add `pytest` + `pytest-asyncio` to requirements
- Write unit tests for `TaskQueue` with an in-memory `:memory:` SQLite DB
- Write unit tests for `MonitoringDB` with mocked time
- Write async unit tests for `WSBroadcaster` using `unittest.mock.AsyncMock`
- Write integration tests using `httpx.AsyncClient` + `TestServer` (Uvicorn's `TestServer`) instead of subprocess
- Add a stress test: enqueue 100 tasks, verify all complete within N seconds
- Add a failure injection test: mock `run_conversation` to raise, verify retry/DLQ behavior

---

## 5. Code Quality & Maintainability Issues

### DRY Violations

- **Two server files** (already covered — this is the worst DRY violation)
- **Duplicate tool registration** (`_register_custom_tools` in swarm_server vs `_register_send_peer_message_tool` + `_register_ask_human_tool` in test_swarm)
- **Duplicate message formatting** (lines 724-743 in swarm_server vs nearly identical block in test_swarm)
- **Duplicate `_send_peer_message_handler`** and `_ask_human_handler` (with slight differences in event logging)

### Naming

- `_SEND_PEER_MESSAGE_TOOL_SCHEMA` — all-caps implies constant, but it's mutated nowhere. Fine, but the leading underscore suggests "private" while it's used across modules.
- `sweep_loop` / `_sweep` — "sweep" is a GC term. In a queue system, "poll" or "drain" is clearer.
- `human_event` / `human_response` — generic names. `human_reply_event` and `human_reply_text` would be clearer.

### Error Handling

- Silent failures everywhere: `log.warning` instead of raising, empty `except: pass` blocks (e.g., line 891 in WebSocket handler, line 155 in event bus). These swallow bugs.
- `except Exception as exc` and then only logging in many places — the caller has no way to know something failed.

---

## 6. Incremental Modernization Strategy

The goal: stabilize to production-grade without a rewrite. Six phases, each independent deliverable.

### Phase 1: Consolidation (Week 1)
**Goal: One source of truth.**

1. Delete `test_swarm.py`
2. Move any test-specific helpers into `conftest.py`
3. Make `test_*.py` import from `swarm_server.py`
4. Extract shared constants, schemas, and defaults into `swarm/config.py`
5. Extract tool handlers into `swarm/tools.py`
6. Create `SwarmApp` class to encapsulate globals

**Acceptance criteria:** `pytest test_*.py` passes with only `swarm_server.py` as the server.

### Phase 2: Concurrency Fix (Week 1–2)
**Goal: Remove deadlock risk.**

1. Replace `threading.Event` with `asyncio.Event` in `AgentDaemon`
2. Add timeout to human waits (configurable, default 300s)
3. Add timeout to `run_conversation` call (configurable, default 120s)
4. Move `state == "asking_human"` check into locked block
5. Add `try/except` around individual task processing in batch

**Acceptance criteria:** Load test with 5 agents all asking human simultaneously — no thread pool exhaustion.

### Phase 3: Queue Durability (Week 2)
**Goal: Survive restarts.**

1. Remove `db_path.unlink()` from startup
2. Add startup recovery: find `processing` tasks, set back to `pending`
3. Add `failed` status and retry counter column
4. Add `max_retries` config (default 3)
5. Add dead-letter queue table or `failed_at` timestamp
6. Cap batch size at configurable limit (default 10)

**Acceptance criteria:** Kill -9 the server mid-task. Restart. Task is requeued and eventually completes.

### Phase 4: SQLite Hardening (Week 2–3)
**Goal: Handle concurrent load.**

1. Enable WAL mode on all SQLite DBs
2. Switch monitoring DB to `aiosqlite` for true async I/O
3. Add connection pooling (or persistent connections)
4. Batch monitoring writes (flush every 1s or 100 events)

**Acceptance criteria:** 10 agents logging 100 events/sec for 60s — no `database is locked` errors.

### Phase 5: Security Hardening (Week 3)
**Goal: Safe to expose.**

1. Add API key middleware to all mutating endpoints
2. Restrict CORS to configured origins
3. Add input validation (max payload size, max soul length)
4. Add rate limiting per IP and per agent

**Acceptance criteria:** Unauthorized request to `/agent/{name}/soul` returns 401.

### Phase 6: Test Suite Rewrite (Week 3–4)
**Goal: Fast, deterministic tests.**

1. Add `pytest` + `pytest-asyncio`
2. Unit tests for `TaskQueue`, `MonitoringDB`, `AgentDaemon`
3. Async integration tests using `httpx.AsyncClient` + Uvicorn `TestServer`
4. Stress test: 100 tasks, 3 agents
5. Failure injection test: mock LLM to raise

**Acceptance criteria:** Full test suite runs in under 60s. No subprocess spawning.

---

## 7. Recommended Immediate Actions (This Week)

1. **Archive `test_swarm.py`** — stop maintaining two servers.
2. **Add task-level try/except** in `_process_tasks_batch()` so one bad task doesn't kill the batch.
3. **Add `timeout` parameter** to `human_event.wait()` (30 minutes max) with automatic state reset.
4. **Remove DB deletion** from startup. Add a `--reset-queues` CLI flag for test runs only.
5. **Enable WAL mode** on all SQLite connections (one line: `PRAGMA journal_mode=WAL;`).

These five changes are small, safe, and dramatically improve reliability. Everything else can follow in the phased plan above.

---

## 8. Architecture Decisions to Revisit

### Decision: Batch vs. Individual Task Processing
**Current:** Batch all pending tasks into one LLM call.
**Tradeoff:** Fewer LLM calls (cheaper) vs. no isolation, context pollution, and token limit risk.
**Question for the team:** Is cost optimization worth the observability loss? If yes, add a "max batch size" and "batch by sender" heuristic. If no, process individually.

### Decision: In-Process vs. Distributed Agent Registry
**Current:** Agents communicate via `_daemon_registry` — purely in-process.
**Tradeoff:** Simple (no networking) vs. can't scale horizontally.
**Question for the team:** Is horizontal scaling on the 6-month roadmap? If yes, add a message bus abstraction (Redis, RabbitMQ, or even HTTP) now. If no, remove the decorative `port`/`peer_port` fields to avoid confusion.

### Decision: SQLite for Monitoring
**Current:** All monitoring events go to one SQLite DB.
**Tradeoff:** Zero infra dependency vs. write bottleneck and query limitations.
**Question for the team:** Will you need to query "agent error rate over the last 24 hours" in a dashboard? SQLite handles this fine for small scale. If you expect >100 agents or long retention, plan for PostgreSQL or a metrics pipeline (Prometheus + Grafana).

---

## 9. Completion Summary

| Area | Findings | Severity |
|------|----------|----------|
| Architecture | Two divergent servers, batch processing breaks boundaries, decorative ports | P0/P1 |
| Concurrency | threading/asyncio mismatch, thread pool exhaustion, race conditions | P0 |
| Data Durability | DB deleted on startup, no WAL, no retry/DLQ | P0/P1 |
| Security | Open CORS, no auth on admin endpoints | P1 |
| Operations | No graceful shutdown, no backpressure, no health probes beyond /health | P2 |
| Testing | Zero unit tests, all subprocess integration tests, 30s+ per test | P1 |
| Code Quality | Heavy DRY violations, global mutable state, silent failures | P1 |

**Most critical (fix this week):**
1. Consolidate to one server file
2. Fix human_event blocking with timeout
3. Stop deleting queues on startup
4. Enable WAL mode

**Estimated effort to production-ready:** 3–4 weeks of focused work, following the six phases above. The core abstractions are sound. The work is stabilization, not redesign.
