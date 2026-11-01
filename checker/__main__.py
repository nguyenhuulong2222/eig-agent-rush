"""CLI: python3 -m checker [--base REF] [--out-dir DIR]

Exit codes: 0 = PASS or WARN, 1 = BLOCK, 2 = usage or configuration error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from . import gitutil
from .engine import run
from .report import JSON_BEGIN, JSON_END, to_codequality, to_json, to_markdown


def _default_base() -> str | None:
    env = os.environ
    if env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA"):
        return env["CI_MERGE_REQUEST_DIFF_BASE_SHA"]
    before = env.get("CI_COMMIT_BEFORE_SHA")
    if before and set(before) != {"0"}:
        return before
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m checker", description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    parser.add_argument("--base", default=None,
                        help="git ref to compare against (default: the MR diff base in CI, else no comparison)")
    parser.add_argument("--mr-description-file", default=None,
                        help="treat this run as an MR and read its description from this file")
    parser.add_argument("--out-dir", default=".",
                        help="where to write eig-report.json, eig-report.md and eig-codequality.json")
    parser.add_argument("--no-fail", action="store_true", help="always exit 0 (local exploration)")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    base_ref = args.base if args.base is not None else _default_base()
    base = gitutil.resolve(root, base_ref) if base_ref else None
    if base_ref and base is None:
        print(f"warning: base ref {base_ref!r} not found; running without base comparison", file=sys.stderr)

    if args.mr_description_file:
        with open(args.mr_description_file, encoding="utf-8") as fh:
            description, is_mr = fh.read(), True
    else:
        is_mr = bool(os.environ.get("CI_MERGE_REQUEST_IID"))
        description = os.environ.get("CI_MERGE_REQUEST_DESCRIPTION", "")

    try:
        report, changed = run(root, base, description, is_mr)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    env = os.environ
    context = {
        "base": base,
        "head": env.get("CI_COMMIT_SHA") or gitutil.resolve(root, "HEAD"),
        "merge_request_iid": env.get("CI_MERGE_REQUEST_IID"),
        "pipeline_id": env.get("CI_PIPELINE_ID"),
        "job_id": env.get("CI_JOB_ID"),
        "is_merge_request": is_mr,
        "changed_files": changed,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }

    os.makedirs(args.out_dir, exist_ok=True)
    md, js = to_markdown(report, context), to_json(report, context)
    with open(os.path.join(args.out_dir, "eig-report.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(os.path.join(args.out_dir, "eig-report.json"), "w", encoding="utf-8") as fh:
        fh.write(js)
    with open(os.path.join(args.out_dir, "eig-codequality.json"), "w", encoding="utf-8") as fh:
        fh.write(to_codequality(report))

    # The job log carries the full report too, so the Duo flow can read it with get_job_logs.
    print(md)
    print(JSON_BEGIN)
    print(js, end="")
    print(JSON_END)

    if report.verdict == "BLOCK" and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
