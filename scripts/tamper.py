"""Simulate an attacker changing the live Kanaloa County results (demo trigger).

  docker compose run --rm ops python scripts/tamper.py [--mode one|swap] [--votes N]

  one   (default) add N votes to the leading Mayor candidate. One number changes, so the
        totals no longer add up: the seal mismatches and a deterministic BLOCK rule fires
        -> ROLLBACK proposal even with the MOCK provider.
  swap  move N votes between the two Mayor front-runners, keeping every total consistent:
        the seal mismatches but arithmetic passes, so the model's verdicts decide
        (MOCK -> ESCALATE unless EIG_MOCK_VERDICT=tampering).

Writes $EIG_COUNTY_DATA_DIR/results.json directly, the way an attacker with write access
to the web root would. Prints the before/after SHA-256. Undo with scripts/reset.py.
"""

import argparse
import hashlib
import json
import os
import sys


def tamper(doc: dict, mode: str = "one", votes: int = 1632) -> dict:
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
    tmp = os.path.join(os.path.dirname(path), ".results.json.tamper")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["one", "swap"], default="one")
    ap.add_argument("--votes", type=int, default=1632)
    ap.add_argument("--dir", default=os.environ.get("EIG_COUNTY_DATA_DIR", "/var/eig/county-data"))
    args = ap.parse_args(argv)
    path = os.path.join(args.dir, "results.json")
    with open(path, "rb") as fh:
        before = fh.read()
    after = (json.dumps(tamper(json.loads(before), args.mode, args.votes), indent=2, ensure_ascii=False) + "\n").encode()
    write_atomic(path, after)
    print(f"tampered ({args.mode}, {args.votes} votes): {hashlib.sha256(before).hexdigest()[:16]}… -> "
          f"{hashlib.sha256(after).hexdigest()[:16]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
