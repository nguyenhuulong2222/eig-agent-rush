import unittest

from checker.model import BLOCK, WARN
from checker.rules_results import check_anomalies, check_results
from checker.tests.helpers import results, rule_ids

PATH = "results/results.json"


class ResultsArithmeticTest(unittest.TestCase):
    def test_repo_results_pass(self):
        self.assertEqual(check_results(results(), PATH), [])

    def test_sum_mismatch_blocks(self):
        doc = results()
        doc["contests"][0]["candidates"][0]["votes"] += 1000
        findings = check_results(doc, PATH)
        self.assertIn("RES-003", rule_ids(findings))
        self.assertTrue(all(f.severity == BLOCK for f in findings if f.rule_id == "RES-003"))

    def test_negative_and_non_integer_votes_block(self):
        for bad in (-5, 12.5, "100", True, None):
            doc = results()
            doc["contests"][1]["candidates"][0]["votes"] = bad
            self.assertIn("RES-002", rule_ids(check_results(doc, PATH)), bad)

    def test_precincts_reporting_over_total_blocks(self):
        doc = results()
        doc["contests"][0]["precincts_reporting"] = 43
        self.assertIn("RES-004", rule_ids(check_results(doc, PATH)))

    def test_ballots_over_registered_blocks(self):
        doc = results()
        doc["turnout"]["ballots_cast"] = doc["turnout"]["registered_voters"] + 1
        doc["turnout"]["turnout_percent"] = 100.0
        ids = rule_ids(check_results(doc, PATH))
        self.assertIn("RES-005", ids)
        self.assertIn("RES-006", ids)  # computed turnout over 100%

    def test_turnout_out_of_range_and_mismatch_block(self):
        doc = results()
        doc["turnout"]["turnout_percent"] = 120
        self.assertIn("RES-006", rule_ids(check_results(doc, PATH)))
        doc["turnout"]["turnout_percent"] = 70.0
        self.assertIn("RES-006", rule_ids(check_results(doc, PATH)))

    def test_contest_total_over_ballots_blocks(self):
        doc = results()
        c = doc["contests"][0]
        c["candidates"][0]["votes"] += 5000
        c["total_votes"] += 5000
        self.assertIn("RES-007", rule_ids(check_results(doc, PATH)))

    def test_missing_fictional_flag_warns(self):
        doc = results()
        del doc["fictional"]
        findings = check_results(doc, PATH)
        self.assertEqual(rule_ids(findings), ["RES-008"])
        self.assertEqual(findings[0].severity, WARN)

    def test_structural_errors_block(self):
        self.assertIn("RES-001", rule_ids(check_results([], PATH)))
        self.assertIn("RES-001", rule_ids(check_results({"update": {}}, PATH)))


class AnomalyTest(unittest.TestCase):
    def setUp(self):
        self.prev = results()
        self.cur = results()
        self.cur["update"]["sequence"] = 4

    def run_check(self, threshold=5.0):
        return check_anomalies(self.cur, self.prev, PATH, threshold, "update #3")

    def test_small_change_is_clean(self):
        c = self.cur["contests"][0]
        c["candidates"][0]["votes"] += 300
        c["candidates"][1]["votes"] += 280
        c["total_votes"] += 580
        self.assertEqual(self.run_check(), [])

    def test_large_swing_warns(self):
        c = self.cur["contests"][0]
        c["candidates"][1]["votes"] += 9000
        c["total_votes"] += 9000
        findings = self.run_check()
        self.assertIn("ANOM-001", rule_ids(findings))
        self.assertTrue(all(f.severity == WARN for f in findings))

    def test_threshold_is_configurable(self):
        c = self.cur["contests"][0]
        c["candidates"][1]["votes"] += 9000
        c["total_votes"] += 9000
        self.assertNotIn("ANOM-001", rule_ids(self.run_check(threshold=50)))

    def test_votes_and_precincts_going_down_warn(self):
        c = self.cur["contests"][0]
        c["candidates"][0]["votes"] -= 10
        c["total_votes"] -= 10
        c["precincts_reporting"] -= 1
        ids = rule_ids(self.run_check())
        self.assertIn("ANOM-002", ids)
        self.assertIn("ANOM-003", ids)

    def test_sequence_must_increase(self):
        self.cur["update"]["sequence"] = 3
        self.assertIn("ANOM-004", rule_ids(self.run_check()))

    def test_candidate_list_change_warns(self):
        self.cur["contests"][0]["candidates"].append({"name": "New Person", "votes": 0})
        self.assertIn("ANOM-005", rule_ids(self.run_check()))

    def test_no_previous_update(self):
        self.assertEqual(check_anomalies(self.cur, None, PATH, 5.0, ""), [])


if __name__ == "__main__":
    unittest.main()
