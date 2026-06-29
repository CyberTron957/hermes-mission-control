"""Out-of-band agent activity summarizer (Layer 3 observability).

A cheap, teams-global model reads each agent's *recorded* transcript from
monitoring.db and writes a short rolling status digest, so an operator running
10+ agents reads digests instead of millions of tokens of chat.

Design notes:
  * This is a plain LLM call in a background loop — NOT a full Hermes agent. It
    never touches an agent's own context window or run path, so summarizing is
    decoupled from (and cannot interrupt or slow) the work being summarized.
  * Rolling: each digest is produced from the PRIOR digest + only the messages
    newer than that digest's watermark (covers_to_msg_id). We never re-read old
    transcript, so input stays bounded no matter how much an agent has produced.
  * Hybrid trigger + skip-if-idle live in maybe_digest(): an idle agent (no new
    messages since its last digest) costs a single COUNT(*) and nothing more.

The digest summary is stored as a JSON object:
    {headline, did[], blocked_on, next, risk_level}
where risk_level ∈ {ok, watch, stuck, error} so the dashboard health roll-up
(Layer 2) and a supervisor agent (Layer 4) can consume fields, not prose.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from teams_server.config import (
    DIGEST_INPUT_CHAR_CAP,
    DIGEST_MAX_AGE_SECONDS,
    DIGEST_MIN_NEW_TOKENS,
    get_global_settings,
)
from teams_server.monitoring import monitor_db

log = logging.getLogger("teams.summarizer")

RISK_LEVELS = ("ok", "watch", "stuck", "error")

_SYSTEM_PROMPT = (
    "You are a monitoring summarizer for an autonomous AI agent team. You read "
    "ONE agent's recent activity transcript and produce a terse status digest "
    "for a human operator who cannot read the full logs. Be factual, specific, "
    "and concise — name the concrete thing worked on, not vague paraphrase. "
    "Actively flag trouble: an agent repeating the same tool call, looping, "
    "hitting the same error, drifting off its task, or stalled/waiting on a "
    "human. Prefer signal over politeness.\n\n"
    "Respond with ONLY a JSON object, no prose, no code fence:\n"
    "{\n"
    '  "headline": "<=12 words, what this agent is doing right now",\n'
    '  "did": ["concrete progress point", "..."],   // 1-3 short items\n'
    '  "blocked_on": "<one line, or null if not blocked>",\n'
    '  "next": "<one line on the apparent next step, or null>",\n'
    '  "risk_level": "ok | watch | stuck | error"\n'
    "}\n"
    "risk_level guide: ok = progressing normally; watch = slow/uncertain/minor "
    "errors; stuck = looping or no real progress or waiting on a human; error = "
    "repeated failures or broken state."
)


def _resolve_summary_target() -> Dict[str, str]:
    """Effective {model, base_url, api_key} for the digest model. Model is the
    UI-configurable global setting (falling back to the teams default model); the
    endpoint is the resolved default backend. The digest is a direct
    OpenAI-compatible call, so it only runs when the default model is a custom /
    OpenAI-compatible endpoint (base_url present); native providers (anthropic/…)
    have no base_url here and the digest is skipped — see _call_llm."""
    settings = get_global_settings()
    try:
        from teams_server.model_config import get_default_model

        dm = get_default_model() or {}
    except Exception:
        dm = {}
    model = (settings.get("summary_model") or "").strip() or dm.get("model") or ""
    base_url = (dm.get("base_url") or "").strip()
    api_key = (dm.get("api_key") or "").strip()
    return {"model": model, "base_url": base_url, "api_key": api_key}


def _build_transcript(messages: List[dict]) -> str:
    """Render messages oldest-first, capping total chars to the most-recent
    slice (no silent loss — a marker notes any truncation)."""
    lines = []
    for m in messages:
        role = (m.get("role") or "?").upper()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"[{role}] {content}")
    text = "\n\n".join(lines)
    if len(text) > DIGEST_INPUT_CHAR_CAP:
        text = ("[…older activity in this window truncated…]\n\n"
                + text[-DIGEST_INPUT_CHAR_CAP:])
    return text


def _prior_status_block(last_digest: Optional[dict]) -> str:
    if not last_digest:
        return "(none — this is the first digest for this agent)"
    try:
        prev = json.loads(last_digest.get("summary") or "{}")
        return json.dumps(prev, ensure_ascii=False)
    except Exception:
        return str(last_digest.get("summary") or "")


def _parse_summary(raw: str) -> Optional[dict]:
    """Tolerant JSON extraction — strip code fences / surrounding prose."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        # drop a leading language tag like "json\n"
        nl = s.find("\n")
        if nl != -1 and " " not in s[:nl]:
            s = s[nl + 1:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    # Normalize shape so consumers can trust the fields.
    did = obj.get("did")
    if isinstance(did, str):
        did = [did]
    elif not isinstance(did, list):
        did = []
    # Fail SAFE: a missing/null/unknown risk_level (the likely truncation shape
    # under max_tokens) must NOT read as healthy. Only an explicit, valid "ok"
    # from the model yields "ok"; anything else escalates to "watch".
    risk = str(obj.get("risk_level") or "watch").lower().strip()
    if risk not in RISK_LEVELS:
        risk = "watch"
    return {
        "headline": str(obj.get("headline") or "").strip()[:200],
        "did": [str(x).strip()[:300] for x in did[:3] if str(x).strip()],
        "blocked_on": (str(obj["blocked_on"]).strip()[:300]
                       if obj.get("blocked_on") else None),
        "next": (str(obj["next"]).strip()[:300] if obj.get("next") else None),
        "risk_level": risk,
    }


def _call_llm(target: Dict[str, str], transcript: str, prior: str,
              agent_name: str) -> Optional[str]:
    if not target.get("base_url") or not target.get("model"):
        # The monitoring digest is a direct OpenAI-compatible call; it needs a
        # custom/OpenAI-compatible endpoint. Native providers have no base_url —
        # skip quietly rather than dial a phantom endpoint every sweep.
        log.debug("[Digest] no OpenAI-compatible endpoint for %s — skipping digest", agent_name)
        return None
    try:
        from openai import OpenAI
    except Exception as e:  # pragma: no cover
        log.warning("[Digest] openai client unavailable: %s", e)
        return None
    user = (
        f"AGENT: {agent_name}\n\n"
        f"PRIOR STATUS (your last digest for this agent):\n{prior}\n\n"
        f"NEW ACTIVITY SINCE THEN (oldest first):\n{transcript}\n\n"
        "Produce the updated status digest as the JSON object specified."
    )
    try:
        client = OpenAI(base_url=target["base_url"], api_key=target["api_key"],
                        timeout=60.0, max_retries=1)
        resp = client.chat.completions.create(
            model=target["model"],
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=400,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[Digest] LLM call failed for %s: %s", agent_name, e)
        return None


def summarize_agent(
    agent_name: str, team_id: Optional[str] = None, *, force: bool = False,
) -> Optional[dict]:
    """Produce one rolling digest for an agent if there is new activity.

    Returns the stored digest dict ({summary, covers_to_msg_id, ...}) on
    success, or None if skipped (no new activity) or the LLM call failed.
    `force` only bypasses the idle-skip, not the activity requirement (there is
    nothing to summarize without new messages).

    Reads query by agent_name only (it is globally unique here); team_id is
    recorded on the digest for grouping, not used to filter the transcript —
    messages are logged with a NULL team_id, so filtering would match nothing."""
    last = monitor_db.get_last_digest(agent_name)
    after_id = int((last or {}).get("covers_to_msg_id") or 0)
    activity = monitor_db.get_new_activity(agent_name, after_id)
    if activity["count"] <= 0:
        return None  # idle — nothing new to summarize

    messages = monitor_db.get_messages_since(agent_name, after_id)
    if not messages:
        return None
    max_id = max(int(m["id"]) for m in messages)

    target = _resolve_summary_target()
    transcript = _build_transcript(messages)
    prior = _prior_status_block(last)
    raw = _call_llm(target, transcript, prior, agent_name)
    summary = _parse_summary(raw or "")
    if summary is None:
        # Don't advance the watermark — retry this window on the next sweep.
        return None

    digest_id = monitor_db.save_digest(
        agent_name=agent_name,
        summary=json.dumps(summary, ensure_ascii=False),
        covers_to_msg_id=max_id,
        msg_count=activity["count"],
        tokens_in=activity["tokens"],
        model=target["model"],
        team_id=team_id,
    )
    record = {
        "id": digest_id,
        "agent_name": agent_name,
        "team_id": team_id,
        "timestamp": time.time(),
        "summary": summary,
        "covers_to_msg_id": max_id,
        "msg_count": activity["count"],
        "tokens_in": activity["tokens"],
        "model": target["model"],
    }
    try:
        from teams_server.websocket import _broadcast

        _broadcast("digest_updated", {
            "agent_name": agent_name, "team_id": team_id,
            "summary": summary, "timestamp": record["timestamp"],
            "msg_count": activity["count"], "model": target["model"],
        })
    except Exception as e:
        log.debug("[Digest] broadcast skipped: %s", e)
    log.info("[Digest] %s: %s (%d msgs, ~%d tok, risk=%s)", agent_name,
             summary.get("headline", ""), activity["count"], activity["tokens"],
             summary.get("risk_level"))
    return record


_ROLLUP_SYSTEM_PROMPT = (
    "You compress a batch of a team's chronological DECISION-LOG entries into one "
    "compact 'milestone' summary for long-term memory. Capture: what was decided, "
    "what shipped/was verified, the current state, and any standing blockers. Be "
    "factual and specific; preserve concrete facts (URLs, modes, names, numbers). "
    "No preamble, no markdown — return 2-4 plain sentences, <=120 words."
)


def maybe_rollup_decisions(team_id: str) -> Optional[dict]:
    """Summarize a team's oldest un-rolled decisions into a milestone once they
    scroll past the live window, so memory older than the injected last-N isn't
    lost. Idempotent and cheap: a single read gates the LLM call.

    Keeps the most recent DECISION_LIVE_WINDOW decisions live (un-rolled) and only
    acts once at least DECISION_ROLLUP_TRIGGER have accumulated beyond the last
    milestone."""
    from teams_server.config import DECISION_LIVE_WINDOW, DECISION_ROLLUP_TRIGGER

    last = monitor_db.get_latest_milestone(team_id)
    after_id = int((last or {}).get("covers_to_decision") or 0)
    unrolled = monitor_db.get_decisions_after(team_id, after_id, limit=1000)  # oldest first
    if len(unrolled) < DECISION_ROLLUP_TRIGGER:
        return None  # not enough beyond the live window yet

    # Roll everything except the most-recent live window; keep that live.
    to_roll = unrolled[:-DECISION_LIVE_WINDOW] if DECISION_LIVE_WINDOW > 0 else unrolled
    if not to_roll:
        return None
    covers_to = int(to_roll[-1]["id"])

    lines = []
    for d in to_roll:
        stamp = ""
        try:
            from datetime import datetime
            stamp = datetime.fromtimestamp(d.get("timestamp", 0)).strftime("%m-%d %H:%M")
        except Exception:
            pass
        lines.append(f"[{stamp}] {d.get('agent_name')}: {d.get('decision')}")
    batch = "\n".join(lines)

    prior = (last or {}).get("summary") or "(none)"
    target = _resolve_summary_target()
    try:
        from openai import OpenAI
        client = OpenAI(base_url=target["base_url"], api_key=target["api_key"],
                        timeout=60.0, max_retries=1)
        resp = client.chat.completions.create(
            model=target["model"],
            messages=[
                {"role": "system", "content": _ROLLUP_SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"PRIOR MILESTONE (context, do not repeat verbatim):\n{prior}\n\n"
                    f"NEW DECISIONS TO ROLL UP ({len(to_roll)} entries, oldest first):\n{batch}\n\n"
                    "Write the updated milestone summary.")},
            ],
            temperature=0.2,
            max_tokens=300,
        )
        summary = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[Rollup] LLM call failed for team %s: %s", team_id, e)
        return None
    if not summary:
        return None

    monitor_db.save_milestone(team_id, summary, covers_to_decision=covers_to)
    log.info("[Rollup] team %s: rolled %d decisions into milestone (covers_to=%d)",
             team_id, len(to_roll), covers_to)
    return {"team_id": team_id, "rolled": len(to_roll), "covers_to": covers_to,
            "summary": summary}


def maybe_digest(agent_name: str, team_id: Optional[str] = None) -> Optional[dict]:
    """Hybrid trigger: summarize iff there's new activity AND (enough new
    volume OR enough time elapsed since the last digest). Idle agents cost a
    single COUNT(*). Respects the global digest_enabled switch."""
    settings = get_global_settings()
    if not settings.get("digest_enabled", True):
        return None

    last = monitor_db.get_last_digest(agent_name)
    after_id = int((last or {}).get("covers_to_msg_id") or 0)
    activity = monitor_db.get_new_activity(agent_name, after_id)
    if activity["count"] <= 0:
        return None  # idle — skip entirely

    volume_due = activity["tokens"] >= DIGEST_MIN_NEW_TOKENS
    last_ts = float((last or {}).get("timestamp") or 0.0)
    # First-ever digest (last_ts == 0) is time-due as soon as there's activity.
    time_due = (time.time() - last_ts) >= DIGEST_MAX_AGE_SECONDS
    if not (volume_due or time_due):
        return None

    return summarize_agent(agent_name, team_id, force=True)
