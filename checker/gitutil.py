"""Thin wrappers around the git CLI. All failures degrade to None so the checker still runs."""

from __future__ import annotations

import subprocess
from typing import Optional


def _git(root: str, *args: str) -> Optional[str]:
    try:
        res = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


def resolve(root: str, ref: Optional[str]) -> Optional[str]:
    if not ref or set(ref) == {"0"}:
        return None
    out = _git(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    return out.strip() if out else None


def changed_files(root: str, base: str) -> Optional[list]:
    """Files that differ between `base` and the working tree, plus untracked files."""
    diff = _git(root, "diff", "--name-only", base)
    if diff is None:
        return None
    untracked = _git(root, "ls-files", "--others", "--exclude-standard") or ""
    return sorted({p for p in (diff + untracked).splitlines() if p})


def show(root: str, ref: str, path: str) -> Optional[str]:
    return _git(root, "show", f"{ref}:{path}")
