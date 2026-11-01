"""Results arithmetic (BLOCK) and anomaly detection between updates (WARN)."""

from __future__ import annotations

from typing import Optional

from .model import BLOCK, WARN, Finding

CAT_ARITH = "results-arithmetic"
CAT_ANOM = "results-anomaly"


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _f(rule, sev, cat, title, detail, path, suggestion=None) -> Finding:
    return Finding(rule, sev, cat, title, detail, file=path, suggestion=suggestion)


def check_results(doc, path: str) -> list:
    """Validate one results document. Every rule here is a hard BLOCK, except the fictional flag."""
    out = []
    if not isinstance(doc, dict):
        return [_f("RES-001", BLOCK, CAT_ARITH, "Results file is not a JSON object",
                   "The top level of the results file must be an object.", path)]

    missing = [k for k in ("update", "turnout", "contests") if k not in doc]
    if missing:
        return [_f("RES-001", BLOCK, CAT_ARITH, "Results file is missing required fields",
                   "Missing: " + ", ".join(missing) + ".", path)]

    if doc.get("fictional") is not True:
        out.append(_f("RES-008", WARN, CAT_ARITH, "Results are not marked as fictional demo data",
                      "Demo data must carry \"fictional\": true so it can never be mistaken for real results.",
                      path, "Add \"fictional\": true at the top level."))

    turnout = doc.get("turnout") or {}
    registered = turnout.get("registered_voters")
    cast = turnout.get("ballots_cast")
    if not (_is_count(registered) and _is_count(cast)):
        out.append(_f("RES-001", BLOCK, CAT_ARITH, "Turnout counts are missing or invalid",
                      "registered_voters and ballots_cast must be non-negative integers.", path))
        registered = cast = None
    else:
        if cast > registered:
            out.append(_f("RES-005", BLOCK, CAT_ARITH, "More ballots cast than registered voters",
                          f"ballots_cast {cast:,} is greater than registered_voters {registered:,}.", path,
                          "Recheck both numbers against the certified voter roll and tabulation export."))
        reported = turnout.get("turnout_percent")
        computed = (cast * 100 / registered) if registered else 0.0
        if reported is not None:
            if isinstance(reported, bool) or not isinstance(reported, (int, float)) or not 0 <= reported <= 100:
                out.append(_f("RES-006", BLOCK, CAT_ARITH, "Turnout is outside 0–100%",
                              f"turnout_percent is {reported!r}.", path))
            elif abs(reported - computed) > 0.01 + 1e-9:
                out.append(_f("RES-006", BLOCK, CAT_ARITH, "Reported turnout does not match the counts",
                              f"turnout_percent is {reported} but ballots_cast / registered_voters = {computed:.2f}%.",
                              path, f"Set turnout_percent to {computed:.2f}."))
        if not 0 <= computed <= 100:
            out.append(_f("RES-006", BLOCK, CAT_ARITH, "Turnout is outside 0–100%",
                          f"Computed turnout is {computed:.2f}%.", path))

    contests = doc.get("contests")
    if not isinstance(contests, list) or not contests:
        out.append(_f("RES-001", BLOCK, CAT_ARITH, "No contests in the results file",
                      "contests must be a non-empty list.", path))
        return out

    for idx, contest in enumerate(contests):
        cid = contest.get("id", f"#{idx}") if isinstance(contest, dict) else f"#{idx}"
        label = f"contest '{cid}'"
        if not isinstance(contest, dict) or not isinstance(contest.get("candidates"), list):
            out.append(_f("RES-001", BLOCK, CAT_ARITH, f"Malformed {label}",
                          "Each contest needs a candidates list.", path))
            continue

        pr, pt = contest.get("precincts_reporting"), contest.get("precincts_total")
        if not (_is_count(pr) and _is_count(pt)):
            out.append(_f("RES-004", BLOCK, CAT_ARITH, f"Invalid precinct counts in {label}",
                          "precincts_reporting and precincts_total must be non-negative integers.", path))
        elif pr > pt:
            out.append(_f("RES-004", BLOCK, CAT_ARITH, f"More precincts reporting than exist in {label}",
                          f"precincts_reporting {pr} is greater than precincts_total {pt}.", path))

        votes_ok = True
        for cand in contest["candidates"]:
            name = cand.get("name", "?") if isinstance(cand, dict) else "?"
            votes = cand.get("votes") if isinstance(cand, dict) else None
            if not _is_count(votes):
                votes_ok = False
                out.append(_f("RES-002", BLOCK, CAT_ARITH, f"Invalid vote count in {label}",
                              f"'{name}' has votes = {votes!r}; votes must be a non-negative integer.", path))

        total = contest.get("total_votes")
        if not _is_count(total):
            out.append(_f("RES-003", BLOCK, CAT_ARITH, f"Invalid total_votes in {label}",
                          f"total_votes is {total!r}.", path))
        elif votes_ok:
            summed = sum(c["votes"] for c in contest["candidates"])
            if summed != total:
                out.append(_f("RES-003", BLOCK, CAT_ARITH, f"Votes do not add up in {label}",
                              f"Candidate votes sum to {summed:,} but total_votes is {total:,} "
                              f"(difference {summed - total:+,}).", path,
                              "Re-export the contest from the tabulation system; do not edit totals by hand."))
            if cast is not None and total > cast:
                out.append(_f("RES-007", BLOCK, CAT_ARITH, f"More votes than ballots in {label}",
                              f"total_votes {total:,} is greater than ballots_cast {cast:,}.", path))
    return out


def _shares(contest: dict) -> dict:
    total = contest.get("total_votes") or 0
    if not total:
        return {}
    return {c["name"]: c["votes"] * 100 / total for c in contest.get("candidates", [])
            if isinstance(c, dict) and _is_count(c.get("votes")) and "name" in c}


def check_anomalies(current: dict, previous: Optional[dict], path: str,
                    max_swing_pp: float, previous_label: str) -> list:
    """Compare two consecutive updates. Anomalies are WARN: a human must look, not a hard stop."""
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return []
    out = []
    cur_seq = (current.get("update") or {}).get("sequence")
    prev_seq = (previous.get("update") or {}).get("sequence")
    if isinstance(cur_seq, int) and isinstance(prev_seq, int) and cur_seq <= prev_seq:
        out.append(_f("ANOM-004", WARN, CAT_ANOM, "Update sequence did not increase",
                      f"This update is #{cur_seq}; the previous one ({previous_label}) is #{prev_seq}.", path))

    prev_by_id = {c.get("id"): c for c in previous.get("contests", []) if isinstance(c, dict)}
    for contest in current.get("contests", []):
        if not isinstance(contest, dict):
            continue
        prev = prev_by_id.get(contest.get("id"))
        if prev is None:
            continue
        cid = contest.get("id")
        if _is_count(contest.get("precincts_reporting")) and _is_count(prev.get("precincts_reporting")) \
                and contest["precincts_reporting"] < prev["precincts_reporting"]:
            out.append(_f("ANOM-003", WARN, CAT_ANOM, f"Precincts reporting went down in contest '{cid}'",
                          f"{prev['precincts_reporting']} → {contest['precincts_reporting']} "
                          f"since {previous_label}.", path))

        prev_votes = {c.get("name"): c.get("votes") for c in prev.get("candidates", []) if isinstance(c, dict)}
        cur_names = {c.get("name") for c in contest.get("candidates", []) if isinstance(c, dict)}
        if set(prev_votes) != cur_names:
            out.append(_f("ANOM-005", WARN, CAT_ANOM, f"Candidate list changed in contest '{cid}'",
                          f"Added: {sorted(cur_names - set(prev_votes)) or 'none'}; "
                          f"removed: {sorted(set(prev_votes) - cur_names) or 'none'}.", path))
        for cand in contest.get("candidates", []):
            if not isinstance(cand, dict):
                continue
            before = prev_votes.get(cand.get("name"))
            if _is_count(before) and _is_count(cand.get("votes")) and cand["votes"] < before:
                out.append(_f("ANOM-002", WARN, CAT_ANOM, f"Vote count went down in contest '{cid}'",
                              f"'{cand['name']}': {before:,} → {cand['votes']:,} since {previous_label}. "
                              "Counts normally only grow during election night.", path,
                              "If this is a correction, explain it in the MR and link the correction record."))

        cur_shares, prev_shares = _shares(contest), _shares(prev)
        for name, share in cur_shares.items():
            if name in prev_shares:
                swing = share - prev_shares[name]
                if abs(swing) > max_swing_pp:
                    out.append(_f("ANOM-001", WARN, CAT_ANOM, f"Large vote-share swing in contest '{cid}'",
                                  f"'{name}' moved {swing:+.1f} percentage points "
                                  f"({prev_shares[name]:.1f}% → {share:.1f}%) since {previous_label}; "
                                  f"the review threshold is {max_swing_pp:g} pp.", path,
                                  "Confirm which precincts were added in this update and that they explain the swing."))
    return out
