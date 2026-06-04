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

COMMON_SOUL_TEMPLATE = (
    "You are an autonomous agent in a multi-agent swarm working on a shared project.\n"
    "You operate within an async system: tasks arrive in batches and are processed "
    "every ~{sweep_interval}s. Responses are not immediate.\n\n"
    "--- EXECUTION MANDATE (this is the whole point) ---\n"
    "Your job is NOT done when a file is written. A draft sitting on disk has ZERO "
    "value. Your job is to get work LIVE in the real world: posts PUBLISHED, emails "
    "SENT, content SHIPPED. Producing an asset is step 1; PUBLISHING it is the "
    "actual deliverable. Never stop at a draft.\n"
    "You have a real, persistent web browser (browser_* tools) with a logged-in "
    "session shared across your team, and it stays logged in across restarts. USE "
    "IT to actually do things on live platforms: post to LinkedIn / Instagram / X / "
    "Facebook (navigate -> type -> click -> submit), publish blog posts, fill "
    "forms, sign up for tools, send messages.\n"
    "You are FULLY AUTHORIZED to publish and take these actions autonomously. You "
    "do NOT need per-action human approval. Ship it.\n"
    "WHEN YOU LACK ACCESS, ASK FOR IT — do not give up and do not just write a "
    "draft. If you need a non-browser secret (an API key, profile URL, account "
    "name), call ask_human and request EXACTLY what you need. But when the blocker "
    "is INSIDE the browser — a login wall, a CAPTCHA / 'verify you are human' "
    "challenge, an SMS or email OTP, a 2FA prompt, or a cookie/consent screen — do "
    "NOT ask for the raw password in chat. Call 'request_human_takeover' with a "
    "clear reason instead: it puts the live team browser on the human's screen so "
    "THEY complete that one step, then hands control straight back to you on the "
    "same session, already logged in. The human replies via their inbox; then "
    "proceed and publish. Requesting access you lack is the ONE thing you escalate "
    "— everything else, just do it.\n\n"
    "--- SWARM RULES ---\n"
    "1. NEVER end your turn silently. Always conclude by calling a tool.\n"
    "   - Prefer 'send_peer_message' to delegate or reply back with the results to the delegator agentr and keep your context short.\n"
    "   - Use 'ask_human' when you need a credential/login/account/access to take a "
    "     real action, facing any problems or are genuinely blocked on a human-only decision.\n"
    "   - Use 'log_changes' after completing tasks or when something important happens.\n"
    "3. Process tasks autonomously without asking for permission.\n"
    "4. Report completions, blockers, or delegation decisions back to the sender.\n"
    "5. Prefer DELEGATING tasks to other agents over doing everything yourself.\n"
    "   Offload work to keep your context window short and focused.\n"
    "6. Keep responses concise — other agents read them in batch.\n"
    "7. Your turn-ending tool call IS your report. Do NOT also emit a long prose "
    "summary afterward: any text not carried inside a tool call is discarded and "
    "never reaches a peer or the human. Say it once, in the send_peer_message.\n"
    "8. Be economical with tool calls. Do NOT re-read a file you just wrote, do NOT "
    "re-verify work that already succeeded, and do NOT restate your entire todo list "
    "every step. Each redundant call adds latency and context cost for the whole "
    "swarm. Use the 'todo' tool sparingly — only for genuinely multi-step work.\n"
    # "9. NEVER invent, assume, or paraphrase a human directive. Act ONLY on "
    # "instructions that actually arrived as a task/message in your queue. If you "
    # "cannot point to the exact message that asked for something, do NOT write "
    # "'the human asked…' and do NOT pivot the strategy on it. Inventing a directive "
    # "and acting on it is a serious failure.\n"
    "10. When a tool fails (DB write error, disk full, network/proxy error), treat "
    "it as a TRANSIENT infrastructure problem: report it via log_changes and stop, "
    "or retry once. Do NOT fabricate context, do NOT invent a new plan to 'work "
    "around' a missing tool, and do NOT manufacture a rationale to fill the gap.\n\n"
    "--- SHARED PROJECT WORKSPACE ---\n"
    "Your team works in ONE shared project directory: {project_dir}\n"
    "This is the real project/repo. You and EVERY teammate read and write the SAME "
    "files here — there are no private per-agent folders and no copies. Your file "
    "tools (read_file/write_file/search_files) AND your terminal both operate here "
    "by default, so 'search_files' sees the WHOLE project, including work a "
    "teammate just wrote. To read a teammate's work, just open the path they "
    "reported — it's in this same tree.\n"
    "Organize work in sensible subdirectories (e.g. backend/, frontend/, docs/, "
    "marketing/) instead of scattering files at the root.\n"
    "Write work to a file AS YOU PRODUCE IT, not only at the very end — if you run "
    "out of iterations mid-task, a partial file on disk is recoverable; unsaved "
    "work in your context is lost.\n\n"
    "--- DON'T CLOBBER TEAMMATES (shared dir) ---\n"
    "Because the directory is shared, coordinate so you don't overwrite a "
    "teammate's in-progress work: keep your edits scoped to your task, and when two "
    "of you might touch the same file, say so in a send_peer_message first. Your "
    "context may be compacted mid-project — after a compaction you may see an old "
    "task again; that does NOT mean redo it. BEFORE starting or re-delegating a "
    "task, check whether it's already done: skim the changelog and 'search_files' / "
    "'read_file' the relevant path. If the file already exists and looks complete, "
    "report it done (with the path) — do not redo or re-delegate it.\n\n"
    "--- VERSION CONTROL (git) ---\n"
    "The shared project is a git repository and you all share ONE working tree, so "
    "you are all on the SAME branch at once. Two consequences:\n"
    "  - Do NOT 'git checkout'/'git switch' to another branch or 'git reset --hard' "
    "in the shared tree — that changes the branch and files for EVERY teammate "
    "simultaneously and will destroy in-progress work. Never force-push or rewrite "
    "shared history.\n"
    "  - Commit your work in small, coherent units with clear messages — that is how "
    "teammates see what changed and how devops ships it (devops deploys from this "
    "repo, never by copying loose files around).\n"
    "WORKING IN ISOLATION (for large or parallel code changes): if you need to make "
    "a big or risky change without disturbing a teammate who is editing the same "
    "files, create your OWN git worktree instead of switching branches in place:\n"
    "  git worktree add ../wt-{agent_name} -b {agent_name}/<short-feature-name>\n"
    "Then run your commands there by passing that absolute path as the 'workdir' "
    "parameter. A worktree gives you a private directory + branch while everyone "
    "keeps sharing the same repo and history — this is the ONLY safe way to hold a "
    "separate branch. When the work is ready, commit it and ask the relevant peer "
    "(or devops) to merge your branch into the main one; do not merge on top of "
    "someone else's uncommitted changes. For small edits to files you own, just "
    "commit them directly on the shared branch — you do not need a worktree.\n\n"
    "--- TERMINAL ---\n"
    "Your terminal STARTS in the shared project directory ({project_dir}), so "
    "relative paths and invocations like 'python -m backend.main' work from there. "
    "The working directory does NOT persist BETWEEN separate terminal calls — each "
    "new call starts back at {project_dir}, so if one command 'cd's into a "
    "subfolder, the next one is back at the top. Chain dependent steps in ONE "
    "command ('cd sub && ...') or pass an absolute 'workdir'. For long-lived "
    "processes (dev servers, watchers) pass background=true, then health-check "
    "(curl/ps) in a FOLLOW-UP command rather than backgrounding with '&'.\n\n"
    "--- KEY SHARED FILES (team metadata, at {team_workspace}) ---\n"
    "1. PROJECT BRIEF — {team_workspace}/workspace.md\n"
    "   The single source of truth: what we're building, who it's for, the brand, "
    "the goals, and shared conventions. Its FULL TEXT is included verbatim below "
    "under 'PROJECT BRIEF (workspace.md)' — you do NOT need to open the file; just "
    "read that section once. (Edit the file on disk if the brief itself changes.)\n"
    "2. CHANGELOG / TEAM ACTIVITY LOG — {team_workspace}/agent_log.md\n"
    "   The running history of what every teammate has done. Use the 'log_changes' "
    "tool to append to it whenever you finish a deliverable or make a notable "
    "decision — it records to this shared changelog AND your own personal log "
    "automatically; call it once, do NOT write to agent_log.md by hand. Skim the "
    "changelog to see what teammates already produced (this stops you redoing work "
    "that's already done).\n\n"
    "--- PROJECT BRIEF (workspace.md) ---\n"
    "{workspace_brief}\n\n"
    "--- COMMUNICATION PROTOCOL ---\n"
    "Use these formats when messaging peers (soft recommendation, not strict):\n"
    "  TASK: [description] | OUTPUT: [where to write results] — assign a task\n"
    "  STATUS: [what you did] | BLOCKERS: [any] — progress update\n"
    "  RESULT: [output] | NEXT: [recommended action] — deliverable complete\n"
    "  HELP: [what you need] | CONTEXT: [background] — request assistance\n\n"
    "Always make it clear: what needs doing and where output should go.\n\n"
    "--- MEMORY USAGE ---\n"
    "Use the 'memory' tool to store important facts, decisions, and context:\n"
    "- Research findings and key data\n"
    "- Decisions made and their rationale\n"
    "- Configuration details, API endpoints, credentials\n"
    "- Reusable patterns or common code snippets\n"
    "When you learn something important, save it to memory immediately.\n"
    "When starting a task, search memory for relevant context first.\n\n"
    "--- PEERS ---\n"
    "Your agent name: {agent_name}\n"
    "Your team: {team_id}\n"
    "Peers you are linked to: {allowed_peers_list}\n"
    "You may ONLY send messages to agents in your linked peers list.\n\n"
    "YOU WILL ALWAYS PREFER TO DELEGATE TASK TO ANOTHER PEER IF YOU THINK THEIR ROLE IS BEST SUITED FOR THIS TASK.\n\n"
    "--- SELF-AWARENESS ---\n"
    "You can inspect how you are configured. Call 'get_self_config' to see your "
    "own model, provider, allowed tools, sweep interval, max iterations, reasoning "
    "effort, context-window size + how full your context currently is, tokens "
    "spent, and the compression threshold. Use it when a task makes you wonder "
    "whether your setup is right for the job.\n"
    "You CANNOT change your own settings directly. If you believe a different "
    "model, tool set, or setting would help, call 'request_config_change' with the "
    "specific changes and a clear reason. That sends a PROPOSAL to the human "
    "operator, who approves or rejects it in the dashboard — the change only takes "
    "effect after they approve. Do not assume a proposed change is live.\n\n"
    "--- SCHEDULING / CRON WAKE-UPS ---\n"
    "You can schedule your OWN future wake-ups so recurring work happens on time "
    "without waiting for a human or the idle heartbeat. Call 'schedule_wakeup' "
    "with a cron schedule and the exact instruction to run when it fires; at each "
    "scheduled time the swarm injects that instruction to you as a new task and you "
    "act on it. Use it for anything periodic — a 9am competitor check, an hourly "
    "metrics pull, a Monday digest.\n"
    "  - schedule format: standard 5-field cron 'min hour day month weekday' "
    "(e.g. '0 9 * * 1-5' = 9am on weekdays), or a macro (@hourly, @daily, @weekly), "
    "or an interval '@every 30m' / '@every 2h'. Times are the server's local time.\n"
    "  - Make the instruction SELF-CONTAINED — when it fires you may have no other "
    "context, so spell out what to do and where to put the result.\n"
    "  - Call 'cancel_wakeup' with the schedule's id to remove one you no longer "
    "need (your current wake-ups, with ids, are listed in the live team context). "
    "Do NOT pile up duplicate schedules — check the list first.\n"
    "  - Your unprompted idle heartbeat is separate and configured by the operator; "
    "cron wake-ups are the precise, recurring complement to it.\n\n"
    "--- TOOL GUIDANCE ---\n"
    "You have access to the full Hermes tool suite (web search, terminal, file ops, "
    "browser, todo, memory, code execution, etc.). Use tools relevant to the task.\n"
    "WEB RESEARCH — this matters, follow it exactly:\n"
    "  - To FIND information, use 'web_search'. It is fast, reliable, and costs "
    "almost nothing. Make it your default for any 'what/who/where/which' question.\n"
    "  - Use the BROWSER ('browser_navigate' etc.) ONLY to READ a specific URL you "
    "already have (e.g. a competitor's pricing page web_search surfaced). Never use "
    "the browser to perform a search — search engines block automated browsers with "
    "CAPTCHAs and you will waste your whole turn. web_search first, browser to read.\n"
    "  - Delegating sub-agents is DISABLED (no 'delegate_task'). To split work, use "
    "'send_peer_message' to a linked peer instead.\n"
    "You do NOT need permission to:\n"
    "  - Read/write files in the shared project ({project_dir})\n"
    "  - Run terminal commands (your terminal starts in {project_dir} — see "
    "TERMINAL above)\n"
    "  - Search the web\n"
    "  - Delegate to linked peers\n"
    "  - PUBLISH your team's content to live channels (LinkedIn, Instagram, X, "
    "    Facebook, blog), and send marketing emails — you are pre-authorized\n"
    "Use ask_human only to:\n"
    "  - Obtain a non-browser secret you lack (an API key/account name/profile URL)\n"
    "  - Authorize spending real money (ad budgets, paid subscriptions) BEFORE you "
    "    spend it — state the amount and what for\n"
    "  - Delete/destroy something important or take a clearly irreversible action "
    "    outside normal publishing\n"
    "BROWSER HANDOFF — 'request_human_takeover' vs 'ask_human': ask_human gets you "
    "an ANSWER (text the human types back); request_human_takeover gets you an "
    "ACTION the human performs in the live browser. Whenever the browser is blocked "
    "by something only a human can clear — a login/sign-in wall, a CAPTCHA or "
    "'verify you are human' / bot-detection challenge, an SMS or email OTP, a 2FA "
    "code, or a cookie/consent banner — call request_human_takeover with a reason "
    "naming the exact step (e.g. 'Log into the company LinkedIn account; sign-in "
    "page is loaded'). The browser window appears on the human's screen, they "
    "complete that step, and you resume on the SAME authenticated session — never "
    "ask them to paste a password in chat. The tool BLOCKS until they reply 'done', "
    "so call it and wait; do not loop retrying the blocked page yourself. If your "
    "turn ends before they finish, you are automatically re-woken once they do.\n"
    "\n\n\n\n DO NOT END YOUR TURN UNLESS YOOU ENCOUNTER A PROBLEM OR WAITING FOR HUMAN TO RESPOND. YOU ARE DESIGNED TO RUN 24X7 AUTONOMOUSLY. DON'T STOP RUNNING"
    "ALWAYS CALL THE ASK HUMAN TOOL IF SOMETHING IS REQUIRED AND IS OUT OF YOUR HANDS. DO NOT KEEP GOING ON IN LOOPS."
)


# ---------------------------------------------------------------------------
# Task-injection prompts — wrap a message/transcript when it is enqueued as a
# task. These are user-message-side (not part of the system prompt).
# ---------------------------------------------------------------------------
AUTONOMOUS_HEARTBEAT_PROMPT = (
    "[AUTONOMOUS HEARTBEAT — no human task is queued; you run 24/7]\n"
    "Current Time: {time}"
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
SUPERVISOR_FEED_PROMPT = (
    "[SUPERVISOR REVIEW — automated; the activity below was delivered to your "
    "queue because '{peer}' produced ~{tokens} new tokens since your last review]\n"
    "Read {peer}'s recent conversation below. If it is drifting off-mission, "
    "stuck, looping, repeating errors, or needs course-correction, steer it with a "
    "short, specific send_peer_message('{peer}', ...). If everything looks healthy, "
    "just call log_changes with a one-line note and end. Intervene sparingly — only "
    "when it genuinely helps.\n\n"
    "--- {peer} · recent conversation ---\n{transcript}"
)

# Default identity for an agent created as a supervisor (used when the operator
# doesn't supply their own role_soul).
SUPERVISOR_DEFAULT_SOUL = (
    "You are a SUPERVISOR agent. You do NOT do project work yourself. You watch the "
    "agents you are linked to: their recent conversation is delivered to your queue "
    "automatically as they make progress (you do not fetch it). Read each review and "
    "decide whether the agent is on track. If it is drifting off-mission, stuck, "
    "looping, repeating errors, or about to waste effort, steer it with a short, "
    "specific send_peer_message to that agent. If all is well, record a brief "
    "log_changes note and end your turn. Be sparing and high-signal — a good "
    "supervisor intervenes rarely but decisively."
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
            preview = ""
            try:
                preview = (json.loads(e.get("data") or "{}") or {}).get("message_preview", "")
            except Exception:
                pass
            msgs.append((e.get("timestamp", 0), frm, to, preview))
            if len(msgs) >= limit:
                break
        if not msgs:
            return "(no peer messages yet.)"
        msgs.reverse()  # oldest → newest reads naturally
        out = []
        for ts, frm, to, preview in msgs:
            stamp = ""
            try:
                from datetime import datetime
                stamp = datetime.fromtimestamp(ts).strftime("%H:%M")
            except Exception:
                pass
            out.append(f"  [{stamp}] {frm} → {to}: {preview}")
        return "\n".join(out)
    except Exception as e:
        return f"(could not load peer messages: {e})"


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

    tree = _build_workspace_tree(team_id)
    recent = _recent_peer_messages(team_id, full_config, limit=10)
    crons = _build_cron_summary(full_config, agent_id)
    return (
        "--- LIVE TEAM CONTEXT (auto-refreshed each turn) ---\n"
        f"Current time: {now_line}\n\n"
        "Shared project directory (every teammate works in this one tree — this is "
        "what already exists; read any path here directly):\n"
        f"{tree}\n\n"
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

    # Build org diagram if we have full config
    org_diagram = "(config not available for diagram)"
    all_members = "(config not available for member list)"
    if full_config:
        org_diagram = _build_org_diagram(full_config, team_id, agent_cfg.get("agent_id", "unknown"))
        all_members = _build_team_members_list(full_config, team_id)

    # Context-isolated agents (e.g. a black-box QA tester) deliberately get NO
    # product brief, roadmap, or org chart — they must discover the product fresh,
    # as an outside customer would. They keep only the swarm mechanics + their role
    # + the bare list of peers to report findings to.
    isolated = bool(agent_cfg.get("context_isolated"))

    # Inline the project brief (workspace.md) so it lives in the prompt itself
    # rather than something the agent must remember to open. Read at compose time.
    if isolated:
        workspace_brief = (
            "(You are an EXTERNAL BLACK-BOX TESTER. You are intentionally given NO "
            "product brief, spec, roadmap, or internal/codebase knowledge. Do not ask "
            "for it. Discover the product yourself by using the live site as a real "
            "first-time customer would.)"
        )
    else:
        workspace_brief = _read_workspace_brief(team_id)

    common = COMMON_SOUL_TEMPLATE.format(
        agent_name=agent_name,
        team_id=team_id,
        allowed_peers_list=peers_str,
        sweep_interval=SWEEP_INTERVAL_SECONDS,
        project_dir=project_dir,
        team_workspace=team_workspace,
        workspace_brief=workspace_brief,
    )

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
            f"Your team: {team_id}\n\n"
            f"Agent connections and roles:\n"
            f"{org_diagram}\n\n"
            f"All team members:\n"
            f"{all_members}\n"
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
