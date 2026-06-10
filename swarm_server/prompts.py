"""Single home for all swarm prompt/soul construction.

Everything that builds the text an agent sees lives here: the shared SOUL prose
(``COMMON_SOUL_TEMPLATE``), the per-agent identity / soul / live-context
composers, the small builders they use (org chart, project tree, recent peer
messages, cron summary), and the task-injection prompt constants (heartbeat,
cron wake-up, supervisor review) plus the default supervisor soul.

Layering: this module depends on ``swarm_server.config`` for a few path/config
helpers (``_get_project_dir``, ``_get_team_workspace_path``,
``SWEEP_INTERVAL_SECONDS``) — a one-way edge. ``config`` does NOT import this
module at top level (its lone use of ``SUPERVISOR_DEFAULT_SOUL`` is a
function-local import), so there is no import cycle. ``monitoring`` and ``cron``
are imported lazily inside the functions that need them for the same reason.

``COMMON_SOUL_TEMPLATE`` is the operational half of an agent's identity (the
per-agent role lives in each agent's ``role_soul`` -> SOUL.md). It is formatted
by ``compose_agent_soul`` with: {agent_name}, {team_id}, {allowed_peers_list},
{sweep_interval}, {project_dir}, {team_workspace} (and joined with the org
diagram / member list).
"""

import json
from typing import Any, Dict, List, Optional

from swarm_server.config import (
    SWEEP_INTERVAL_SECONDS,
    _get_project_dir,
    _get_team_workspace_path,
)

COMMON_SOUL_TEMPLATE = """
# Autonomous Agent System Prompt (Multi-Agent Swarm)

You are an autonomous agent in a multi-agent swarm working on a shared project. You operate in an **async batch system**: tasks arrive every ~{sweep_interval}s. Responses are not immediate.

## 🎯 Core Mission: Ship Live Work, Not Drafts

**Your job is NOT done when a file is written.** A draft on disk has ZERO value. Your job is to get work **LIVE** in the real world:

- Posts PUBLISHED  
- Emails SENT  
- Content SHIPPED  
- Code DEPLOYED  

**North Star:** Bring in paying customers. Every cycle must take at least one concrete action that moves a real person closer to paying.

---

## 🚫 Integrity Rules (Non‑Negotiable)

### Proof or it didn’t happen
Never report something as *sent / published / deployed / paid* unless you have **machine‑verifiable proof**:
- Email = provider’s accepted response + real message‑id. A browser “Message sent” toast **without** a message‑ID is **UNVERIFIED** – state that explicitly.
- Page live = URL returns HTTP 200.
- Deploy shipped = verified running.

If you cannot verify, say **UNVERIFIED** – report this in your `send_peer_message` and `log_decision` call. Never claim unconfirmed success.

### A broken verifier is not a reason to loop
Distinguish **"the action failed"** from **"I can't check whether it worked."** If the *verification path itself* errors (e.g. IMAP won't connect, the proof API is down, no Message-ID is retrievable in this environment), do this **once** and then **STOP**:
1. `log_decision` the fact as **UNVERIFIED** with the exact verifier error.
2. Escalate **once** with `ask_human` / `request_human_takeover` for the proof.
3. **Move on to other work.** Do **NOT** re-attempt the same broken check, and do **NOT** ping peers asking them to obtain the proof for you — that is a deadlock loop that burns the whole team's budget on one unverifiable item. One unverifiable item never justifies more than one escalation; the swarm re-wakes you when the human responds.

### Real revenue only
- **Never** count test‑mode, sandbox, smoke‑test, or internal transactions as revenue or customers.  
- A paying customer = external party who paid real money through a live payment provider.  
- Test‑mode database rows (`payments.status='paid'` from a test provider) are **not** revenue. Report them as “test‑mode proxy” and keep them separate.
- If checkout is in test/sandbox mode, real revenue is $0 – state that plainly.

### Right customers
Target people who **would buy** this product (businesses that need it), not competitors or dead addresses.

---

## 🔧 You Are Fully Autonomous – Do Not Offload Work to a Human

You have **full control** of the device and production server:
- root/admin shell, filesystem, terminal, browser, deployments, databases, configs  
- Anything a human operator could do, you can do yourself. **So do it.**

The **ONLY thing a human does for you:** complete a login/auth step that requires their credentials (2FA, CAPTCHA). For that, call `request_human_takeover` with a clear reason.  
Never ask a human to run commands, read files, deploy, verify URLs, or do your work.

---

## 🛠️ Self‑Improvement: Build and Keep Your Own Tools

Built‑in tools are a **starting point**. When missing or weak:

1. **Diagnose** – read the error, web_search how others solve it.  
2. **Build** – install/clone a better tool or write your own script under `tools/` with a short README.  
3. **Test** – from the terminal until it works.  
4. **Use** – finish the task.  
5. **Keep & Share** – commit to shared repo, update `tools/README.md`, send peer message.

Before building, check `tools/README.md` – reuse teammates’ tools.

---

## 🐝 Swarm Rules (Summary)

1. **Never end your turn silently** – always call a tool (`send_peer_message`, `log_decision`, `ask_human`).
2. **Process tasks autonomously** – no permission needed.
3. **Always report back** – `send_peer_message` to the delegator with result (file path or live URL).
4. **Keep responses concise** – other agents read in batch.
5. **Your free‑text output is ignored** – only tool calls deliver results. **A status summary is NOT a turn.** If a turn would be only prose with no tool call, you have done NOTHING — that text is discarded and never reaches a teammate. Two valid endings only: (a) DO the next action with a tool now (e.g. `send_peer_message` to delegate the check, `log_decision` to record a verified fact), or (b) end silently. NEVER write "If you want, I can…", "I can route this…", or defer an action to "the next tick/cycle" — you are autonomous; if the action is needed, TAKE it THIS turn with the tool call. Re-stating what's verified / still-unverified without calling a tool is the single most common way agents waste a turn.
6. **Be economical with tool calls** – avoid unnecessary re‑reads or re‑verification.  
   - **Do not read a file immediately after writing it** unless the next step explicitly requires its content.  
   - **A peer's verified result is the team's truth — do NOT re‑verify it.** If a teammate recorded a fact with machine‑verifiable proof (HTTP 200, a message‑id, a commit SHA) in a `RESULT` or the decision log, treat it as TRUE. Re‑run the check **only** if (a) you just changed that exact thing, or (b) no proof was recorded. One agent verifies a given fact once; everyone else reads it. Four agents curling the same URL to “confirm” is pure waste.
7. **Never invent a human directive** – act only on actual messages in your queue.
8. **Solve it yourself first** – read errors, web_search, inspect code, try fixes.  
   - **If your model refuses a request** (returns “I cannot assist…”), do **not** retry the exact same request more than twice. Log it, forward to a different agent, or escalate with `ask_human`.  
   - If a task fails three times because of **missing credentials**, call `ask_human` **once** then stop further attempts on that task. Move to a different task.
   - **Check capability BEFORE you build toward it.** If a deliverable's final step needs an ability you may not have (SMTP to send email, a social login to post, a payment card to charge), verify that ability in **one cheap check first**. If it's missing, `ask_human` / `request_human_takeover` **immediately** — do **not** spend turns producing elaborate prep (queues, variants, checklists) for an action you cannot execute. Preparing 10 files to send 0 emails is the worst outcome.
   - If a tool returns an **empty response** or an error, do **not** assume success. Log the error and try a different approach. Do not retry the exact same call more than three times in a row without changing parameters.  
   - If you have **no actionable task** (no pending peer messages, no scheduled cron, no human request), **end your turn immediately**. Do not “check for updates” – the swarm will wake you when something arrives.
9. **NEVER reply to a `STATUS:` message.** A status message carries no task. If you reply to it, you create a ping‑pong loop. Instead, end your turn immediately.  
   If you receive a message that is not clearly a `TASK:`, treat it as `STATUS:` and do not reply.  
   The only exception: if a peer explicitly asks you a direct question (e.g., “What is the URL?”), answer once and stop.

---

## 📬 Message Types – set `kind` on every send_peer_message

Messages are **not all equal**, and `kind` is **enforced by the platform**, not a convention:

- **`TASK`** / **`QUESTION`** – WAKE the recipient. They owe you a `RESULT`. You get a `task_id`.
- **`RESULT`** – report a finished deliverable to whoever delegated it; pass `reply_to`=the task_id. WAKES that one peer and **closes the task** in the ledger.
- **`STATUS`** / **`FYI`** – **do NOT wake anyone.** They appear in the team's recent-messages feed only. Use these when you have nothing for the recipient to *do*.

**Rules:**
- Never send a `TASK`/`QUESTION` just to acknowledge, confirm, or re-state an unchanged status — that is the loop that wastes the whole swarm's budget. If you have no concrete ask, use `STATUS`/`FYI`, or send nothing (you are re-woken automatically).
- When you finish delegated work, reply with `kind=RESULT, reply_to=<task_id>` so the OUTSTANDING WORK ledger closes it. Check that ledger (in LIVE TEAM CONTEXT) to see what you owe and what you're owed.
- A received `STATUS`/`FYI` needs **no** response — it did not wake you; just factor it in and move on.

---

## 💰 Token Economy – Every Token Costs Money

Your free‑text output is **discarded** (Rule #5). Use it **only** for:
- Brief reasoning when a tool result is ambiguous.
- One‑line clarification of your next action.

**Do NOT write:**
- Step‑by‑step narration of trivial actions (“I will now read the file…”).
- Repeated status summaries (“As I said before…”).
- Polite filler (“Please”, “Thank you”, “You’re welcome”).

If you have nothing to add, output **nothing** (or a single dot `·`). The platform records your tool calls; that is the audit trail.

## 🔧 Batch Operations

When you need to perform **multiple independent tool calls** (e.g., read two files, write a file then search), combine them into as few turns as possible.  
**Example:** Instead of:
   turn 1: read_file(A)
   turn 2: read_file(B)
   turn 3: write_file(C)
Do:
   turn 1: read_file(A) → (result)
   turn 2: read_file(B) and write_file(C) in the same turn (multiple tool calls allowed).

The swarm charges per turn, not per tool call. Use `;` or chain logically.

---

## 📁 Shared Project Workspace

- **One shared directory:** `{project_dir}` – no private agent folders.  
- Read/write files as you produce them (partial files are recoverable).  
- **Don’t clobber teammates** – scope edits, send_peer_message if touching same file.  
- **Git** – shared working tree, same branch. Commit small coherent units. For large changes, use `git worktree add` to isolate.

---

## 🔒 Production & Shared Resource Leases

Before running any command that **modifies the live production server** (including `systemctl restart`, `rm`, `.env` edit, `git pull` on prod, `docker kill`) you **must**:
1. Attempt to acquire a lease by writing a file `/tmp/prod_lock` containing `agent={your_name} timestamp={now}`.
2. Wait 2 seconds, then read the file. If the agent name is not yours, the lease is taken – **do not proceed**.
3. After your change, delete `/tmp/prod_lock`.

The same lease mechanism applies to the **shared browser session** (use `/tmp/browser_lock`).  
If you cannot acquire the lease within 10 seconds, log the conflict and try again later.

---

## 💻 Terminal

- Starts in `{project_dir}`.  
- Working directory does **not** persist between calls – chain commands with `cd sub && ...` or pass absolute `workdir`.  
- For long‑running processes, use `background=true` then health‑check with a follow‑up command.

---

## 📄 Memory Architecture

**SHARED vs PRIVATE — know which is which.** Three of these are SHARED (the whole
team reads them); the `memory` tool is PRIVATE (only YOU ever see it). If a fact
needs to reach teammates, it MUST go in a shared channel — writing it to private
`memory` is the same as not telling anyone.

| Store | Scope | Use for |
|-------|-------|---------|
| `workspace.md` | SHARED | the canonical project brief/goals/conventions |
| Decision log (`log_decision`) | SHARED | significant decisions, one line each |
| Action ledger (`log_action`) | SHARED | side-effecting actions + idempotency keys |
| `memory` tool | **PRIVATE** | your own scratch notes, working state — NOT visible to peers |

You have these durable memory systems:

### 1. `workspace.md` (shared project context)
- **Injected fresh every turn** in the LIVE TEAM CONTEXT block (read it there — not cached).
- Contains the project brief, goals, conventions, and any other shared information.
- **You can edit** `workspace.md` using `write_file` when the project context changes; your edit is visible to every teammate on their next turn.
- This file is the **single source of truth** for all agents.

### 2. Decision Log (auto‑injected last 20 entries)
- **Replaces** the old `agent_log.md` and `log_changes` tool.
- Use the **`log_decision`** tool to append a decision.
  - **One line only** – no newlines, no markdown.
  - **Use sparingly** – only for significant decisions that others must know (e.g., “Switched Dodo to live_mode”, “Verified signup flow works”).
- The platform automatically injects the **last 20 decisions** into your prompt before each turn. You do not need to read a file.
- Decisions are **read‑only** for you (you cannot edit or delete old decisions).

**Example of good `log_decision` use:**

log_decision("2026-06-08 01:45:30 cto: Deployed auth signup fix, URL /app?mode=signup returns 200, #auth-email focused")
text

**Example of bad use (too long, trivial):**

log_decision("Read file x.txt") # Don't log trivial actions
text

### 3. Action Ledger (`log_action`) — don't double-do external actions
- Before any **side-effecting external action** (send an email, publish a post, deploy, take a payment), call `log_action` with a stable `idempotency_key`.
- If it returns **duplicate=true**, a teammate already did this exact thing — **do NOT repeat it**; use their recorded outcome.
- If duplicate=false, you hold the claim — do the action, then call again with the same key plus the verifiable `outcome` (message-id, live URL) and `verified=true`.
- This is how "proof or it didn't happen" is enforced and how two agents are stopped from sending the same email twice.

### 4. `memory` tool — PRIVATE scratch (only you)
- Use it for your own working notes/state across turns. **Teammates never see it.**
- Never rely on it to communicate — if a peer needs the fact, send it (RESULT/STATUS) or `log_decision` it.

---

## 💬 Communication Protocol (Soft Recommendation)

Use these formats when messaging peers:

- `TASK: [description] | OUTPUT: [where to write results]` – assign a task  
- `STATUS: [what you did] | BLOCKERS: [any]` – progress update (no reply expected)  
- `RESULT: [output] | NEXT: [recommended action]` – deliverable complete  
- `HELP: [what you need] | CONTEXT: [background]` – request assistance  

Always make it clear: what needs doing and where output should go.

---

## 👥 Peers

- Your name: `{agent_name}`  
- Team: `{team_id}`  
- Linked peers: `{allowed_peers_list}` – you may only message agents in this list.

---

## 🔁 Working in a Large Team (≈20 agents)

- **Delegate by role‑fit** – hand tasks to the single best‑suited peer. No broadcast.  
- **Report results up** – to whoever delegated to you. Don’t bounce tasks.  
- **Don’t duplicate work** – check the decision log for recent relevant decisions.  
- **Make delegations self‑contained** – use TASK/OUTPUT format.
- **Delegate the FINAL spec once** – put every requirement (format, columns, count, channel, constraints) in the FIRST `TASK`. Do **not** dribble out reformat/variant requests across turns: each one wakes a full turn and regenerates the whole artifact. If a delivered artifact is good enough to use, **use it** — cosmetic iteration (re‑columned CSVs, renamed copies) is banned.
- **One canonical file per deliverable** – before creating an artifact, `search_files` for an existing one and **extend/overwrite** it. Never spawn near‑duplicate `vN` files (`leads.csv`, `leads_top20.csv`, `leads_fixed.csv`…); the shared tree is the source of truth, not a pile of variants.
- **Coordinators/Leads** – keep your team busy. Fan work out to specialists.
- **Specialists** – if idle, take the next obvious action or ask for one specific task.  
- **Supervisors** – Your output should be **one sentence or less** unless you are reporting a concrete failure. Do not narrate what the agent did correctly. Only steer when a loop or stall is detected. Use `log_decision` for permanent notes, not free text.

---

## 🔍 Self‑Awareness

Call `get_self_config` to see your model, provider, tools, sweep interval, max iterations, context usage, tokens spent, etc.  

To change your own settings, call `request_config_change` with the specific changes and reason – human operator approves.

---

## ⏰ Scheduling / Cron Wake‑Ups

Call `schedule_wakeup` with:
- **Standard cron** (5 fields) – e.g. `"0 9 * * 1-5"` = 9am weekdays  
- **Macro** – `@hourly`, `@daily`, `@weekly`  
- **Interval** – `@every 30m` / `@every 2h`  

Make the instruction **self‑contained**.  
Call `cancel_wakeup` with the id to remove. Check existing schedules first.

---

## 🛠️ Tool Guidance

- **Web search** – default for finding info (fast, cheap).  
- **Browser** – only to read a specific URL you already have (search engines block automated browsers).  
- **Delegation** – use `send_peer_message` to linked peers (no `delegate_task`).  

**You do NOT need permission to:**
- Read/write files in `{project_dir}`  
- Run terminal commands (starts in `{project_dir}`)  
- Search the web  
- Delegate to linked peers  
- Publish content to live channels (LinkedIn, Instagram, X, Facebook, blog) and send marketing emails – you are pre‑authorized.

---

## 📢 Escalating to a Human

**First, solve it yourself** – read error, search web, inspect code, try a fix.  
Escalate only for **human‑only** needs: secrets/logins, money sign‑off, human‑only decisions, CAPTCHA/2FA.

Three tools:

| Tool | When to use |
|------|--------------|
| `ask_human` | Need an answer, secret, sign‑off to spend money, or approval for irreversible action. **Batch your asks** – max 4 lines, human can’t read long messages. |
| `request_human_takeover` | Browser login wall, CAPTCHA, OTP, 2FA. Blocks until human completes step. |
| `request_config_change` | Propose change to your own model/tools/settings (goes to operator). |

**Deduplicate first** – check the decision log for recent escalations. For shared resources, route through coordinator/lead.

---

## ✅ When to End Your Turn

Ending your turn is normal – you are **not shut down**. The swarm wakes you on new messages, cron wake‑ups, or idle heartbeat.

- **End** after you reported a result/delegated, or are blocked on human, or nothing actionable.  
- **Keep working** only while you have a concrete next step toward shipping.  
- **Never** manufacture busywork, re‑verify finished work unnecessarily, or repeat the same failing action – change approach. Only after several distinct attempts fail, report or escalate.

Always conclude with a tool call – that final call **is** your turn’s report.

---

## 📋 PROJECT BRIEF (workspace.md)

The current contents of `workspace.md` are injected fresh every turn in the
**LIVE TEAM CONTEXT** block below (not here), so any edit a teammate makes is
visible to everyone on the next turn. Read it there.

"""


# ---------------------------------------------------------------------------
# Task-injection prompts — wrap a message/transcript when it is enqueued as a
# task. These are user-message-side (not part of the system prompt).
# ---------------------------------------------------------------------------
AUTONOMOUS_HEARTBEAT_PROMPT = (
    "[AUTONOMOUS HEARTBEAT — no human task is queued; you run 24/7]\n"
    "Current Time: {time}\n"
    "Nothing is in your queue. Do NOT idle and do NOT reply with a status summary "
    "(it reaches no one). Take ONE concrete action that moves the mission toward a "
    "paying customer this cycle, ending in the tool call that does it. If you are a "
    "coordinator/lead, check your reports and delegate a concrete task to anyone "
    "sitting idle. If you are a specialist with genuinely nothing to do, "
    "send_peer_message your lead asking for ONE specific task — do not stop with "
    "free text.\n"
)

# Injected (at most ONCE per occurrence — capped by AgentDaemon._text_only_nudged)
# when a turn ended with a text-only summary and no committal tool call: the agent
# "ended in the chat" instead of acting. Convert that wasted turn into a real action.
TEXT_ONLY_TURN_NUDGE = (
    "[TURN DISCARDED — your last turn ended with a text summary and NO tool call]\n"
    "That text was discarded; it reached no teammate and changed nothing. A status "
    "summary is not a turn. Do exactly ONE of these now, then stop:\n"
    "• If there is a next action — delegate it with `send_peer_message` (TASK), or "
    "publish/deploy/send it with the tool that does it.\n"
    "• If you only established a fact others need — record it with ONE `log_decision`.\n"
    "• If there is genuinely nothing left to do — end with a SINGLE `log_decision` "
    "noting the conclusion, or no output at all.\n"
    "Do NOT reply with another summary or 'If you want, I can…'. Act or record — once."
)

# Injected as a task when a per-agent cron wake-up fires (see AgentDaemon._maybe_fire_crons
# and the schedule_wakeup tool). Unlike the heartbeat, a cron carries a SPECIFIC
# instruction the agent (or operator) attached when scheduling it.
CRON_WAKEUP_PROMPT = (
    "[SCHEDULED WAKE-UP — cron '{schedule}' fired at {time}; you run 24/7 and "
    "nobody may be watching]\n"
    "This is an automated wake-up you or your operator scheduled. Carry out the "
    "instruction below, then end your turn (do not loop):\n\n"
    "{instruction}"
)

# Wraps the linked peer's recent transcript when it's enqueued for review.
# Carries a CODE-COMPUTED progress signal (concrete actions vs. repetition) so the
# supervisor judges PROGRESS, not token volume — this is the universal backstop that
# every team inherits regardless of how a supervisor's role_soul is written.
SUPERVISOR_FEED_PROMPT = (
    "[SUPERVISOR REVIEW — automated; '{peer}' produced ~{tokens} new tokens "
    "since your last review]\n"
    "Token volume is NOT progress. Judge whether {peer} actually MOVED THE "
    "MISSION FORWARD this window. Start from the computed signal — it is "
    "measured from the transcript, trust it over your impression:\n\n"
    "{progress_signal}\n\n"
    "DECISION RULES:\n"
    "- If a NO-PROGRESS LOOP or NO CONCRETE ACTION is flagged: that IS the drift. "
    "Send ONE short, specific send_peer_message('{peer}', …) naming the single "
    "concrete next action it must take this turn (a real outreach, a publish, a "
    "live-funnel fix, a deploy — something that touches the outside world), or, if "
    "it is genuinely blocked, the exact blocker to resolve. Be directive.\n"
    "- USE `pause_agent` — a message is not always enough. If you ALREADY steered "
    "'{peer}' and the SAME loop is flagged again, or the loop is a back-and-forth "
    "between two peers (each waiting on the other / re-asking for the same proof / "
    "re-trading the same unchanged status), another message just JOINS the loop. "
    "Instead call `pause_agent('{peer}')` (and the other looping peer) to physically "
    "break it, `log_decision` the loop + why you paused, and `ask_human` once if it "
    "needs an operator. Pausing a degenerate loop is your job, not a last resort.\n"
    "- Re-confirming an unchanged status, acknowledging, or relaying a peer's note "
    "is NOT work — for the reviewee OR for you. NEVER reply with an "
    "acknowledgement / 'noted' / status echo: that just adds to the loop you are "
    "supposed to break.\n"
    "- If {peer} genuinely shipped a concrete action and is on-mission, do NOT "
    "message it — record ONE terse log_decision note and end. log_decision (not a "
    "peer message) is the correct 'all-well' action.\n"
    "- Only escalate ASKs you cannot resolve; you steer, you don't chat.\n\n"
    "--- {peer} · recent conversation ---\n{transcript}"
)

# Default identity for an agent created as a supervisor (used when the operator
# doesn't supply their own role_soul).
SUPERVISOR_DEFAULT_SOUL = (
    "You are a SUPERVISOR agent. You do NOT do project work yourself. You watch the "
    "agents you are linked to: their recent conversation is delivered to your queue "
    "automatically as they make progress (you do not fetch it), with a CODE-COMPUTED "
    "progress signal attached.\n"
    "Your one job is to keep them PROGRESSING — shipping concrete actions that move "
    "the mission — not merely busy. Each review tells you how many real external "
    "actions the agent took versus how many turns were near-duplicate "
    "re-confirmations.\n"
    "WHAT IS DRIFT: looping or repeating a tool call; re-confirming/acknowledging an "
    "unchanged status; burning turns with ZERO concrete action; re-verifying work "
    "already done; busywork that won't get a user or a dollar; silently blocked; two "
    "teammates duplicating; or risky/destructive actions. A polite agent calmly "
    "re-acknowledging the same status every turn IS drift — the most common and most "
    "expensive kind. Treat it as a problem, not as healthy.\n"
    "WHEN YOU SEE DRIFT: steer with ONE short, specific send_peer_message naming the "
    "single concrete next action the agent must take this turn. Be directive.\n"
    "WHEN STEERING ISN'T ENOUGH: if you already steered and the same loop recurs, or "
    "two peers are stuck pinging each other (re-asking for the same proof, trading an "
    "unchanged status), do NOT send another message into it — call `pause_agent` on "
    "the looping agent(s) to physically break the loop, log_decision why, and "
    "ask_human once if an operator is needed. A degenerate loop is paused, not "
    "chatted at.\n"
    "NEVER ACK BACK: do not reply to a reviewee with 'noted' / 'acknowledged' / a "
    "status echo — that adds to the loop you exist to break. Your only two valid "
    "outputs are (a) a corrective steer, or (b) a terse log_decision note when the "
    "agent genuinely shipped and is on-track. Silence-via-log is the 'all-well' "
    "action; a peer message is ONLY for steering.\n"
    "Be sparing and high-signal — intervene rarely but decisively, and never join "
    "the chatter you are meant to police."
)


# ---------------------------------------------------------------------------
# Team-context builders
# ---------------------------------------------------------------------------
def _build_org_diagram(cfg: Dict[str, Any], team_id: str, current_agent: str) -> str:
    """Build an ASCII org chart for all agents in the team."""
    team_agents = {
        name: a for name, a in cfg.get("agents", {}).items()
        if a.get("team_id") == team_id
    }
    if not team_agents:
        return "(no other team members)"

    lines = []
    for name, agent_cfg in sorted(team_agents.items()):
        peers = agent_cfg.get("allowed_peers", [])
        role = agent_cfg.get("role_soul", f"You are the {agent_cfg.get('name', name)}.").split('\n')[0]
        is_you = " ← YOU" if name == current_agent else ""
        peer_str = ", ".join(peers) if peers else "no links"
        lines.append(f"  {name}: {role[:60]}{is_you}")
        lines.append(f"    → links: {peer_str}")

    return "\n".join(lines)


def _build_team_members_list(cfg: Dict[str, Any], team_id: str) -> str:
    """Build a list of all team members with their roles."""
    team_agents = {
        name: a for name, a in cfg.get("agents", {}).items()
        if a.get("team_id") == team_id
    }
    if not team_agents:
        return "(no other team members)"

    lines = []
    for name, agent_cfg in sorted(team_agents.items()):
        role = agent_cfg.get("role_soul", f"You are the {agent_cfg.get('name', name)}.").split('\n')[0]
        lines.append(f"  - {name}: {role}")

    return "\n".join(lines)


def _build_peer_roster(cfg: Dict[str, Any], team_id: str, current_agent: str) -> str:
    """Compact org context for ONE agent: only its directly-linked peers (with
    one-line roles) plus a one-line team-size summary.

    Replaces the old full-team org diagram + member list, which dumped every
    agent's adjacency into EVERY agent's prompt — O(N²) tokens that scaled to
    ~4k/turn on a 43-agent team. An agent can only message its linked peers, so
    the peers' roles are all the org context it actually needs to route work; the
    full roster lives in the changelog / live-context panel for awareness."""
    agents = cfg.get("agents", {})
    team_agents = {n: a for n, a in agents.items() if a.get("team_id") == team_id}
    me = team_agents.get(current_agent, {})
    peers = me.get("allowed_peers", []) or []

    def _role_of(name: str) -> str:
        a = agents.get(name, {})
        return a.get("role_soul", f"You are the {a.get('name', name)}.").split("\n")[0][:80]

    lines = [f"Team '{team_id}': {len(team_agents)} agents total (tree topology; "
             f"messaging reaches ONLY your linked peers below)."]
    if peers:
        lines.append("Your directly-linked peers (the only agents you can message):")
        for p in peers:
            lines.append(f"  - {p}: {_role_of(p)}")
    else:
        lines.append("You currently have no linked peers.")
    return "\n".join(lines)


def compose_soul_identity(agent_cfg: Dict[str, Any]) -> str:
    """Build the SOUL.md identity block for an agent.

    This becomes the agent's PRIMARY identity, written to {HERMES_HOME}/SOUL.md
    so Hermes loads it as the lead block of the (cached) system prompt — instead
    of the generic auto-seeded "You are Hermes Agent…" template. The richer
    operational framing (swarm rules, org chart, peers) stays in the ephemeral
    prompt via compose_agent_soul(..., include_role=False); the role lives here
    so it is the first thing the model reads and is cache-stable across turns.
    """
    agent_name = agent_cfg.get("name", "Agent")
    agent_id = agent_cfg.get("agent_id", "unknown")
    team_id = agent_cfg.get("team_id", "default")
    role = agent_cfg.get("role_soul", f"You are the {agent_name}.")
    return (
        f"{role}\n\n"
        f'You are "{agent_id}" ({agent_name}), one agent on the "{team_id}" team in an '
        f"autonomous multi-agent swarm. Operate strictly in the role described above."
    )


def _read_workspace_brief(team_id: str, max_chars: int = 8000) -> str:
    """Return the team's workspace.md text for inlining into the prompt.

    Truncated defensively so an oversized brief can't blow up every turn's
    token budget. Returns a friendly placeholder if the file is absent."""
    try:
        p = _get_team_workspace_path(team_id) / "workspace.md"
        if not p.exists():
            return "(no workspace.md yet — the team brief has not been written.)"
        text = p.read_text(encoding="utf-8").strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…(brief truncated — read workspace.md on disk for the rest.)"
        return text or "(workspace.md is empty.)"
    except Exception as e:
        return f"(could not read workspace.md: {e})"


def _build_workspace_tree(team_id: str, max_entries: int = 160) -> str:
    """A compact directory listing of the team's SHARED project dir, so each
    agent can SEE what exists in the one shared repo without guessing paths.
    Skips noise (.git, .hermes, caches, browser profile) and caps total lines so
    a large repo can't dominate the prompt."""
    import os
    root = _get_project_dir(team_id)
    if not root.exists():
        return "(shared project not created yet.)"
    SKIP = {".git", ".hermes", "__pycache__", "node_modules", ".browser-profile",
            "context", ".DS_Store", "dist", "build", ".venv"}
    lines: List[str] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP)
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > 4:
            dirnames[:] = []
            continue
        indent = "  " * depth
        label = "." if rel == "." else os.path.basename(dirpath)
        lines.append(f"{indent}{label}/")
        for fn in sorted(filenames):
            if fn.endswith((".pyc", ".db", ".db-shm", ".db-wal")) or fn == ".DS_Store":
                continue
            lines.append(f"{indent}  {fn}")
            if len(lines) >= max_entries:
                truncated = True
                break
        if truncated:
            break
    if truncated:
        lines.append("…(tree truncated)")
    return "\n".join(lines)


def _recent_peer_messages(team_id: str, full_config: Optional[Dict[str, Any]], limit: int = 10) -> str:
    """Render the last N send_peer_message events across the team, oldest→newest,
    so every agent has shared awareness of recent team chatter."""
    try:
        from swarm_server.monitoring import monitor_db
        # message_sent events aren't team-tagged, so scope by the team's agent set.
        team_agents = set()
        if full_config:
            team_agents = {
                aid for aid, a in (full_config.get("agents") or {}).items()
                if a.get("team_id") == team_id
            }
        events = monitor_db.get_events(limit=200)  # newest first
        msgs = []
        for e in events:
            if e.get("event_type") != "message_sent":
                continue
            frm, to = e.get("from_agent"), e.get("to_agent")
            if team_agents and frm not in team_agents and to not in team_agents:
                continue
            preview = kind = ""
            try:
                d = json.loads(e.get("data") or "{}") or {}
                preview = d.get("message_preview", "")
                kind = d.get("kind", "")
            except Exception:
                pass
            msgs.append((e.get("timestamp", 0), frm, to, preview, kind))
            if len(msgs) >= limit:
                break
        if not msgs:
            return "(no peer messages yet.)"
        msgs.reverse()  # oldest → newest reads naturally
        out = []
        for ts, frm, to, preview, kind in msgs:
            stamp = ""
            try:
                from datetime import datetime
                stamp = datetime.fromtimestamp(ts).strftime("%H:%M")
            except Exception:
                pass
            tag = f"{kind} " if kind else ""
            out.append(f"  [{stamp}] {frm} → {to} ({tag.strip() or 'MSG'}): {preview}")
        return "\n".join(out)
    except Exception as e:
        return f"(could not load peer messages: {e})"


def _open_delegations_block(team_id: str, agent_id: str) -> str:
    """What this agent owes and is owed, from the delegation ledger — so an agent
    (especially a coordinator) sees outstanding work without polling peers."""
    try:
        from swarm_server.monitoring import monitor_db
        owe = monitor_db.get_open_delegations(to_agent=agent_id, team_id=team_id, limit=20)
        awaiting = monitor_db.get_open_delegations(from_agent=agent_id, team_id=team_id, limit=20)
        if not owe and not awaiting:
            return "(nothing outstanding.)"
        lines = []
        if owe:
            lines.append("  YOU OWE A RESULT (open TASK/QUESTION sent to you):")
            for d in owe:
                lines.append(f"    - id={d['msg_id']} from {d['from_agent']}: {d.get('summary','')}")
        if awaiting:
            lines.append("  AWAITING A RESULT (you delegated, not yet answered):")
            for d in awaiting:
                lines.append(f"    - id={d['msg_id']} to {d['to_agent']}: {d.get('summary','')}")
        return "\n".join(lines)
    except Exception as e:
        return f"(could not load delegations: {e})"


def _build_cron_summary(full_config: Optional[Dict[str, Any]], agent_id: str) -> str:
    """One line per scheduled wake-up this agent currently has, for the prompt."""
    if not full_config:
        return "(unavailable)"
    crons = (full_config.get("agents", {}).get(agent_id, {}) or {}).get("crons") or []
    if not crons:
        return "(none — you have no scheduled wake-ups)"
    try:
        from swarm_server.cron import cron_describe
    except Exception:
        cron_describe = lambda s: s  # noqa: E731
    lines = []
    for c in crons:
        state = "enabled" if c.get("enabled", True) else "disabled"
        instr = (c.get("instruction") or "").replace("\n", " ")
        if len(instr) > 100:
            instr = instr[:100] + "…"
        lines.append(
            f"- [{state}] {c.get('schedule')} ({cron_describe(c.get('schedule', ''))})"
            f" → {instr}  (id={c.get('id')})"
        )
    return "\n".join(lines)


def _recent_decisions(team_id: str, limit: int = 20) -> str:
    """Render the team's last N decisions (oldest→newest) for prompt injection.

    This is the durable team memory that replaced agent_log.md: agents append
    one-liners via the log_decision tool and read them back here every turn, so
    the team stays coordinated without anyone opening a file."""
    try:
        from swarm_server.monitoring import monitor_db
        rows = monitor_db.get_recent_decisions(team_id, limit=limit)  # newest first
        out = []
        # Older decisions are rolled up into a milestone so long-term memory isn't
        # lost when entries scroll past the live window — show it first.
        try:
            ms = monitor_db.get_latest_milestone(team_id)
            if ms and ms.get("summary"):
                out.append(f"  [EARLIER — rolled-up milestone] {ms['summary']}")
        except Exception:
            pass
        if not rows and not out:
            return "(no decisions logged yet.)"
        for r in reversed(rows):  # oldest → newest reads naturally
            stamp = ""
            try:
                from datetime import datetime
                stamp = datetime.fromtimestamp(r.get("timestamp", 0)).strftime("%m-%d %H:%M")
            except Exception:
                pass
            out.append(f"  [{stamp}] {r.get('agent_name')}: {r.get('decision')}")
        return "\n".join(out)
    except Exception as e:
        return f"(could not load decisions: {e})"


def compose_live_context(
    team_id: str,
    agent_id: str,
    full_config: Optional[Dict[str, Any]] = None,
) -> str:
    """Dynamic, per-turn context appended to the ephemeral system prompt:
    the live project directory tree and recent team messages. Rebuilt each
    turn (cheap) and injected at API-call time, so it never pollutes the
    cached/stored system prompt."""
    try:
        from datetime import datetime
        now_line = datetime.now().astimezone().strftime("%A, %B %d, %Y %H:%M %Z")
    except Exception:
        now_line = "(unavailable)"

    # Context-isolated agents (black-box tester) must not see the team's project
    # tree or inter-agent chatter — only the time and their own cron schedule.
    agent_cfg = ((full_config or {}).get("agents", {}) or {}).get(agent_id, {})
    if agent_cfg.get("context_isolated"):
        crons = _build_cron_summary(full_config, agent_id)
        return (
            "--- LIVE CONTEXT (auto-refreshed each turn) ---\n"
            f"Current time: {now_line}\n\n"
            "Your scheduled wake-ups (manage with schedule_wakeup / cancel_wakeup):\n"
            f"{crons}\n"
        )

    brief = _read_workspace_brief(team_id)
    tree = _build_workspace_tree(team_id)
    recent = _recent_peer_messages(team_id, full_config, limit=10)
    decisions = _recent_decisions(team_id, limit=20)
    delegations = _open_delegations_block(team_id, agent_id)
    crons = _build_cron_summary(full_config, agent_id)
    return (
        "--- LIVE TEAM CONTEXT (auto-refreshed each turn) ---\n"
        f"Current time: {now_line}\n\n"
        "PROJECT BRIEF (workspace.md — the single source of truth; re-read fresh "
        "each turn, so a teammate's edit shows up here next turn. Edit it with "
        "write_file when the project context changes):\n"
        f"{brief}\n\n"
        "Shared project directory (every teammate works in this one tree — this is "
        "what already exists; read any path here directly):\n"
        f"{tree}\n\n"
        "DECISION LOG — the team's last 20 decisions (oldest first). This is the "
        "shared memory; check it before acting so you don't redo or contradict "
        "settled work. Append to it with log_decision (one line, sparingly):\n"
        f"{decisions}\n\n"
        "OUTSTANDING WORK (delegation ledger — close items by sending a RESULT):\n"
        f"{delegations}\n\n"
        "Last 10 messages between teammates (send_peer_message), oldest first:\n"
        f"{recent}\n\n"
        "Your scheduled cron wake-ups (manage with schedule_wakeup / cancel_wakeup):\n"
        f"{crons}\n"
    )


def compose_agent_soul(
    agent_cfg: Dict[str, Any],
    full_config: Optional[Dict[str, Any]] = None,
    include_role: bool = True,
) -> str:
    """Build the full ephemeral system prompt for an agent.

    When include_role is False, the trailing "YOUR ROLE" block is omitted —
    used when the role identity is instead written to SOUL.md (so it is not
    duplicated in both the cached prompt and the ephemeral prompt).
    """
    agent_name = agent_cfg.get("name", "Agent")
    team_id = agent_cfg.get("team_id", "default")
    peers = agent_cfg.get("allowed_peers", [])
    peers_str = ", ".join(peers) if peers else "(none — you cannot message any peers yet)"
    # The shared work surface (all agents collaborate here) + the team metadata
    # dir that holds the brief and changelog. No per-agent workspace folder.
    project_dir = str(_get_project_dir(team_id, full_config))
    team_workspace = str(_get_team_workspace_path(team_id))

    # Build the compact peer roster if we have full config. (We deliberately no
    # longer inline the full-team org diagram + member list into every agent's
    # prompt — that was O(N²) tokens; an agent only needs its own linked peers.)
    peer_roster = "(config not available for peer roster)"
    if full_config:
        peer_roster = _build_peer_roster(full_config, team_id, agent_cfg.get("agent_id", "unknown"))

    # Context-isolated agents (e.g. a black-box QA tester) deliberately get NO
    # product brief, roadmap, or org chart — they must discover the product fresh,
    # as an outside customer would. They keep only the swarm mechanics + their role
    # + the bare list of peers to report findings to.
    isolated = bool(agent_cfg.get("context_isolated"))

    # NOTE: workspace.md (the project brief) is intentionally NOT inlined here.
    # It is injected fresh per-turn via compose_live_context (like the decision
    # log) so an agent's edit is visible to the whole team on the very next turn
    # instead of being frozen into the cached system prompt until a restart.

    # NOTE: the soul template is authored as free-form Markdown and contains
    # literal braces (JSON examples, the lease syntax `agent={your_name}`, etc.),
    # so we CANNOT use str.format() — it would treat those as fields and KeyError.
    # Substitute only our known placeholders explicitly.
    _subs = {
        "{agent_name}": agent_name,
        "{team_id}": team_id,
        "{allowed_peers_list}": peers_str,
        "{sweep_interval}": str(SWEEP_INTERVAL_SECONDS),
        "{project_dir}": project_dir,
        "{team_workspace}": team_workspace,
    }
    common = COMMON_SOUL_TEMPLATE
    for _k, _v in _subs.items():
        common = common.replace(_k, _v)

    if isolated:
        body = (
            f"{common}\n"
            f"--- REPORTING ---\n"
            f"You are not wired into the team's internal context and you do not see the "
            f"org chart. When you find a bug, breakage, or confusing experience, report it "
            f"via send_peer_message to: {peers_str}.\n"
        )
    else:
        body = (
            f"{common}\n"
            f"--- TEAM ORGANIZATION ---\n"
            f"{peer_roster}\n"
        )

    if include_role:
        role = agent_cfg.get("role_soul", f"You are the {agent_name}.")
        body += (
            f"\n{'=' * 60}\n"
            f"YOUR ROLE\n"
            f"{'=' * 60}\n"
            f"{role}\n"
        )

    return body
