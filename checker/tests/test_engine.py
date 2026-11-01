"""End-to-end: copy the repo into a temp git repo, apply each demo scenario, run the checker."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from checker.__main__ import main
from checker.engine import run
from checker.tests.helpers import REPO

TRACKED = ("config", "results", "public", ".gitlab")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.example",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.example"))


@unittest.skipUnless(shutil.which("git"), "git is required")
class ScenarioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="eig-test-")
        for name in TRACKED:
            shutil.copytree(os.path.join(REPO, name), os.path.join(self.tmp, name),
                            ignore=shutil.ignore_patterns("data"))
        _git(self.tmp, "init", "-q")
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-q", "-m", "base")
        self.base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.tmp, capture_output=True,
                                   text=True, check=True).stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, rel):
        return os.path.join(self.tmp, rel)

    def edit(self, rel, old, new):
        with open(self.path(rel), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(old, text)
        with open(self.path(rel), "w", encoding="utf-8") as fh:
            fh.write(text.replace(old, new, 1))

    def results(self):
        with open(self.path("results/results.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def write_results(self, doc):
        with open(self.path("results/results.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)

    def check(self, description="Closes #2"):
        report, _ = run(self.tmp, self.base, description, is_mr=True)
        return report

    def test_unchanged_repo_passes(self):
        self.assertEqual(self.check().verdict, "PASS")

    def test_scenario_1_arithmetic_error_blocks(self):
        doc = self.results()
        doc["contests"][0]["candidates"][1]["votes"] += 1500   # total_votes not updated
        self.write_results(doc)
        report = self.check()
        self.assertEqual(report.verdict, "BLOCK")
        self.assertIn("RES-003", {f.rule_id for f in report.findings})

    def test_scenario_2_lookalike_polling_link_blocks(self):
        self.edit("public/voter-info.html", "https://elections.kanaloacounty.example/polling-places",
                  "https://elections.kanaloa-county.example/polling-places")
        report = self.check()
        self.assertEqual(report.verdict, "BLOCK")
        self.assertEqual({f.rule_id for f in report.findings if f.severity == "BLOCK"}, {"LINK-002"})

    def test_scenario_3_injected_script_and_weakened_csp_blocks(self):
        self.edit("public/results.html", '<script src="app.js" defer></script>',
                  '<script src="app.js" defer></script>\n  '
                  '<script src="https://cdn.analytics-tracker.test/t.js"></script>')
        self.edit("public/results.html", "script-src 'self';",
                  "script-src 'self' https://cdn.analytics-tracker.test 'unsafe-inline';")
        report = self.check()
        blocked = {f.rule_id for f in report.findings if f.severity == "BLOCK"}
        self.assertEqual(report.verdict, "BLOCK")
        self.assertTrue({"ASSET-001", "CSP-002", "CSP-003", "CSP-004"} <= blocked, blocked)

    def test_scenario_4_clean_update_passes(self):
        shutil.copy(self.path("results/results.json"), self.path("results/history/0003.json"))
        doc = self.results()
        doc["update"].update(sequence=4, published_at="2026-11-03T23:45:00-10:00")
        doc["turnout"].update(ballots_cast=34502, turnout_percent=round(34502 * 100 / 48210, 2))
        for contest, adds, precincts in zip(doc["contests"], ([2051, 1876, 301], [618, 590], [2400, 1610]),
                                            (42, 14, 42)):
            for cand, add in zip(contest["candidates"], adds):
                cand["votes"] += add
            contest["total_votes"] = sum(c["votes"] for c in contest["candidates"])
            contest["precincts_reporting"] = precincts
        self.write_results(doc)
        report = self.check("Publishes update 4.\n\nCloses #5")
        self.assertEqual(report.verdict, "PASS", [f.to_dict() for f in report.findings])
        self.assertEqual({f.rule_id for f in report.findings}, {"GOV-002"})  # code-owner note only

    def test_mr_without_issue_blocks(self):
        report = self.check(description="Small copy fix.")
        self.assertEqual({f.rule_id for f in report.findings}, {"GOV-001"})

    def test_policy_loosening_is_caught(self):
        self.edit("config/policy.json", '"max_share_swing_pp": 5.0', '"max_share_swing_pp": 40.0')
        self.edit("config/security-policy.json", '"\'unsafe-inline\'",\n', "")
        rules = {f.rule_id for f in self.check().findings}
        self.assertTrue({"POL-001", "CSP-006"} <= rules, rules)

    def test_cli_writes_reports_and_exit_codes(self):
        out = os.path.join(self.tmp, "out")
        desc = os.path.join(self.tmp, "desc.txt")
        with open(desc, "w", encoding="utf-8") as fh:
            fh.write("Closes #1")
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            code = main(["--root", self.tmp, "--base", self.base, "--mr-description-file", desc, "--out-dir", out])
        self.assertEqual(code, 0)
        self.assertIn("EIG-REPORT-JSON-BEGIN", buf.getvalue())
        with open(os.path.join(out, "eig-report.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["verdict"], "PASS")

        doc = self.results()
        doc["contests"][0]["precincts_reporting"] = 99
        self.write_results(doc)
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--root", self.tmp, "--base", self.base, "--mr-description-file", desc, "--out-dir", out])
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(os.path.join(out, "eig-report.md")))
        with open(os.path.join(out, "eig-codequality.json"), encoding="utf-8") as fh:
            cq = json.load(fh)
        self.assertEqual([(i["check_name"], i["severity"], i["location"]["path"]) for i in cq],
                         [("RES-004", "blocker", "results/results.json")])
        self.assertEqual(len(cq[0]["fingerprint"]), 64)


if __name__ == "__main__":
    unittest.main()
