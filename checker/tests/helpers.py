"""Shared fixtures for checker tests."""

import copy
import json
import os

# Repository root, for importing code such as scripts/.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Frozen snapshot of config/, results/, public/ and CODEOWNERS (see fixtures/repo/README.md).
# Tests never read the live files, because on an MR branch those are the change under test.
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "repo")


def load_repo_json(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return json.load(fh)


def results():
    return copy.deepcopy(load_repo_json("results/results.json"))


def rule_ids(findings):
    return sorted({f.rule_id for f in findings})
