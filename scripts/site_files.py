"""Which bytes make up the published site, read from the working tree or from a git commit.

Shared by the package job (build_site.py) and the monitor job (monitor_drift.py), so
the monitor compares the live site against exactly what the package step would publish.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from typing import Dict, Optional

SITE_DIR = "public"
RESULTS_FILE = "results/results.json"
RESULTS_PUBLISHED_AS = "data/results.json"
MANIFEST = "manifest.json"


def _git(root: str, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def site_files(root: str, ref: Optional[str] = None) -> Dict[str, bytes]:
    """Map of published path (relative to the site root) to file bytes."""
    files: Dict[str, bytes] = {}
    if ref is None:
        site = os.path.join(root, SITE_DIR)
        for dirpath, _dirs, names in os.walk(site):
            for name in names:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, site).replace(os.sep, "/")
                if rel == MANIFEST or rel.startswith("data/"):
                    continue  # generated outputs, rebuilt below
                with open(full, "rb") as fh:
                    files[rel] = fh.read()
        with open(os.path.join(root, RESULTS_FILE), "rb") as fh:
            files[RESULTS_PUBLISHED_AS] = fh.read()
    else:
        listing = _git(root, "ls-tree", "-r", "--name-only", ref, SITE_DIR + "/").decode().splitlines()
        for path in listing:
            rel = path[len(SITE_DIR) + 1:]
            if rel == MANIFEST or rel.startswith("data/"):
                continue
            files[rel] = _git(root, "show", f"{ref}:{path}")
        files[RESULTS_PUBLISHED_AS] = _git(root, "show", f"{ref}:{RESULTS_FILE}")
    return files


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
