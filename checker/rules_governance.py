"""Governance: linked issue, code-owner coverage, protected paths."""

from __future__ import annotations

import fnmatch
import re
from typing import Iterable, Optional

from .model import BLOCK, INFO, Finding

CAT_GOV = "governance"

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_ISSUE_REF = re.compile(
    r"(?:(?<![\w/])#\d+\b)"                       # #12
    r"|(?:[\w.-]+/[\w./-]+#\d+\b)"                # group/project#12
    r"|(?:/-/(?:issues|work_items)/\d+\b)"        # .../-/issues/12
)


def references_issue(description: str) -> bool:
    return bool(_ISSUE_REF.search(_HTML_COMMENT.sub("", description or "")))


def check_issue_link(description: Optional[str], is_mr: bool, required: bool) -> list:
    if not is_mr or not required:
        return []
    if references_issue(description or ""):
        return []
    return [Finding("GOV-001", BLOCK, CAT_GOV, "Merge request does not reference an issue",
                    "Every change to the election site must be traceable to a request (for example, a "
                    "results-update issue). The MR description has no issue reference.", None, None,
                    "Add \"Closes #<issue number>\" to the MR description, then re-run the pipeline.")]


def matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return fnmatch.fnmatch(path, pattern)


def check_protected_paths(changed: Iterable[str], patterns: Iterable[str]) -> list:
    patterns = list(patterns)
    hits = sorted({p for p in changed if any(matches(p, pat) for pat in patterns)})
    if not hits:
        return []
    return [Finding("GOV-002", INFO, CAT_GOV, "Code-owner approval required",
                    "This change touches protected paths: " + ", ".join(hits) + ". GitLab requires a code "
                    "owner to approve before merge.", None, None)]


def check_codeowners(text: Optional[str], must_cover: Iterable[str]) -> list:
    if text is None:
        return [Finding("GOV-003", BLOCK, CAT_GOV, "CODEOWNERS file is missing",
                        "Without .gitlab/CODEOWNERS, results and policy changes need no owner approval.",
                        ".gitlab/CODEOWNERS", None, "Restore .gitlab/CODEOWNERS.")]
    owned = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        parts = line.split()
        if len(parts) >= 2 and any(p.startswith("@") for p in parts[1:]):
            owned.add(parts[0])
        elif len(parts) == 1:
            owned.add(parts[0])  # inherits owners from its [Section] header
    out = []
    for path in must_cover:
        if path not in owned:
            out.append(Finding("GOV-003", BLOCK, CAT_GOV, f"CODEOWNERS no longer covers {path}",
                               f"{path} must have a code owner.", ".gitlab/CODEOWNERS", None,
                               f"Restore the {path} entry."))
    return out
