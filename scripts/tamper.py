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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eig_agent.demo_ops import DEFAULT_VOTES, MODES, tamper, write_atomic  # noqa: E402,F401


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=MODES, default="one")
    ap.add_argument("--votes", type=int, default=DEFAULT_VOTES)
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
