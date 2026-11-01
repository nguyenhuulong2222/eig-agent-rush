import os
import unittest

os.environ["EIG_USAGE_LOG"] = "off"

from eig_agent import multilingual  # noqa: E402
from eig_agent.llm import LLMClient  # noqa: E402
from eig_agent.pages import visible_text  # noqa: E402
from eig_agent.schemas import ELECTION_FACTS, FACT_KEYS, validate  # noqa: E402
from eig_agent.tests.test_m1 import FakeTransport  # noqa: E402

CERT = multilingual.load_certified()
EN = visible_text(multilingual.load_pages("consistent")["en"])
QUOTES = {"election_day": "Tuesday, November 3, 2026", "polls_open": "7:00 am", "polls_close": "7:00 pm",
          "drop_box_close": "open until 7:00 pm", "mail_ballots_sent": "Tuesday, October 13, 2026",
          "service_centers_open": "Tuesday, October 20, 2026", "certification_date": "Tuesday, November 17, 2026",
          "polling_place_site": "Find your polling place: elections.kanaloacounty.example",
          "registration_site": "register.kanaloacounty.example", "phone": "(808) 555-0147"}


def facts(**overrides):
    out = {"language": "English",
           "facts": {k: {"value": CERT[k], "quote": QUOTES[k]} for k in FACT_KEYS},
           "drop_box_addresses": [{"value": a, "quote": a} for a in CERT["drop_box_addresses"]]}
    for key, (value, quote) in overrides.items():
        out["facts"][key] = {"value": value, "quote": quote}
    return out


class CompareTest(unittest.TestCase):
    def test_fixture_facts_are_schema_valid(self):
        self.assertEqual(validate(facts(), ELECTION_FACTS), [])

    def test_consistent_page_has_no_mismatch(self):
        self.assertEqual(multilingual.compare("en", facts(), EN, CERT), [])

    def test_equivalent_formats_are_not_mismatches(self):
        f = facts(polls_close=("7:00", "7:00 pm"), phone=("1-808-555-0147", "(808) 555-0147"),
                  polling_place_site=("https://elections.kanaloacounty.example/", "elections.kanaloacounty.example"))
        f["facts"]["polls_close"]["value"] = "19:00"
        self.assertEqual(multilingual.compare("en", f, EN, CERT), [])

    def test_wrong_value_with_real_quote_is_wrong(self):
        page = EN.replace("November 3", "November 4")
        rows = multilingual.compare("en", facts(election_day=("2026-11-04", "Tuesday, November 4, 2026")), page, CERT)
        self.assertEqual([(r["field"], r["kind"]) for r in rows], [("election_day", "wrong")])

    def test_quote_not_on_page_is_unverified_not_wrong(self):
        rows = multilingual.compare("en", facts(election_day=("2026-11-04", "Wednesday, November 4")), EN, CERT)
        self.assertEqual([(r["field"], r["kind"]) for r in rows], [("election_day", "unverified")])

    def test_lookalike_site_is_annotated(self):
        page = EN.replace("elections.kanaloacounty", "elections.kanaloa-county")
        rows = multilingual.compare("en", facts(polling_place_site=(
            "elections.kanaloa-county.example", "elections.kanaloa-county.example")), page, CERT)
        self.assertEqual(rows[0]["lookalike_of"], "kanaloacounty.example")

    def test_missing_fact(self):
        rows = multilingual.compare("en", facts(phone=("", "")), EN, CERT)
        self.assertEqual([(r["field"], r["kind"]) for r in rows], [("phone", "missing")])

    def test_scenarios_apply_and_bad_replacement_fails(self):
        drift = multilingual.load_pages("drift")
        self.assertIn("Nobyembre 4", drift["tl"])
        with self.assertRaises(ValueError):
            multilingual.apply_replacements({"en": [["no such text", "x"]]})


class MultilingualRunTest(unittest.TestCase):
    def test_no_explain_call_when_consistent(self):
        fake = FakeTransport(facts())
        out = multilingual.run({"en": multilingual.load_pages("consistent")["en"]}, LLMClient(fake))
        self.assertEqual(out["mismatches"], [])
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0]["schema_name"], "election_facts")


if __name__ == "__main__":
    unittest.main()
