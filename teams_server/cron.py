"""Tiny, dependency-free cron engine for agent scheduled wake-ups.

Supports standard 5-field cron (``minute hour day-of-month month day-of-week``)
with ``*``, ``*/n`` steps, ``a-b`` ranges, ``a-b/n`` stepped ranges, and
comma lists of any of those. Day-of-week is ``0-6`` with ``0``/``7`` = Sunday
(``mon``-``sun`` names also accepted); month accepts ``jan``-``dec``.

Also accepts the common macros ``@hourly @daily @midnight @weekly @monthly
@yearly @annually`` and an interval form ``@every <N><s|m|h|d>`` (e.g.
``@every 30m``) for "fire every N" schedules.

Vixie-cron day semantics: when BOTH day-of-month and day-of-week are
restricted (neither is ``*``) a day matches if EITHER field matches; if only
one is restricted that one applies. Times are evaluated in the server's local
timezone, matching how the dashboard shows "now".

Kept self-contained (no ``croniter`` dependency) so the teams install stays
"batteries-included" and existing deployments need no extra pip step.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

# Smallest interval the ``@every`` form will accept, in seconds. Guards against a
# runaway "@every 1s" that would hammer the agent every sweep tick.
MIN_EVERY_SECONDS = 30
# How far ahead cron_next will search before giving up (covers yearly schedules
# plus leap slack). Returns None past this — a schedule that never matches.
_SEARCH_HORIZON_DAYS = 1500

_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DOWS = {d: i for i, d in enumerate(
    ["sun", "mon", "tue", "wed", "thu", "fri", "sat"], start=0)}

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class CronError(ValueError):
    """Raised when a cron expression cannot be parsed."""


def _expand_field(field: str, lo: int, hi: int, names: Optional[Dict[str, int]] = None) -> Set[int]:
    """Expand one cron field into the concrete set of integers it matches."""
    out: Set[int] = set()
    for part in field.split(","):
        part = part.strip().lower()
        if not part:
            raise CronError(f"empty term in field '{field}'")
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                raise CronError(f"bad step '/{step_s}'")
            if step < 1:
                raise CronError("step must be >= 1")
        else:
            base = part

        if base in ("*", ""):
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            start, end = _name_or_int(a, names, lo, hi), _name_or_int(b, names, lo, hi)
        else:
            start = end = _name_or_int(base, names, lo, hi)

        if start > end:
            raise CronError(f"range start > end in '{part}'")
        for v in range(start, end + 1, step):
            out.add(v)
    if not out:
        raise CronError(f"field '{field}' matched nothing")
    return out


def _name_or_int(tok: str, names: Optional[Dict[str, int]], lo: int, hi: int) -> int:
    tok = tok.strip().lower()
    if names and tok in names:
        return names[tok]
    try:
        v = int(tok)
    except ValueError:
        raise CronError(f"unrecognized value '{tok}'")
    if v < lo or v > hi:
        raise CronError(f"value {v} out of range {lo}-{hi}")
    return v


def _parse_every(expr: str) -> int:
    """Parse '@every 30m' / '@every 2h' → seconds. Raises CronError if invalid."""
    rest = expr[len("@every"):].strip().lower()
    if not rest:
        raise CronError("@every needs an interval, e.g. '@every 15m'")
    unit = rest[-1]
    if unit not in _UNIT_SECONDS:
        raise CronError("@every unit must be one of s, m, h, d")
    try:
        n = int(rest[:-1])
    except ValueError:
        raise CronError(f"bad @every interval '{rest}'")
    if n < 1:
        raise CronError("@every interval must be >= 1")
    secs = n * _UNIT_SECONDS[unit]
    if secs < MIN_EVERY_SECONDS:
        raise CronError(f"@every interval too small (min {MIN_EVERY_SECONDS}s)")
    return secs


def _parse_fields(expr: str) -> Tuple[Set[int], Set[int], Set[int], Set[int], Set[int], bool, bool]:
    parts = expr.split()
    if len(parts) != 5:
        raise CronError("cron must have 5 fields: minute hour day month weekday")
    mins = _expand_field(parts[0], 0, 59)
    hours = _expand_field(parts[1], 0, 23)
    doms = _expand_field(parts[2], 1, 31)
    months = _expand_field(parts[3], 1, 12, _MONTHS)
    dows_raw = _expand_field(parts[4], 0, 7, _DOWS)
    dows = {0 if d == 7 else d for d in dows_raw}  # normalize Sunday (7→0)
    dom_restricted = parts[2].strip() != "*"
    dow_restricted = parts[4].strip() != "*"
    return mins, hours, doms, months, dows, dom_restricted, dow_restricted


def _day_matches(dt: datetime, doms: Set[int], dows: Set[int],
                 dom_restricted: bool, dow_restricted: bool) -> bool:
    cron_dow = dt.isoweekday() % 7  # isoweekday: Mon=1..Sun=7 → cron Sun=0..Sat=6
    dom_ok = dt.day in doms
    dow_ok = cron_dow in dows
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    if dom_restricted:
        return dom_ok
    if dow_restricted:
        return dow_ok
    return True


def _normalize(expr: str) -> str:
    return " ".join(expr.strip().lower().split())


def cron_next(expr: str, after: float) -> Optional[float]:
    """Return the next Unix timestamp strictly after ``after`` that matches.

    ``@every N`` schedules simply return ``after + N`` (interval from now).
    Returns None if no match within the search horizon.
    """
    expr = _normalize(expr)
    if expr.startswith("@every"):
        return after + _parse_every(expr)
    expr = _MACROS.get(expr, expr)
    mins, hours, doms, months, dows, dom_r, dow_r = _parse_fields(expr)

    dt = datetime.fromtimestamp(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = dt + timedelta(days=_SEARCH_HORIZON_DAYS)
    while dt < limit:
        if dt.month not in months:
            # jump to 00:00 on the 1st of the next month
            year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
            dt = dt.replace(year=year, month=month, day=1, hour=0, minute=0)
            continue
        if not _day_matches(dt, doms, dows, dom_r, dow_r):
            dt = (dt + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if dt.hour not in hours:
            dt = (dt + timedelta(hours=1)).replace(minute=0)
            continue
        if dt.minute not in mins:
            dt = dt + timedelta(minutes=1)
            continue
        ts = dt.timestamp()
        # Enforce the strict-after contract. During a DST fall-back the repeated
        # hour maps naive->timestamp with fold=0 (the FIRST occurrence), so a
        # candidate inside that hour can resolve to a timestamp <= ``after``.
        # Returning it would make the caller re-fire the same cron every tick;
        # advance and keep scanning until we get a strictly-greater timestamp.
        if ts <= after:
            dt = dt + timedelta(minutes=1)
            continue
        return ts
    return None


def cron_validate(expr: str) -> Tuple[bool, str]:
    """Validate an expression. Returns (ok, normalized-expr-or-error-message)."""
    try:
        norm = _normalize(expr)
        if not norm:
            return False, "empty schedule"
        if norm.startswith("@every"):
            _parse_every(norm)
            return True, norm
        resolved = _MACROS.get(norm, norm)
        _parse_fields(resolved)
        return True, norm
    except CronError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001 — never let a bad string crash a caller
        return False, f"invalid cron: {e}"


def cron_describe(expr: str) -> str:
    """Best-effort human-readable summary (falls back to the raw expression)."""
    norm = _normalize(expr)
    friendly = {
        "@hourly": "every hour", "@daily": "every day at midnight",
        "@midnight": "every day at midnight", "@weekly": "every Sunday at midnight",
        "@monthly": "on the 1st of each month", "@yearly": "once a year",
        "@annually": "once a year",
    }
    if norm in friendly:
        return friendly[norm]
    if norm.startswith("@every"):
        try:
            secs = _parse_every(norm)
            return f"every {_fmt_secs(secs)}"
        except CronError:
            return norm
    return norm


def _fmt_secs(secs: int) -> str:
    for unit, label in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if secs % unit == 0:
            n = secs // unit
            return f"{n} {label}{'s' if n != 1 else ''}"
    return f"{secs} seconds"
