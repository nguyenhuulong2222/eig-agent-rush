import unittest

from checker.domains import host_of, is_allowed, levenshtein, lookalike_of, skeleton

OFFICIAL = ["kanaloacounty.example"]


class DomainTest(unittest.TestCase):
    def test_host_of(self):
        self.assertEqual(host_of("https://Elections.KanaloaCounty.example/x"), "elections.kanaloacounty.example")
        self.assertIsNone(host_of("results.html"))
        self.assertIsNone(host_of("tel:+18085550147"))
        self.assertIsNone(host_of("mailto:a@b.example"))

    def test_allowed_includes_subdomains_only(self):
        self.assertTrue(is_allowed("kanaloacounty.example", OFFICIAL))
        self.assertTrue(is_allowed("register.kanaloacounty.example", OFFICIAL))
        self.assertFalse(is_allowed("evilkanaloacounty.example", OFFICIAL))

    def test_lookalikes_detected(self):
        for host in (
            "elections.kanal0acounty.example",      # digit zero
            "elections.kanaioacounty.example",      # i for l (capital I lowercased)
            "kanaloa-county.example",               # hyphen
            "kanaloacounty-vote.example",           # extra word
            "kanaloacounty.example.evil.test",      # official name as a subdomain
            "kanaloacounty.vote",                   # different TLD
            "kanalaocounty.example",                # transposition
            "elections.kаnaloacounty.example",      # Cyrillic a
            "kanaloacouncy.example",                # one letter off
        ):
            self.assertEqual(lookalike_of(host, OFFICIAL), "kanaloacounty.example", host)

    def test_punycode_decoded(self):
        puny = "kаnaloacounty".encode("idna").decode("ascii")  # Cyrillic a -> xn--...
        self.assertTrue(puny.startswith("xn--"))
        host = host_of(f"https://elections.{puny}.example/")
        self.assertEqual(lookalike_of(host, OFFICIAL), "kanaloacounty.example")

    def test_unrelated_and_official_hosts_are_not_lookalikes(self):
        for host in ("gitlab.com", "www.example.org", "register.kanaloacounty.example", "kanaloacounty.example"):
            self.assertIsNone(lookalike_of(host, OFFICIAL), host)

    def test_helpers(self):
        self.assertEqual(skeleton("KANAL0A-COUNTY"), "kanaloacounty")
        self.assertEqual(levenshtein("kitten", "sitting"), 3)


if __name__ == "__main__":
    unittest.main()
