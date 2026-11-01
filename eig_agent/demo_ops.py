"""Demo operations on the live county results: simulated tampering and atomic writes.

Used by scripts/tamper.py (operator CLI) and by the orchestrator's rate-limited
"Simulate tamper" API, so judges can trigger the whole flow themselves. Every use through
the API is written to the ledger as SIMULATED_TAMPER.

  one   add N votes to the leading Mayor candidate: totals no longer add up, so the seal
        mismatches and a deterministic BLOCK rule (RES-003) fires -> ROLLBACK proposal.
  swap  move N votes between the two Mayor front-runners, every total kept: the seal
        mismatches but arithmetic passes, so the model's verdicts decide.
"""

from __future__ import annotations

import os

MODES = ("one", "swap")
DEFAULT_VOTES = 1632


def tamper(doc: dict, mode: str = "one", votes: int = DEFAULT_VOTES) -> dict:
    mayor = next(c for c in doc["contests"] if c["id"] == "mayor")
    cands = sorted(mayor["candidates"], key=lambda c: -c["votes"])
    if mode == "one":
        cands[0]["votes"] += votes
    elif mode == "swap":
        moved = min(votes, cands[0]["votes"])
        cands[0]["votes"] -= moved
        cands[1]["votes"] += moved
    else:
        raise ValueError(f"unknown mode {mode!r}")
    return doc


def write_atomic(path: str, data: bytes) -> None:
    """Write next to the target and rename, so the county site never serves a half-written file."""
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
