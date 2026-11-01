import json
import os
import unittest

os.environ["EIG_USAGE_LOG"] = "off"

from eig_agent import triage  # noqa: E402
from eig_agent.__main__ import CASES, CERTIFIED, case_live_bytes, load_case  # noqa: E402
from eig_agent.llm import LLMError, LLMClient, parse_json  # noqa: E402
from eig_agent.schemas import TAMPER_TRIAGE, validate  # noqa: E402
from eig_agent.seal import apply_patches, compare, diff, flatten  # noqa: E402

CERT = open(CERTIFIED, "rb").read()


def verdict(v="tampering", conf=0.93, action="rollback_to_certified"):
    return {"verdict": v, "confidence": conf, "recommended_action": action, "summary": "s",
            "evidence": [{"change": "a: 1 -> 2", "observation": "o", "points_to": "tampering"}]}


class FakeTransport:
    """Returns queued replies and records what was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []

    def __call__(self, **kwargs):
        self.sent.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return {"content": content, "finish_reason": "stop", "prompt_tokens": 1000,
                "completion_tokens": 200, "reasoning_tokens": 50, "reasoning_chars": 10}


class SealTest(unittest.TestCase):
    def test_match_needs_no_model(self):
        fake = FakeTransport()
        out = triage.run(CERT, CERT, {}, LLMClient(fake), gate="v1")
        self.assertTrue(out["seal"]["match"])
        self.assertEqual(out["decision"]["action"], "NONE")
        self.assertEqual(fake.sent, [])

    def test_patch_and_diff_roundtrip(self):
        doc = json.loads(CERT)
        live = apply_patches(doc, [{"path": "contests[id=mayor].candidates[1].votes", "value": 1}])
        rows = diff(doc, live)
        self.assertEqual(rows, [{"path": "contests[id=mayor].candidates[1].votes",
                                 "certified": 14752, "live": 1, "kind": "changed"}])
        self.assertIn("contests[id=mayor].candidates[0].name", flatten(doc))

    def test_arithmetic_violation_blocks_deterministically(self):
        live = case_live_bytes(load_case("t08"), CERT)
        seal = compare(CERT, live)
        self.assertFalse(seal["match"])
        self.assertTrue(seal["blocking"])

    def test_consistent_tamper_passes_arithmetic(self):
        # The attacker kept totals consistent: only Nemotron's reasoning can catch T01.
        seal = compare(CERT, case_live_bytes(load_case("t01"), CERT))
        self.assertFalse(seal["blocking"])

    def test_all_fixture_cases_apply(self):
        for name in sorted(os.listdir(CASES)):
            case = load_case(os.path.join(CASES, name))
            self.assertIn(case["expected"], ("legitimate", "tampering", "uncertain"))
            self.assertFalse(compare(CERT, case_live_bytes(case, CERT))["match"], name)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.live = case_live_bytes(load_case("t01"), CERT)

    def test_tampering_high_confidence_rolls_back_and_reports(self):
        fake = FakeTransport(verdict(), {"incident_report_md": "## Summary\nx"})
        out = triage.run(CERT, self.live, {}, LLMClient(fake), gate="v1")
        self.assertEqual(out["decision"]["action"], "ROLLBACK")
        self.assertEqual(out["incident_report_md"], "## Summary\nx")
        self.assertEqual([s["schema_name"] for s in fake.sent], ["tamper_triage", "incident_report"])
        self.assertEqual(len(out["calls"]), 2)

    def test_low_confidence_tampering_escalates_without_report(self):
        fake = FakeTransport(verdict(conf=0.6))
        out = triage.run(CERT, self.live, {}, LLMClient(fake), gate="v1")
        self.assertEqual(out["decision"]["action"], "ESCALATE")
        self.assertIsNone(out["incident_report_md"])
        self.assertEqual(len(fake.sent), 1)

    def test_uncertain_escalates(self):
        fake = FakeTransport(verdict("uncertain", 0.9, "escalate_to_human"))
        self.assertEqual(triage.run(CERT, self.live, {}, LLMClient(fake), gate="v1")["decision"]["action"], "ESCALATE")

    def test_legitimate_needs_human_signoff(self):
        fake = FakeTransport(verdict("legitimate", 0.95, "human_review"))
        self.assertEqual(triage.run(CERT, self.live, {}, LLMClient(fake), gate="v1")["decision"]["action"],
                         "HUMAN_SIGNOFF")

    def test_invalid_json_retries_once_then_escalates(self):
        fake = FakeTransport("not json", {"verdict": "maybe"})
        out = triage.run(CERT, self.live, {}, LLMClient(fake), gate="v1")
        self.assertEqual(len(fake.sent), 2)
        self.assertEqual(out["decision"]["action"], "ESCALATE")
        self.assertIn("no valid JSON", out["triage_error"])
        self.assertEqual(len(out["calls"]), 2)

    def test_transport_error_escalates(self):
        out = triage.run(CERT, self.live, {}, LLMClient(FakeTransport(TimeoutError("slow"))), gate="v1")
        self.assertEqual(out["decision"]["action"], "ESCALATE")

    def test_deterministic_block_rolls_back_even_if_model_says_legitimate(self):
        live = case_live_bytes(load_case("t08"), CERT)
        fake = FakeTransport(verdict("legitimate", 0.99, "no_action"), {"incident_report_md": "r"})
        out = triage.run(CERT, live, {}, LLMClient(fake), gate="v1")
        self.assertEqual(out["decision"]["action"], "ROLLBACK")


class FrozenPromptTest(unittest.TestCase):
    def test_triage_prompt_is_frozen_for_the_heldout_set(self):
        # See fixtures/heldout/README.md. Changing the prompt invalidates the held-out results.
        import hashlib
        self.assertEqual(hashlib.sha256(triage.TRIAGE_SYSTEM.encode()).hexdigest(),
                         "588f117b4a2d2f8ecfe90b1944ab2387e7f7270570fa24fa7d4f8f85c1b564d7")

    def test_heldout_cases_apply(self):
        heldout = os.path.join(os.path.dirname(CASES), "heldout")
        names = [n for n in sorted(os.listdir(heldout)) if n.endswith(".json")]
        self.assertGreaterEqual(len(names), 6)
        for name in names:
            case = load_case(os.path.join(heldout, name))
            self.assertFalse(compare(CERT, case_live_bytes(case, CERT))["match"], name)


class GateV2Test(unittest.TestCase):
    def setUp(self):
        self.live = case_live_bytes(load_case("t01"), CERT)

    def run_v2(self, *replies):
        fake = FakeTransport(*replies)
        return triage.run(CERT, self.live, {}, LLMClient(fake), gate="v2"), fake

    def test_two_agreeing_tampering_votes_roll_back(self):
        out, fake = self.run_v2(verdict(conf=0.9), verdict(conf=0.85), {"incident_report_md": "r"})
        self.assertEqual(out["decision"]["action"], "ROLLBACK")
        self.assertEqual(len(out["votes"]), 2)
        self.assertEqual(len(fake.sent), 3)

    def test_disagreement_escalates(self):
        out, _ = self.run_v2(verdict(conf=0.95), verdict("legitimate", 0.9, "human_review"))
        self.assertEqual(out["decision"]["action"], "ESCALATE")
        self.assertIsNone(out["incident_report_md"])

    def test_one_vote_below_threshold_escalates(self):
        out, _ = self.run_v2(verdict(conf=0.95), verdict(conf=0.7))
        self.assertEqual(out["decision"]["action"], "ESCALATE")

    def test_both_legitimate_needs_signoff(self):
        out, _ = self.run_v2(verdict("legitimate", 0.9, "human_review"), verdict("legitimate", 0.8, "human_review"))
        self.assertEqual(out["decision"]["action"], "HUMAN_SIGNOFF")

    def test_one_invalid_vote_escalates(self):
        out, fake = self.run_v2(verdict(), "bad", "still bad")
        self.assertEqual(out["decision"]["action"], "ESCALATE")
        # Thread interleaving decides which vote gets which reply; every interleaving must escalate.
        self.assertGreaterEqual(len(fake.sent), 3)


class ParsingTest(unittest.TestCase):
    def test_parse_tolerates_fences_and_think(self):
        self.assertEqual(parse_json('<think>hm</think>\n```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json('\n\n{"a": 1}'), {"a": 1})

    def test_schema_rejects_bad_values(self):
        bad = verdict()
        bad["confidence"] = 1.5
        bad["extra"] = True
        errors = validate(bad, TAMPER_TRIAGE)
        self.assertTrue(any("1.5" in e for e in errors))
        self.assertTrue(any("extra" in e for e in errors))
        self.assertEqual(validate(verdict(), TAMPER_TRIAGE), [])
        self.assertTrue(validate(dict(verdict(), confidence=True), TAMPER_TRIAGE))

    def test_length_cutoff_is_an_error(self):
        class Cut(FakeTransport):
            def __call__(self, **kw):
                r = super().__call__(**kw)
                r["finish_reason"] = "length"
                return r
        with self.assertRaises(LLMError) as ctx:
            LLMClient(Cut("{", "{")).complete_json(task="t", role="fast", system="s", user="u",
                                                         schema=TAMPER_TRIAGE, schema_name="x")
        self.assertEqual(len(ctx.exception.calls), 2)


if __name__ == "__main__":
    unittest.main()
