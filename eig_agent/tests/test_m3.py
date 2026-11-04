import json
import os
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

os.environ["EIG_USAGE_LOG"] = "off"
TOKEN = "test-approver-token-0123456789"

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
    """Browser -> web container (static + forwarder) -> orchestrator API, both on loopback."""

    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from eig_agent.server import ApiHandler
        from eig_agent.web import WebHandler
        ApiHandler.log_message = WebHandler.log_message = lambda *a: None
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        upstream = f"http://127.0.0.1:{cls.api.server_address[1]}"
        cls.Web = type("Web", (WebHandler,), {"upstream": upstream})
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.Web)
        for srv in (cls.api, cls.httpd):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.httpd, cls.api):
            srv.shutdown()
            srv.server_close()

    def test_status_reports_mock_provider_and_spend(self):
        with urllib.request.urlopen(self.base + "/api/status") as resp:
            data = json.load(resp)
        self.assertEqual(data["provider"], "mock")
        self.assertIn("cap_usd", data["spend"])

    def post_to(self, path, body, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_task_kind_is_validated(self):
        self.assertEqual(self.post_to("/api/tasks", {"kind": "shell", "goal": "x"})[0], 400)
        self.assertEqual(self.post_to("/api/tasks", {"kind": "check", "goal": ["x"]})[0], 400)
        self.assertEqual(self.post_to("/api/tasks", ["not", "an", "object"])[0], 400)

    def test_unknown_proposal_and_bad_ids(self):
        with mock.patch.dict(os.environ, {"EIG_APPROVER_TOKEN": TOKEN}):
            self.assertEqual(self.post_to("/api/proposals/p000000000000/approve", {}, TOKEN)[0], 404)
            self.assertEqual(self.post_to("/api/proposals/../approve", {}, TOKEN)[0], 404)

    # --- approver authorization (the human-approval gate) ----------------------------------

    def pending(self):
        """A pending proposal on the server's agent, with the ledger in a temp dir."""
        import tempfile
        from eig_agent import server
        from eig_agent.ledger import Ledger
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        patcher = mock.patch.object(server.AGENT, "ledger", Ledger(os.path.join(tmp, "ledger.jsonl")))
        patcher.start()
        self.addCleanup(patcher.stop)
        pid = "p0123456789ab"
        server.AGENT.proposals[pid] = {"id": pid, "status": "PENDING_APPROVAL", "created_at": 0}
        self.addCleanup(server.AGENT.proposals.pop, pid, None)
        return pid, server.AGENT

    def test_approval_fails_closed_without_a_configured_token(self):
        pid, ag = self.pending()
        with mock.patch.dict(os.environ, {"EIG_APPROVER_TOKEN": ""}):
            self.assertEqual(self.post_to(f"/api/proposals/{pid}/reject", {}, "anything-at-all-123")[0], 503)
        with mock.patch.dict(os.environ, {"EIG_APPROVER_TOKEN": "short"}):  # too short counts as unset
            self.assertEqual(self.post_to(f"/api/proposals/{pid}/reject", {}, "short")[0], 503)
        self.assertEqual(ag.proposals[pid]["status"], "PENDING_APPROVAL")

    def test_missing_token_is_401(self):
        pid, ag = self.pending()
        with mock.patch.dict(os.environ, {"EIG_APPROVER_TOKEN": TOKEN}):
            for verb in ("approve", "reject"):
                self.assertEqual(self.post_to(f"/api/proposals/{pid}/{verb}", {})[0], 401)
        self.assertEqual(ag.proposals[pid]["status"], "PENDING_APPROVAL")

    def test_wrong_token_is_403(self):
        pid, ag = self.pending()
        with mock.patch.dict(os.environ, {"EIG_APPROVER_TOKEN": TOKEN}):
            for verb in ("approve", "reject"):
                self.assertEqual(self.post_to(f"/api/proposals/{pid}/{verb}", {}, TOKEN + "x")[0], 403)
        self.assertEqual(ag.proposals[pid]["status"], "PENDING_APPROVAL")

    def test_correct_token_decides_and_ledger_gets_only_a_hash(self):
        pid, ag = self.pending()
        with mock.patch.dict(os.environ, {"EIG_APPROVER_TOKEN": TOKEN}):
            status, body = self.post_to(f"/api/proposals/{pid}/reject", {}, TOKEN)
        self.assertEqual((status, body["status"]), (200, "REJECTED"))
        entry = ag.ledger.entries()[-1]
        self.assertEqual(entry["type"], "PROPOSAL_REJECTED")
        self.assertRegex(entry["data"]["by"], r"^approver:[0-9a-f]{10}$")
        self.assertNotIn(TOKEN, open(ag.ledger.path).read())

    def test_ledger_endpoint_reports_verification(self):
        with urllib.request.urlopen(self.base + "/api/ledger") as resp:
            data = json.load(resp)
        self.assertIn("ok", data["verify"])

    def test_evidence_files_only_by_strict_name(self):
        for path in ("/api/sandbox/j0123456789abcdef/../../etc/passwd", "/api/sandbox/x/results_page.png",
                     "/api/sandbox/j0123456789abcdef/results_page.png"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(self.base + path)
            self.assertEqual(ctx.exception.code, 404)

    def test_web_returns_502_when_orchestrator_is_down(self):
        from http.server import ThreadingHTTPServer
        Dead = type("Dead", (self.Web,), {"upstream": "http://127.0.0.1:9"})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Dead)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/status")
            self.assertEqual(ctx.exception.code, 502)
        finally:
            srv.shutdown()
            srv.server_close()

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
        self.assertIn("judges: use the demo token from the lablab submission / README", html)
        with urllib.request.urlopen(self.base + "/static/app.js") as resp:
            self.assertEqual(resp.headers["Cache-Control"], "no-cache")
        for name in ("app.js", "index.html", "styles.css"):
            with open(os.path.join(demo.PKG, "web", name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("VULTR_INFERENCE_API_KEY", text)


if __name__ == "__main__":
    unittest.main()
