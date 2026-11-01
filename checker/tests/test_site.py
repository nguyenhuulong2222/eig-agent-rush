import unittest

from checker.htmlscan import parse_html
from checker.model import BLOCK, WARN
from checker.rules_site import (check_allowlist_change, check_assets, check_csp, check_links,
                                check_security_policy_change, parse_csp, scan_js)
from checker.tests.helpers import REPO, load_repo_json, rule_ids

ALLOW = load_repo_json("config/allowlist.json")
SECURITY = load_repo_json("config/security-policy.json")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; upgrade-insecure-requests")


def page(body="", csp=CSP, referrer=True):
    head = f'<meta http-equiv="Content-Security-Policy" content="{csp}">' if csp is not None else ""
    if referrer:
        head += '<meta name="referrer" content="strict-origin-when-cross-origin">'
    return parse_html(f"<!doctype html><html><head>{head}</head><body>{body}</body></html>")


class LinkTest(unittest.TestCase):
    def test_repo_pages_are_clean(self):
        for name in ("index", "results", "voter-info"):
            with open(f"{REPO}/public/{name}.html", encoding="utf-8") as fh:
                doc = parse_html(fh.read())
            findings = check_links(doc, name, ALLOW) + check_assets(doc, name, ALLOW) + \
                check_csp(doc, name, SECURITY, ALLOW, None)
            self.assertEqual(findings, [], name)

    def test_lookalike_official_link_blocks(self):
        doc = page('<a data-official-link="polling-place" href="https://elections.kanaloa-county.example/p">x</a>')
        findings = check_links(doc, "p.html", ALLOW)
        self.assertEqual(rule_ids(findings), ["LINK-002"])
        self.assertEqual(findings[0].severity, BLOCK)

    def test_lookalike_blocks_even_without_official_marker(self):
        doc = page('<a href="https://kanal0acounty.example/register">register</a>')
        self.assertEqual(rule_ids(check_links(doc, "p.html", ALLOW)), ["LINK-002"])

    def test_official_link_to_other_domain_blocks(self):
        doc = page('<a data-official-link="registration" href="https://vote-here.test/">x</a>')
        self.assertEqual(rule_ids(check_links(doc, "p.html", ALLOW)), ["LINK-001"])

    def test_official_link_must_be_https(self):
        doc = page('<a data-official-link="registration" href="http://register.kanaloacounty.example/">x</a>')
        self.assertEqual(rule_ids(check_links(doc, "p.html", ALLOW)), ["LINK-003"])
        doc = page('<a data-official-link="registration" href="/register">x</a>')
        self.assertEqual(rule_ids(check_links(doc, "p.html", ALLOW)), ["LINK-003"])

    def test_unofficial_external_links_are_fine(self):
        doc = page('<a href="https://gitlab.com/x">repo</a><a href="tel:+18085550147">call</a>')
        self.assertEqual(check_links(doc, "p.html", ALLOW), [])

    def test_javascript_url_blocks(self):
        doc = page('<a href="javascript:alert(1)">x</a>')
        self.assertEqual(rule_ids(check_links(doc, "p.html", ALLOW)), ["ASSET-005"])


class AssetTest(unittest.TestCase):
    def test_third_party_script_blocks(self):
        doc = page('<script src="https://cdn.analytics-tracker.test/t.js"></script>')
        self.assertEqual(rule_ids(check_assets(doc, "p.html", ALLOW)), ["ASSET-001"])

    def test_same_origin_script_is_fine(self):
        self.assertEqual(check_assets(page('<script src="app.js" defer></script>'), "p.html", ALLOW), [])

    def test_allowlisted_script_host_is_fine(self):
        allow = dict(ALLOW, script_sources=["cdn.kanaloacounty.example"])
        doc = page('<script src="https://cdn.kanaloacounty.example/a.js"></script>')
        self.assertEqual(check_assets(doc, "p.html", allow), [])

    def test_inline_script_blocks_and_is_scanned(self):
        doc = page("<script>\nvar x = 1;\neval(location.hash)\n</script>")
        ids = rule_ids(check_assets(doc, "p.html", ALLOW))
        self.assertEqual(ids, ["ASSET-002", "ASSET-006"])

    def test_iframe_and_event_handler_block(self):
        doc = page('<iframe src="https://maps.other.test/embed"></iframe><img src="a.png" onerror="go()">')
        self.assertEqual(rule_ids(check_assets(doc, "p.html", ALLOW)), ["ASSET-003", "ASSET-004"])

    def test_external_stylesheet_warns(self):
        findings = check_assets(page('<link rel="stylesheet" href="https://fonts.other.test/a.css">'), "p", ALLOW)
        self.assertEqual(rule_ids(findings), ["ASSET-008"])
        self.assertEqual(findings[0].severity, WARN)

    def test_js_scan(self):
        src = ("// eval( in a comment is ignored\n/* innerHTML = also ignored */\n"
               "el.textContent = s;\nel.innerHTML = s;\nnew Function('a', b)\nsetTimeout(\"go()\", 5)\n"
               "var u = 'https://example.test/x'; eval(u)\n")
        findings = scan_js(src, "a.js")
        self.assertEqual([(f.rule_id, f.line) for f in findings],
                         [("ASSET-007", 4), ("ASSET-006", 5), ("ASSET-006", 6), ("ASSET-006", 7)])

    def test_repo_js_is_clean(self):
        with open(f"{REPO}/public/app.js", encoding="utf-8") as fh:
            self.assertEqual(scan_js(fh.read(), "public/app.js"), [])


class CspTest(unittest.TestCase):
    def check(self, doc, base=None):
        return check_csp(doc, "p.html", SECURITY, ALLOW, base)

    def test_parse(self):
        csp = parse_csp("default-src 'none'; Script-Src 'self' HTTPS://CDN.test ;;")
        self.assertEqual(csp, {"default-src": ["'none'"], "script-src": ["'self'", "https://cdn.test"]})

    def test_missing_csp_blocks(self):
        self.assertEqual(rule_ids(self.check(page(csp=None))), ["CSP-001"])

    def test_unsafe_inline_blocks(self):
        doc = page(csp=CSP.replace("script-src 'self'", "script-src 'self' 'unsafe-inline'"))
        self.assertIn("CSP-003", rule_ids(self.check(doc)))

    def test_third_party_host_in_csp_blocks(self):
        doc = page(csp=CSP.replace("script-src 'self'", "script-src 'self' https://cdn.analytics-tracker.test"))
        self.assertIn("CSP-002", rule_ids(self.check(doc)))

    def test_required_directive_removed_blocks(self):
        doc = page(csp=CSP.replace("base-uri 'none'; ", ""))
        self.assertIn("CSP-002", rule_ids(self.check(doc)))

    def test_widening_against_base_blocks(self):
        base = page()
        head = page(csp=CSP + "; frame-src https://maps.other.test")
        self.assertIn("CSP-004", rule_ids(self.check(head, base)))

    def test_unchanged_csp_against_base_is_clean(self):
        self.assertEqual(self.check(page(), page()), [])

    def test_missing_referrer_warns(self):
        findings = self.check(page(referrer=False))
        self.assertEqual(rule_ids(findings), ["CSP-005"])
        self.assertEqual(findings[0].severity, WARN)


class PolicyFileTest(unittest.TestCase):
    def test_security_policy_loosened_blocks(self):
        head = load_repo_json("config/security-policy.json")
        head["required_directives"]["script-src"] = ["'self'", "'unsafe-inline'"]
        head["forbidden_source_expressions"].remove("'unsafe-inline'")
        self.assertEqual(rule_ids(check_security_policy_change(head, SECURITY, "s.json")), ["CSP-006"])
        self.assertEqual(len(check_security_policy_change(head, SECURITY, "s.json")), 2)

    def test_allowlist_widening_warns_and_lookalike_blocks(self):
        head = dict(ALLOW, official_link_domains=["kanaloacounty.example", "kanaloa-county.example"])
        findings = check_allowlist_change(head, ALLOW, "a.json")
        self.assertEqual(rule_ids(findings), ["ALLOW-001", "LINK-002"])

    def test_unchanged_policy_is_clean(self):
        self.assertEqual(check_security_policy_change(SECURITY, SECURITY, "s.json"), [])
        self.assertEqual(check_allowlist_change(ALLOW, ALLOW, "a.json"), [])


if __name__ == "__main__":
    unittest.main()
