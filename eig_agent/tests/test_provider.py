"""M2: Vultr provider, MOCK provider, spend cap, model listing, web/orchestrator separation."""

import json
import re
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

os.environ["EIG_USAGE_LOG"] = "off"

from eig_agent import config, models, triage  # noqa: E402
from eig_agent.__main__ import CERTIFIED, case_live_bytes, load_case  # noqa: E402
from eig_agent.llm import LLMClient, LLMError, SpendCap, SpendCapReached, load_env  # noqa: E402
from eig_agent.schemas import TAMPER_TRIAGE  # noqa: E402
from eig_agent.tests.test_m1 import FakeTransport, verdict  # noqa: E402

CERT = open(CERTIFIED, "rb").read()
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOCK_ENV = {"EIG_LLM_PROVIDER": "mock", "EIG_USAGE_LOG": "off"}
VULTR_ENV = {"EIG_LLM_PROVIDER": "vultr", "MODEL_REASONING": "vendor/reasoner", "MODEL_VISION": "",
             "VULTR_INFERENCE_API_KEY": "test-key-not-real", "EIG_USAGE_LOG": "off",
             "EIG_PRICE_IN_PER_MTOK": "1", "EIG_PRICE_OUT_PER_MTOK": "1"}


class MockProviderTest(unittest.TestCase):
    def setUp(self):
        self.live = case_live_bytes(load_case("t01"), CERT)

    def test_mock_is_default_free_and_escalates(self):
        with mock.patch.dict(os.environ, MOCK_ENV, clear=False):
            os.environ.pop("EIG_MOCK_VERDICT", None)
            out = triage.run(CERT, self.live, {}, LLMClient(spend=SpendCap(0.0)), gate="v2")
        self.assertEqual(out["decision"]["action"], "ESCALATE")
        self.assertTrue(all(c.model == "mock" and c.cost_usd == 0 for c in out["calls"]))
        self.assertIn("[MOCK]", out["votes"][0]["summary"])

    def test_mock_verdict_drives_gate_branches(self):
        for v, action in (("tampering", "ROLLBACK"), ("legitimate", "HUMAN_SIGNOFF")):
            with mock.patch.dict(os.environ, {**MOCK_ENV, "EIG_MOCK_VERDICT": v}):
                out = triage.run(CERT, self.live, {}, LLMClient(), gate="v2")
            self.assertEqual(out["decision"]["action"], action, v)
        self.assertIn("[MOCK]", out["votes"][0]["summary"])

    def test_seal_match_never_calls_mock_either(self):
        with mock.patch.dict(os.environ, MOCK_ENV):
            out = triage.run(CERT, CERT, {}, LLMClient(), gate="v2")
        self.assertEqual((out["decision"]["action"], out["calls"]), ("NONE", []))


class VultrTransportTest(unittest.TestCase):
    def fake_openai(self, captured):
        class Completions:
            def create(self, **kw):
                captured["create"] = kw
                msg = types.SimpleNamespace(content=json.dumps(verdict()))
                usage = types.SimpleNamespace(prompt_tokens=1000, completion_tokens=500, completion_tokens_details=None)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")],
                                             usage=usage)

        class OpenAI:
            def __init__(self, **kw):
                captured["client"] = kw
                self.chat = types.SimpleNamespace(completions=Completions())
        return types.SimpleNamespace(OpenAI=OpenAI)

    def test_request_goes_to_vultr_with_schema_and_no_vendor_extras(self):
        captured = {}
        with mock.patch.dict(os.environ, VULTR_ENV), mock.patch.dict(sys.modules, {"openai": self.fake_openai(captured)}):
            res = LLMClient(spend=SpendCap(1.0)).complete_json(task="t", role="reasoning", system="s", user="u",
                                                               schema=TAMPER_TRIAGE, schema_name="tamper_triage")
        self.assertEqual(captured["client"]["base_url"], "https://api.vultrinference.com/v1")
        self.assertEqual(captured["client"]["api_key"], "test-key-not-real")
        self.assertEqual(captured["create"]["model"], "vendor/reasoner")
        self.assertEqual(captured["create"]["response_format"]["type"], "json_schema")
        self.assertNotIn("extra_body", captured["create"])
        self.assertAlmostEqual(res.calls[0].cost_usd, 0.0015)

    def test_reasoning_effort_is_sent_only_when_set(self):
        for value, expected in (("low", {"reasoning_effort": "low"}), ("", None), ("bogus", None)):
            captured = {}
            env = {**VULTR_ENV, "EIG_REASONING_EFFORT": value}
            with mock.patch.dict(os.environ, env), mock.patch.dict(sys.modules, {"openai": self.fake_openai(captured)}):
                LLMClient(spend=SpendCap(1.0)).complete_json(task="t", role="reasoning", system="s", user="u",
                                                             schema=TAMPER_TRIAGE, schema_name="x")
            self.assertEqual(captured["create"].get("extra_body"), expected, value)

    def test_response_format_can_be_relaxed(self):
        captured = {}
        env = {**VULTR_ENV, "EIG_RESPONSE_FORMAT": "none"}
        with mock.patch.dict(os.environ, env), mock.patch.dict(sys.modules, {"openai": self.fake_openai(captured)}):
            LLMClient(spend=SpendCap(1.0)).complete_json(task="t", role="reasoning", system="s", user="u",
                                                         schema=TAMPER_TRIAGE, schema_name="x")
        self.assertNotIn("response_format", captured["create"])

    def test_missing_key_is_a_clear_error(self):
        env = {**VULTR_ENV, "VULTR_INFERENCE_API_KEY": ""}
        with mock.patch.dict(os.environ, env), mock.patch("eig_agent.llm.load_env"), \
                mock.patch.dict(sys.modules, {"openai": self.fake_openai({})}):
            with self.assertRaises(LLMError) as ctx:
                LLMClient().transport
        self.assertIn("VULTR_INFERENCE_API_KEY", str(ctx.exception))

    def test_unknown_provider_rejected(self):
        with mock.patch.dict(os.environ, {"EIG_LLM_PROVIDER": "some-other-cloud"}):
            with self.assertRaises(LLMError):
                LLMClient().transport


class RolesTest(unittest.TestCase):
    def test_vision_needs_a_model_and_images_need_vision(self):
        with mock.patch.dict(os.environ, VULTR_ENV):
            client = LLMClient(FakeTransport(verdict()), spend=SpendCap(1.0))
            self.assertFalse(config.has_vision())
            with self.assertRaisesRegex(LLMError, "MODEL_VISION"):
                client.complete_json(task="t", role="vision", system="s", user="u", schema=TAMPER_TRIAGE,
                                     schema_name="x", images=[b"png"])
            with self.assertRaisesRegex(LLMError, "vision role"):
                client.complete_json(task="t", role="reasoning", system="s", user="u", schema=TAMPER_TRIAGE,
                                     schema_name="x", images=[b"png"])

    def test_images_are_sent_as_data_urls(self):
        env = {**VULTR_ENV, "MODEL_VISION": "vendor/vl"}
        fake = FakeTransport(verdict())
        with mock.patch.dict(os.environ, env):
            LLMClient(fake, spend=SpendCap(1.0)).complete_json(task="t", role="vision", system="s", user="u",
                                                               schema=TAMPER_TRIAGE, schema_name="x",
                                                               images=[b"\x89PNG"])
        parts = fake.sent[0]["messages"][1]["content"]
        self.assertEqual(fake.sent[0]["model"], "vendor/vl")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))


class RolePricesTest(unittest.TestCase):
    ENV = {"EIG_PRICE_IN_PER_MTOK": "2", "EIG_PRICE_OUT_PER_MTOK": "4",
           "EIG_PRICE_REASONING_IN_PER_MTOK": "", "EIG_PRICE_REASONING_OUT_PER_MTOK": "",
           "EIG_PRICE_VISION_IN_PER_MTOK": "", "EIG_PRICE_VISION_OUT_PER_MTOK": ""}

    def test_unset_prices_use_the_conservative_ceiling(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("EIG_PRICE_")}
        with mock.patch.dict(os.environ, clean, clear=True):
            for role in ("reasoning", "vision", "fast", "report"):
                self.assertEqual(config.prices(role), (2e-6, 6e-6), role)
        example = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
        self.assertIn("CONSERVATIVE CEILING, NOT VULTR LIST PRICES", example)
        for side, value in (("IN", "2.00"), ("OUT", "6.00")):
            self.assertEqual(len(re.findall(rf"(?m)^EIG_PRICE_(?:REASONING_|VISION_|TRIAGE_2_)?{side}_PER_MTOK={value}$",
                                            example)), 4)

    def test_falls_back_to_the_shared_pair(self):
        with mock.patch.dict(os.environ, self.ENV):
            for role in ("fast", "reasoning", "report", "vision", None):
                self.assertEqual(config.prices(role), (2e-6, 4e-6), role)

    def test_role_prices_override_each_side_separately(self):
        env = {**self.ENV, "EIG_PRICE_REASONING_IN_PER_MTOK": "0.5", "EIG_PRICE_VISION_IN_PER_MTOK": "1",
               "EIG_PRICE_VISION_OUT_PER_MTOK": "9"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(config.prices("reasoning"), (0.5e-6, 4e-6))  # OUT falls back to shared
            self.assertEqual(config.prices("fast"), (0.5e-6, 4e-6))       # fast/report bill as reasoning
            self.assertEqual(config.prices("vision"), (1e-6, 9e-6))
            self.assertAlmostEqual(config.cost_usd("m", 1_000_000, 1_000_000, "vision"), 10.0)
            self.assertEqual(config.cost_usd("mock", 10**6, 10**6, "vision"), 0.0)

    def test_client_bills_each_call_at_its_role_price(self):
        env = {**VULTR_ENV, **self.ENV, "MODEL_VISION": "vendor/vl", "EIG_PRICE_VISION_IN_PER_MTOK": "10",
               "EIG_PRICE_VISION_OUT_PER_MTOK": "10"}
        with mock.patch.dict(os.environ, env):
            client = LLMClient(FakeTransport(verdict(), verdict()), spend=SpendCap(1.0))
            r = client.complete_json(task="t", role="reasoning", system="s", user="u",
                                     schema=TAMPER_TRIAGE, schema_name="x")
            v = client.complete_json(task="t", role="vision", system="s", user="u",
                                     schema=TAMPER_TRIAGE, schema_name="x", images=[b"png"])
        self.assertAlmostEqual(r.calls[0].cost_usd, 1000 * 2e-6 + 200 * 4e-6)  # shared pair
        self.assertAlmostEqual(v.calls[0].cost_usd, 1200 * 10e-6)             # vision price


class SecondVerdictTest(unittest.TestCase):
    def setUp(self):
        self.live = case_live_bytes(load_case("t01"), CERT)

    def test_second_verdict_uses_its_own_model_and_price(self):
        env = {**VULTR_ENV, "MODEL_TRIAGE_2": "other/family", "EIG_PRICE_TRIAGE_2_IN_PER_MTOK": "10",
               "EIG_PRICE_TRIAGE_2_OUT_PER_MTOK": "10"}
        fake = FakeTransport(verdict(), verdict(), {"incident_report_md": "r"})
        with mock.patch.dict(os.environ, env):
            self.assertEqual(config.routes()["triage_2"].model, "other/family")
            out = triage.run(CERT, self.live, {}, LLMClient(fake, spend=SpendCap(1.0)), gate="v2")
        self.assertEqual(out["vote_models"], ["vendor/reasoner", "other/family"])
        self.assertFalse(out["votes_same_model"])
        self.assertEqual({s["model"] for s in fake.sent[:2]}, {"vendor/reasoner", "other/family"})
        by_task = {c.task: c for c in out["calls"]}
        self.assertAlmostEqual(by_task["tamper_triage#2"].cost_usd, 1200 * 10e-6)   # its own price
        self.assertEqual(out["decision"]["action"], "ROLLBACK")
        self.assertEqual(out["decision"]["vote_models"], ["vendor/reasoner", "other/family"])

    def test_without_a_second_model_the_gate_says_so(self):
        env = {**VULTR_ENV, "MODEL_TRIAGE_2": ""}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(config.routes()["triage_2"].model, "vendor/reasoner")
            out = triage.run(CERT, self.live, {}, LLMClient(FakeTransport(verdict(), verdict(), {"incident_report_md": "r"}),
                                                            spend=SpendCap(1.0)), gate="v2")
        self.assertTrue(out["votes_same_model"])
        self.assertIn("same model", out["decision"]["note"])

    def test_a_failed_second_verdict_escalates(self):
        env = {**VULTR_ENV, "MODEL_TRIAGE_2": "other/family"}
        with mock.patch.dict(os.environ, env):
            out = triage.run(CERT, self.live, {}, LLMClient(FakeTransport(verdict(), TimeoutError("down")),
                                                            spend=SpendCap(1.0)), gate="v2")
        self.assertEqual(out["decision"]["action"], "ESCALATE")


class SpendCapTest(unittest.TestCase):
    def test_default_cap_is_fifty_cents_until_prices_are_real(self):
        env = {k: v for k, v in os.environ.items() if k != "EIG_DAILY_SPEND_CAP_USD"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.daily_spend_cap_usd(), 0.5)
            self.assertEqual(SpendCap().cap, 0.5)
        example = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
        self.assertRegex(example, r"(?m)^EIG_DAILY_SPEND_CAP_USD=0\.50\b")

    def test_refuses_before_sending_when_cap_would_be_passed(self):
        fake = FakeTransport(verdict())
        with mock.patch.dict(os.environ, VULTR_ENV):
            with self.assertRaises(SpendCapReached):
                LLMClient(fake, spend=SpendCap(0.0001)).complete_json(
                    task="t", role="reasoning", system="s", user="u", schema=TAMPER_TRIAGE, schema_name="x")
        self.assertEqual(fake.sent, [])

    def test_settles_to_actual_cost(self):
        cap = SpendCap(1.0)
        with mock.patch.dict(os.environ, VULTR_ENV):
            LLMClient(FakeTransport(verdict()), spend=cap).complete_json(
                task="t", role="reasoning", system="s", user="u", schema=TAMPER_TRIAGE, schema_name="x")
        self.assertAlmostEqual(cap.status()["spent_usd"], 0.0012)  # 1000 in + 200 out at $1/M
        self.assertEqual(cap.reserved, 0.0)

    def test_restart_keeps_todays_spend_from_the_usage_log(self):
        import datetime as dt
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "usage.jsonl")
            with open(path, "w") as fh:
                fh.write(json.dumps({"ts": today + "T01:00:00+00:00", "cost_usd": 0.4}) + "\n")
                fh.write(json.dumps({"ts": "2000-01-01T01:00:00+00:00", "cost_usd": 9}) + "\n")
            with mock.patch.dict(os.environ, {"EIG_USAGE_LOG": path}):
                cap = SpendCap(0.5)
                self.assertAlmostEqual(cap.status()["spent_usd"], 0.4)
                self.assertIsNotNone(cap.reserve(0.2))
                self.assertIsNone(cap.reserve(0.05))


class ModelsTest(unittest.TestCase):
    def test_vision_from_metadata_beats_name(self):
        self.assertEqual(models.vision_support({"id": "x-vl", "input_modalities": ["text"]}),
                         (False, "metadata: input_modalities"))
        self.assertTrue(models.vision_support({"id": "plain", "capabilities": {"vision": True,
                                                                                "kind": "image-to-text"}})[0])

    def test_vision_from_name_when_no_metadata(self):
        for mid in ("qwen/Qwen2.5-VL-7B-Instruct", "meta-llama/Llama-3.2-11B-Vision-Instruct", "llava-1.6"):
            self.assertEqual(models.vision_support({"id": mid}), (True, "name only"), mid)
        for mid in ("deepseek-ai/DeepSeek-R1", "meta-llama/Llama-3.3-70B-Instruct", "devstral"):
            self.assertFalse(models.vision_support({"id": mid})[0], mid)

    def test_report_picks_ocr_fallback_without_vision(self):
        out = models.report([{"id": "deepseek-r1"}, {"id": "llama-3.3-70b"}])
        self.assertEqual(out["vision_models"], [])
        self.assertIn("OCR", out["path"])


class EnvFileTest(unittest.TestCase):
    def test_compose_style_comment_on_empty_value_counts_as_empty(self):
        env = {**VULTR_ENV, "MODEL_VISION": "# empty = OCR text from the sandbox goes to MODEL_REASONING",
               "MODEL_REASONING": "vendor/reasoner   # chosen on Nov 3", "EIG_EVIDENCE_PATH": "auto  # auto | vision"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(config.routes()["vision"].model, "")
            self.assertFalse(config.has_vision())
            self.assertEqual(config.routes()["reasoning"].model, "vendor/reasoner")
            self.assertEqual(config.env("EIG_EVIDENCE_PATH"), "auto")
            self.assertEqual(config.env("NOT_SET_AT_ALL", "d"), "d")

    def test_example_never_comments_an_empty_value(self):
        example = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
        self.assertEqual(re.findall(r"(?m)^[A-Z_]+=[ \t]*#.*$", example), [])

    def test_inline_comments_and_quotes(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write('EIG_T_A=60          # a comment\nEIG_T_B="quoted"\n# EIG_T_C=x\n')
        try:
            with mock.patch.dict(os.environ, {}):
                load_env(fh.name)
                self.assertEqual((os.environ["EIG_T_A"], os.environ["EIG_T_B"]), ("60", "quoted"))
                self.assertNotIn("EIG_T_C", os.environ)
        finally:
            os.unlink(fh.name)


class SeparationTest(unittest.TestCase):
    def test_web_imports_no_orchestrator_code(self):
        code = ("import sys, eig_agent.web; "
                "bad = [m for m in sys.modules if m.split('.')[0] == 'openai' or m in "
                "('eig_agent.llm', 'eig_agent.config', 'eig_agent.demo', 'eig_agent.server')]; "
                "print(','.join(bad))")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "")

    def test_no_secret_names_in_static_ui(self):
        web = os.path.join(ROOT, "eig_agent", "web")
        for name in os.listdir(web):
            with open(os.path.join(web, name), encoding="utf-8") as fh:
                self.assertNotIn("VULTR_INFERENCE_API_KEY", fh.read(), name)


if __name__ == "__main__":
    unittest.main()
