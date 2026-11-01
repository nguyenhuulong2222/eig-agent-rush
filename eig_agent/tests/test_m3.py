import json
import os
import threading
import unittest
import urllib.error
import urllib.request

os.environ["EIG_USAGE_LOG"] = "off"

from eig_agent import demo  # noqa: E402
from eig_agent.llm import CallRecord  # noqa: E402


class LimitsTest(unittest.TestCase):
    def test_per_ip_and_daily_caps(self):
        lim = demo.Limits(daily=3, per_ip=2)
        t = 1_800_000_000.0
        self.assertIsNone(lim.take("a", t))
        self.assertIsNone(lim.take("a", t + 1))
        self.assertIn("per hour", lim.take("a", t + 2))
        self.assertIsNone(lim.take("a", t + 3601))  # the hour window slid
        self.assertIn("daily limit", lim.take("b", t + 3602))

    def test_serialize_totals(self):
        calls = [CallRecord("t", "reasoning", "m", 100, 50, 10, 900, 0.001, True, 1)]
        out = demo.serialize({"calls": calls})
        self.assertEqual(out["totals"]["model_calls"], 1)
        json.dumps(out)


class ClientIpTest(unittest.TestCase):
    def test_spoofed_leading_entries_are_ignored(self):
        from eig_agent.server import client_ip
        # Client sent "1.2.3.4"; Render appended the real address it saw.
        self.assertEqual(client_ip("1.2.3.4, 203.0.113.7", "10.0.0.5", 1), "203.0.113.7")
        self.assertEqual(client_ip("1.2.3.4, 5.6.7.8, 203.0.113.7", "10.0.0.5", 1), "203.0.113.7")

    def test_render_chain_observed_in_production(self):
        from eig_agent.server import client_ip
        # Seen on Render: visitor, Cloudflare edge, Render internal proxy (a spoofed entry lands first).
        self.assertEqual(client_ip("198.51.100.23, 172.71.146.176, 10.194.13.185", "10.0.0.1", 3), "198.51.100.23")
        self.assertEqual(client_ip("203.0.113.99, 198.51.100.23, 172.71.146.85, 10.26.163.59", "10.0.0.1", 3),
                         "198.51.100.23")

    def test_two_proxy_hops(self):
        from eig_agent.server import client_ip
        self.assertEqual(client_ip("1.2.3.4, 203.0.113.7, 10.1.1.1", "10.0.0.5", 2), "203.0.113.7")

    def test_missing_or_short_header_falls_back_to_peer(self):
        from eig_agent.server import client_ip
        self.assertEqual(client_ip("", "10.0.0.5", 1), "10.0.0.5")
        self.assertEqual(client_ip("203.0.113.7", "10.0.0.5", 2), "10.0.0.5")
        self.assertEqual(client_ip("1.2.3.4", "10.0.0.5", 0), "10.0.0.5")

    def test_distinct_visitors_get_distinct_limits(self):
        from eig_agent.server import client_ip
        lim = demo.Limits(daily=100, per_ip=1)
        a = client_ip("spoof, 198.51.100.1", "10.0.0.5", 1)
        b = client_ip("spoof, 198.51.100.2", "10.0.0.5", 1)
        self.assertIsNone(lim.take(a))
        self.assertIsNone(lim.take(b))
        self.assertIsNotNone(lim.take(client_ip("other-spoof, 198.51.100.1", "10.0.0.5", 1)))


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from eig_agent.server import Handler
        Handler.log_message = lambda *a: None
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def post(self, body):
        req = urllib.request.Request(self.base + "/api/run", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    @unittest.skipUnless(os.path.exists(os.path.join(demo.RECORDED, "tamper.json")),
                         "recorded runs are re-recorded against Vultr Inference (M4)")
    def test_scenarios_and_recorded_replay_need_no_keys(self):
        with urllib.request.urlopen(self.base + "/api/scenarios") as resp:
            data = json.load(resp)
        self.assertEqual({r["role"] for r in data["routing"]}, {"FAST", "REASONING", "REPORT"})
        status, job = self.post({"scenario": "tamper"})
        self.assertEqual(status, 200)
        self.assertEqual(job["result"]["decision"]["action"], "ROLLBACK")
        self.assertFalse(job["live"])

    def test_unknown_scenario_and_big_body_rejected(self):
        self.assertEqual(self.post({"scenario": "nope"})[0], 400)
        self.assertEqual(self.post({"scenario": "x" * 3000})[0], 413)

    def test_index_has_strict_csp_and_no_keys(self):
        with urllib.request.urlopen(self.base + "/") as resp:
            csp = resp.headers["Content-Security-Policy"]
            html = resp.read().decode()
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe", csp)
        self.assertIn("Fictional demo data", html)
        for name in ("app.js", "index.html", "styles.css"):
            with open(os.path.join(demo.PKG, "web", name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("NEBIUS_API_KEY", text)
            self.assertNotIn("VULTR_INFERENCE_API_KEY", text)


if __name__ == "__main__":
    unittest.main()
