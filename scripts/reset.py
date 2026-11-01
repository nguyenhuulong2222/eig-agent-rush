"""Reset the live Kanaloa County results to the certified copy (demo reset).

  docker compose run --rm ops python scripts/reset.py

Copies results/results.json (the certified bytes baked into the image) over
$EIG_COUNTY_DATA_DIR/results.json. The ledger is append-only and is not touched.
"""

import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tamper import write_atomic  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=os.environ.get("EIG_COUNTY_DATA_DIR", "/var/eig/county-data"))
    ap.add_argument("--certified", default=os.path.join(ROOT, "results", "results.json"))
    args = ap.parse_args(argv)
    with open(args.certified, "rb") as fh:
        cert = fh.read()
    write_atomic(os.path.join(args.dir, "results.json"), cert)
    print(f"reset to certified {hashlib.sha256(cert).hexdigest()[:16]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
