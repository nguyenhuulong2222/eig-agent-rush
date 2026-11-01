"""Package step: copy results into the site and write a SHA-256 manifest.

Usage: python3 scripts/build_site.py [--root .]

Writes public/data/results.json and public/manifest.json. The manifest records the
commit and a hash of every published file; the monitor job uses it to detect drift.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from site_files import MANIFEST, RESULTS_PUBLISHED_AS, SITE_DIR, sha256, site_files  # noqa: E402


def _head(root: str) -> str:
    sha = os.environ.get("CI_COMMIT_SHA")
    if sha:
        return sha
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    root = os.path.abspath(args.root)

    files = site_files(root)
    data_path = os.path.join(root, SITE_DIR, RESULTS_PUBLISHED_AS)
    os.makedirs(os.path.dirname(data_path), exist_ok=True)
    with open(data_path, "wb") as fh:
        fh.write(files[RESULTS_PUBLISHED_AS])

    manifest = {
        "fictional_demo": True,
        "commit": _head(root),
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "files": {path: sha256(data) for path, data in sorted(files.items())},
    }
    with open(os.path.join(root, SITE_DIR, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    print(f"Packaged {len(files)} files for commit {manifest['commit'][:12]}:")
    for path, digest in manifest["files"].items():
        print(f"  {digest[:16]}  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
