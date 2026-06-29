"""Layer-5 observability: a teams-wide loop detector.

Per-agent supervision (Layer 4) feeds each supervisor ONE peer's transcript, so
a loop that spans agents — ceo↔cgo↔cto trading confirmations, or a whole team
"monitoring" one blocker — is invisible to every local supervisor. This module
watches the team's *message graph* as a whole and intervenes when motion isn't
producing progress.

Two deterministic detectors run over a recent window of `message_sent` events:

  A. Pair ping-pong — one A↔B pair trades a lot of WAKING messages whose content
     keeps repeating (acknowledge/status churn with no new information).
  B. Team stall — the team sends many messages in the window while logging ZERO
     decisions AND ZERO actions: everyone is talking, nobody is shipping.

On a fresh detection (cooldown-deduped) it enqueues ONE corrective nudge to the
most-active involved agent and records a team-visible decision-log note, so the
loop is broken structurally rather than waiting for a model to notice it itself.
"""

import json
import logging
import re
import time
from typing import Callable, Dict, List, Optional

from teams_server.config import (
    LOOP_ALERT_COOLDOWN_SECONDS,
    LOOP_PAIR_THRESHOLD,
    LOOP_TEAM_MSG_THRESHOLD,
    LOOP_WINDOW_SECONDS,
)
from teams_server.monitoring import monitor_db

log = logging.getLogger("teams.loopdetector")

_WAKING = {"TASK", "QUESTION", "RESULT"}

# signature -> last alert time, for cooldown dedup across scans.
_last_alert: Dict[str, float] = {}


def _norm(text: str) -> str:
    """Collapse a message preview to its intent so near-duplicate status
    re-confirmations compare equal (lowercase, alnum+space, drop a tool RESULT)."""
    t = (text or "").split("→", 1)[0]
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()[:120]


def _recent_team_messages(team_members: set, now: float) -> List[dict]:
    """message_sent events within the window touching this team, newest-first."""
    cutoff = now - LOOP_WINDOW_SECONDS
    out: List[dict] = []
    # Query message_sent specifically + time-bounded, so a flood of other event
    # types (state_change, etc.) can't crowd the window out of a fixed page.
    for e in monitor_db.get_events_since("message_sent", cutoff, limit=1000):
        ts = e.get("timestamp", 0) or 0
        frm, to = e.get("from_agent"), e.get("to_agent")
        if frm not in team_members and to not in team_members:
            continue
        kind = preview = ""
        try:
            d = json.loads(e.get("data") or "{}") or {}
            kind = (d.get("kind") or "").upper()
            preview = d.get("message_preview") or ""
        except Exception:
            pass
        out.append({"ts": ts, "from": frm, "to": to, "kind": kind, "preview": preview})
    return out


def _detect_pair_pingpong(msgs: List[dict]) -> Optional[dict]:
    """Find the worst A↔B pair that is trading repetitive WAKING messages."""
    pairs: Dict[frozenset, List[dict]] = {}
    for m in msgs:
        if m["kind"] and m["kind"] not in _WAKING:
            continue  # STATUS/FYI don't wake — they can't be a loop
        if not m["from"] or not m["to"]:
            continue
        pairs.setdefault(frozenset((m["from"], m["to"])), []).append(m)

    worst = None
    for pair, items in pairs.items():
        if len(items) < LOOP_PAIR_THRESHOLD:
            continue
        # both directions present (a true back-and-forth, not a one-way fan-out)
        directions = {(m["from"], m["to"]) for m in items}
        if len(directions) < 2:
            continue
        norms = [_norm(m["preview"]) for m in items if m["preview"]]
        if not norms:
            continue
        uniq = len(set(norms))
        dup_ratio = 1.0 - (uniq / len(norms))
        # repetitive == few distinct intents relative to volume
        if dup_ratio >= 0.4 or uniq <= max(2, len(norms) // 3):
            cand = {"pair": sorted(pair), "count": len(items), "dup_ratio": round(dup_ratio, 2),
                    "latest_from": max(items, key=lambda m: m["ts"])["from"]}
            if worst is None or cand["count"] > worst["count"]:
                worst = cand
    return worst


def _detect_team_stall(team_id: str, msgs: List[dict], now: float) -> Optional[dict]:
    """Many messages, zero decisions AND zero actions in the window."""
    if len(msgs) < LOOP_TEAM_MSG_THRESHOLD:
        return None
    since = now - LOOP_WINDOW_SECONDS
    decisions = monitor_db.count_decisions_since(team_id, since)
    actions = monitor_db.count_actions_since(team_id, since)
    if decisions == 0 and actions == 0:
        return {"messages": len(msgs), "decisions": 0, "actions": 0}
    return None


def _fire(signature: str, now: float) -> bool:
    """Cooldown gate — True if we should alert (and records the alert time)."""
    last = _last_alert.get(signature, 0)
    if now - last < LOOP_ALERT_COOLDOWN_SECONDS:
        return False
    _last_alert[signature] = now
    return True


def _broadcast(event: str, payload: dict) -> None:
    try:
        from teams_server.websocket import _broadcast as _b
        _b(event, payload)
    except Exception:
        pass


def scan_team(team_id: str, member_names: List[str],
              ingest_fn: Callable[[str, str, str], None]) -> List[dict]:
    """Scan one team; act on fresh detections. `ingest_fn(agent, from_agent,
    payload)` enqueues a corrective nudge. Returns the alerts raised."""
    now = time.time()
    members = set(member_names)
    msgs = _recent_team_messages(members, now)
    if not msgs:
        return []

    alerts: List[dict] = []

    pp = _detect_pair_pingpong(msgs)
    if pp:
        a, b = pp["pair"]
        sig = f"{team_id}:pair:{a}|{b}"
        if _fire(sig, now):
            target = pp["latest_from"] if pp["latest_from"] in members else a
            other = b if target == a else a
            note = (f"LOOP DETECTED: {a} and {b} exchanged {pp['count']} messages with "
                    f"little new content (dup_ratio={pp['dup_ratio']}) and no shipped "
                    f"progress.")
            ingest_fn(target, "loop_detector", (
                f"[SYSTEM — LOOP BREAKER] You and {other} are in a no-progress message "
                f"loop ({pp['count']} messages, mostly repeats). STOP confirming/"
                f"re-statusing. Either take ONE concrete external action toward the "
                f"team's brief right now (a publish, deploy, send, or real fix — and log "
                f"it with log_action), or, if you are genuinely blocked, send {other} ONE "
                f"message naming the exact blocker and then work a DIFFERENT task. Do not "
                f"reply to acknowledge this."))
            monitor_db.log_decision("loop_detector", note, team_id=team_id)
            _broadcast("loop_detected", {"team_id": team_id, "type": "pair",
                                         "pair": pp["pair"], "count": pp["count"],
                                         "nudged": target, "timestamp": now})
            log.warning("[LoopDetector] %s pair loop %s<->%s (%d msgs) -> nudged %s",
                        team_id, a, b, pp["count"], target)
            alerts.append({"type": "pair", **pp, "nudged": target})

    stall = _detect_team_stall(team_id, msgs, now)
    if stall:
        sig = f"{team_id}:stall"
        if _fire(sig, now):
            # Nudge the busiest sender — the one most able to redirect the team.
            from collections import Counter
            busiest = Counter(m["from"] for m in msgs if m["from"] in members).most_common(1)
            target = busiest[0][0] if busiest else member_names[0]
            note = (f"TEAM STALL: {stall['messages']} messages in the last "
                    f"{LOOP_WINDOW_SECONDS // 60}m but 0 decisions and 0 actions shipped.")
            ingest_fn(target, "loop_detector", (
                f"[SYSTEM — STALL BREAKER] The team sent {stall['messages']} messages in "
                f"the last {LOOP_WINDOW_SECONDS // 60} minutes but shipped NOTHING (0 "
                f"decisions, 0 actions). Talk is not progress. Take ONE concrete action "
                f"from the team's brief now and log it (log_action), or delegate ONE "
                f"specific shippable task to a specialist. Do not reply to acknowledge."))
            monitor_db.log_decision("loop_detector", note, team_id=team_id)
            _broadcast("loop_detected", {"team_id": team_id, "type": "stall",
                                         **stall, "nudged": target, "timestamp": now})
            log.warning("[LoopDetector] %s team stall (%d msgs, 0 progress) -> nudged %s",
                        team_id, stall["messages"], target)
            alerts.append({"type": "stall", **stall, "nudged": target})

    return alerts
