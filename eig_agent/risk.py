"""Deterministic risk signals for the official who signs off or takes an escalation.

Informational only: the gate never reads them and they are not sent to the triage model,
so they cannot change a verdict or an eval result. They point a human at the patterns the
eval showed Nemotron can miss (disguised tampering behind plausible paperwork).
"""

from __future__ import annotations

import datetime as dt
import re

OFF_HOURS = (0, 5)  # 00:00-04:59 local time of the change
_SERVICE = re.compile(r"service account|\bsvc\b|-svc\b|\bbot\b|api token|unknown", re.I)
_CLEANUP = re.compile(r"duplicat|clean ?-?up|dedup|remov", re.I)
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T[\d:]+(?:[+-]\d{2}:\d{2})?)?\b")
_TICKET_ID = re.compile(r"\b[A-Z]{2,}-\d{4}-\d+\b")
_NUMBER = re.compile(r"\d[\d,]*")
# Fields whose changes are counts of ballots, votes or voters.
_COUNT_FIELD = re.compile(r"(votes|total_votes|ballots_cast|registered_voters|precincts_\w+)$")


def _signal(sid: str, title: str, detail: str) -> dict:
    return {"id": sid, "title": title, "detail": detail}


def _count_deltas(diff: list) -> list:
    out = []
    for row in diff:
        a, b = row.get("certified"), row.get("live")
        if (_COUNT_FIELD.search(row["path"]) and isinstance(a, int) and isinstance(b, int)
                and not isinstance(a, bool) and not isinstance(b, bool) and a != b):
            out.append((row["path"], a, b))
    return out


def ticket_numbers(text: str) -> list:
    """Figures quoted in a ticket, ignoring dates, ticket ids and code references like 12-7."""
    text = _TICKET_ID.sub(" ", _DATE.sub(" ", text or ""))
    text = re.sub(r"\b\d+-\d+\b", " ", text)
    return [int(n.replace(",", "")) for n in _NUMBER.findall(text) if n.replace(",", "")]


def actor(meta: dict) -> list:
    who = " ".join(str(meta.get(k) or "") for k in ("submitted_by", "role", "channel"))
    if _SERVICE.search(who):
        return [_signal("RISK-ACTOR", "Change made by a service account or unknown actor",
                        f"submitted_by: {meta.get('submitted_by')!r}, role: {meta.get('role')!r}, "
                        f"channel: {meta.get('channel')!r}")]
    return []


def off_hours(meta: dict) -> list:
    raw = meta.get("submitted_at")
    try:
        when = dt.datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return []
    if when and OFF_HOURS[0] <= when.hour < OFF_HOURS[1]:
        return [_signal("RISK-HOURS", "Change made off-hours",
                        f"submitted at {when.strftime('%H:%M')} local time ({raw}); "
                        f"window {OFF_HOURS[0]:02d}:00-{OFF_HOURS[1]:02d}:00")]
    return []


def scope(meta: dict, diff: list) -> list:
    deltas = _count_deltas(diff)
    if not deltas:
        return []
    biggest = max(abs(b - a) for _, a, b in deltas)
    nums = ticket_numbers(meta.get("ticket_text", ""))
    if not nums:
        return [_signal("RISK-SCOPE", "Ticket gives no figures, but counts changed",
                        f"largest count change is {biggest:,}; the ticket text quotes no numbers")]
    allowed = set(nums) | {abs(a - b) for a in nums for b in nums}
    if max(allowed) * 2 < biggest:
        return [_signal("RISK-SCOPE", "Diff is much larger than the ticket describes",
                        f"largest count change is {biggest:,}; the largest figure the ticket supports is "
                        f"{max(allowed):,}")]
    return []


def one_sided(meta: dict, diff: list) -> list:
    if not _CLEANUP.search(meta.get("ticket_text", "") + " " + (meta.get("commit_message") or "")):
        return []
    out = []
    by_contest = {}
    for path, a, b in _count_deltas(diff):
        m = re.match(r"(contests\[id=[^\]]+\])\.candidates\[(\d+)\]\.votes$", path)
        if m:
            by_contest.setdefault(m[1], []).append((int(m[2]), b - a))
    for contest, changes in by_contest.items():
        if len(changes) == 1 and changes[0][1] < 0:
            out.append(_signal("RISK-ONESIDED", "Votes removed from a single candidate under a cleanup ticket",
                               f"{contest}: candidate #{changes[0][0]} lost {-changes[0][1]:,} votes and no other "
                               f"candidate changed, although the ticket describes a duplicate/cleanup removal"))
    return out


def registered_drop(diff: list) -> list:
    for path, a, b in _count_deltas(diff):
        if path == "turnout.registered_voters" and b < a:
            return [_signal("RISK-REGDROP", "Registered-voter count decreased",
                            f"{a:,} -> {b:,} ({b - a:+,}); this raises reported turnout")]
    return []


def signals(seal: dict, meta: dict) -> list:
    if seal.get("match"):
        return []
    diff = seal.get("diff", [])
    return actor(meta) + off_hours(meta) + scope(meta, diff) + one_sided(meta, diff) + registered_drop(diff)
