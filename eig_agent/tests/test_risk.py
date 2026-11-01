import json
import os
import unittest

os.environ["EIG_USAGE_LOG"] = "off"

from eig_agent import risk, triage  # noqa: E402
from eig_agent.__main__ import CERTIFIED, case_live_bytes, load_case  # noqa: E402
from eig_agent.llm import NemotronClient  # noqa: E402
from eig_agent.seal import compare  # noqa: E402
from eig_agent.tests.test_m1 import FakeTransport, verdict  # noqa: E402

CERT = open(CERTIFIED, "rb").read()
FIX = os.path.join(os.path.dirname(CERTIFIED), "..")


def row(path, a, b):
    return {"path": path, "certified": a, "live": b, "kind": "changed"}


def ids(signals):
    return [s["id"] for s in signals]


class SignalTest(unittest.TestCase):
    def test_actor(self):
        self.assertEqual(ids(risk.actor({"submitted_by": "web-deploy-svc"})), ["RISK-ACTOR"])
        self.assertEqual(ids(risk.actor({"submitted_by": "x", "channel": "results API token"})), ["RISK-ACTOR"])
        self.assertEqual(ids(risk.actor({"submitted_by": "unknown"})), ["RISK-ACTOR"])
        self.assertEqual(risk.actor({"submitted_by": "m.alvarez", "role": "results supervisor",
                                     "channel": "results portal (two-person approval)"}), [])

    def test_off_hours_uses_local_time_of_the_change(self):
        self.assertEqual(ids(risk.off_hours({"submitted_at": "2026-11-04T03:12:09-10:00"})), ["RISK-HOURS"])
        self.assertEqual(ids(risk.off_hours({"submitted_at": "2026-11-04T00:00:00-10:00"})), ["RISK-HOURS"])
        self.assertEqual(risk.off_hours({"submitted_at": "2026-11-04T05:00:00-10:00"}), [])
        self.assertEqual(risk.off_hours({"submitted_at": "2026-11-05T13:45:00-10:00"}), [])
        self.assertEqual(risk.off_hours({"submitted_at": "not a time"}), [])

    def test_scope_mismatch(self):
        meta = {"ticket_text": "Dana Ironwood tally sheet read 312, correct value 321 (transposed digits)."}
        diff = [row("contests[id=mayor].candidates[1].votes", 14752, 15552)]
        self.assertEqual(ids(risk.scope(meta, diff)), ["RISK-SCOPE"])
        small = [row("contests[id=mayor].candidates[1].votes", 14752, 14761)]
        self.assertEqual(risk.scope(meta, small), [])

    def test_scope_ignores_dates_ids_and_code_refs(self):
        self.assertEqual(risk.ticket_numbers("CORR-2026-102 approved 2026-11-05 under code 12-7: 271 -> 244"),
                         [271, 244])

    def test_scope_ticket_without_figures(self):
        diff = [row("contests[id=charter-amendment-1].candidates[0].votes", 19304, 12898)]
        self.assertEqual(ids(risk.scope({"ticket_text": "Show No before Yes."}, diff)), ["RISK-SCOPE"])
        self.assertEqual(risk.scope({"ticket_text": "Title only."}, [row("contests[id=a].title", "x", "y")]), [])

    def test_one_sided_removal_under_cleanup_ticket(self):
        meta = {"ticket_text": "Batch 27-C (214 ballots) was scanned twice. Remove the duplicate batch."}
        diff = [row("contests[id=mayor].candidates[0].votes", 16284, 16070),
                row("contests[id=mayor].total_votes", 33630, 33416)]
        self.assertEqual(ids(risk.one_sided(meta, diff)), ["RISK-ONESIDED"])
        both = diff + [row("contests[id=mayor].candidates[1].votes", 14752, 14700)]
        self.assertEqual(risk.one_sided(meta, both), [])
        self.assertEqual(risk.one_sided({"ticket_text": "transposed digits"}, diff), [])

    def test_registered_voter_drop(self):
        self.assertEqual(ids(risk.registered_drop([row("turnout.registered_voters", 48210, 45210)])),
                         ["RISK-REGDROP"])
        self.assertEqual(risk.registered_drop([row("turnout.registered_voters", 48210, 48390)]), [])


class IsolationTest(unittest.TestCase):
    def test_fixture_signals(self):
        # Documents the signals per case; legitimate cases must stay quiet.
        expected = {"t04": [], "h01": [], "h08": [], "h13": [], "h11": [],
                    "h09": ["RISK-ONESIDED"], "h12": ["RISK-ACTOR", "RISK-HOURS", "RISK-REGDROP"],
                    "h03": ["RISK-SCOPE"], "h14": ["RISK-SCOPE"]}
        for cid, want in expected.items():
            sub = "cases" if cid.startswith("t") else ("heldout2" if int(cid[1:]) >= 8 else "heldout")
            case = load_case(os.path.join(FIX, sub, cid + ".json"))
            seal = compare(CERT, case_live_bytes(case, CERT))
            self.assertEqual(ids(risk.signals(seal, case["metadata"])), want, cid)

    def test_signals_do_not_reach_the_model_or_the_gate(self):
        case = load_case("t01")
        live = case_live_bytes(case, CERT)
        fake = FakeTransport(verdict("legitimate", 0.95, "human_review"), verdict("legitimate", 0.9, "human_review"))
        out = triage.run(CERT, live, case["metadata"], NemotronClient(fake), gate="v2")
        self.assertTrue(out["risk_signals"])
        self.assertEqual(out["decision"]["action"], "HUMAN_SIGNOFF")  # signals did not change the gate
        for sent in fake.sent:
            self.assertNotIn("RISK-", json.dumps(sent["messages"]))


if __name__ == "__main__":
    unittest.main()
