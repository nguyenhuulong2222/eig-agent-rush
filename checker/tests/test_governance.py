import unittest

from checker.rules_governance import (check_codeowners, check_issue_link, check_protected_paths,
                                      references_issue)
from checker.tests.helpers import REPO


class GovernanceTest(unittest.TestCase):
    def test_issue_references(self):
        for text in ("Closes #12", "Refs #3.", "See group/proj#4",
                     "https://gitlab.com/g/p/-/issues/9", "https://gitlab.com/g/p/-/work_items/1"):
            self.assertTrue(references_issue(text), text)
        for text in ("", "Closes #", "Fixes color #fff", "<!-- Closes #12 -->", "a/b#c", "page#section"):
            self.assertFalse(references_issue(text), text)

    def test_issue_link_only_required_for_mrs(self):
        self.assertEqual(check_issue_link("", is_mr=False, required=True), [])
        self.assertEqual(check_issue_link("", is_mr=True, required=False), [])
        self.assertEqual([f.rule_id for f in check_issue_link("no ref", True, True)], ["GOV-001"])
        self.assertEqual(check_issue_link("Closes #7", True, True), [])

    def test_protected_paths(self):
        patterns = ["results/", "config/", "public/*.html"]
        self.assertEqual(check_protected_paths(["public/app.js", "README.md"], patterns), [])
        findings = check_protected_paths(["results/results.json", "public/index.html"], patterns)
        self.assertEqual([f.severity for f in findings], ["INFO"])

    def test_codeowners(self):
        with open(f"{REPO}/.gitlab/CODEOWNERS", encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(check_codeowners(text, ["/results/", "/config/"]), [])
        self.assertEqual([f.rule_id for f in check_codeowners(None, ["/results/"])], ["GOV-003"])
        stripped = text.replace("/results/\n", "")
        self.assertEqual([f.rule_id for f in check_codeowners(stripped, ["/results/", "/config/"])], ["GOV-003"])


if __name__ == "__main__":
    unittest.main()
