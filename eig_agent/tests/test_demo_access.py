"""M4 back end: judge token, simulated tamper, auto-reset, read-only county mirror."""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

os.environ["EIG_USAGE_LOG"] = "off"
os.environ["EIG_LLM_PROVIDER"] = "mock"

from eig_agent import agent, server  # noqa: E402
from eig_agent.ledger import Ledger  # noqa: E402
from eig_agent.web import WebHandler  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CERT_PATH = os.path.join(ROOT, "results", "results.json")
CERT = open(CERT_PATH, "rb").read()
APPROVER = "approver-token-0123456789"
DEMO = "demo-token-for-judges-0123"


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.county = os.path.join(self.tmp, "county")
        os.makedirs(self.county)
        shutil.copy(CERT_PATH, os.path.join(self.county, "results.json"))
        self.ledger = Ledger(os.path.join(self.tmp, "ledger.jsonl"))
        self.agent = agent.Agent(ledger=self.ledger, runner=None, county_dir=self.county, certified_path=CERT_PATH)

    def types(self):
        return [e["type"] for e in self.ledger.entries()]


class DemoTokenTest(unittest.TestCase):
    def test_demo_token_has_its_own_label(self):
        with mock.patch.dict(os.environ, {"EIG_APPROVER_TOKEN": APPROVER, "EIG_DEMO_TOKEN": DEMO}):
            a = server.authorize("Bearer " + APPROVER)
            d = server.authorize("Bearer " + DEMO)
            self.assertEqual(server.authorize("Bearer " + DEMO + "x")[0], 403)
        self.assertRegex(a[1], r"^approver:[0-9a-f]{10}$")
        self.assertRegex(d[1], r"^demo:[0-9a-f]{10}$")
        self.assertNotEqual(a[1].split(":")[1], d[1].split(":")[1])

    def test_demo_token_alone_enables_approvals(self):
        with mock.patch.dict(os.environ, {"EIG_APPROVER_TOKEN": "", "EIG_DEMO_TOKEN": DEMO}):
            self.assertEqual(server.authorize("Bearer " + DEMO)[0], 200)
            self.assertTrue(server.approvals_enabled())
        with mock.patch.dict(os.environ, {"EIG_APPROVER_TOKEN": "", "EIG_DEMO_TOKEN": ""}):
            self.assertFalse(server.approvals_enabled())

    def test_visitor_label_hides_the_ip(self):
        label = server.visitor_id("203.0.113.7")
        self.assertRegex(label, r"^visitor:[0-9a-f]{10}$")
        self.assertNotIn("203.0.113.7", label)


class SimulateTamperTest(Env):
    def test_tamper_changes_the_live_file_and_is_logged(self):
        out = self.agent.simulate_tamper("one", by="visitor:abc")
        self.assertNotEqual(read(os.path.join(self.county, "results.json")), CERT)
        self.assertEqual(out["before_sha256"], agent._sha(CERT))
        self.assertEqual(self.types(), ["SIMULATED_TAMPER"])
        self.assertEqual(self.ledger.entries()[0]["data"]["by"], "visitor:abc")
        with self.assertRaises(ValueError):
            self.agent.simulate_tamper("delete-everything")


class ResetTest(Env):
    def test_reset_restores_and_expires_pending_proposals(self):
        self.agent.simulate_tamper("swap")
        self.agent.proposals["p1"] = {"id": "p1", "status": "PENDING_APPROVAL", "created_at": 0}
        self.agent.proposals["p2"] = {"id": "p2", "status": "REJECTED", "created_at": 0}
        data = self.agent.reset("auto")
        self.assertTrue(data["restored"])
        self.assertEqual(data["expired_proposals"], ["p1"])
        self.assertEqual(read(os.path.join(self.county, "results.json")), CERT)
        self.assertEqual(self.agent.proposals["p1"]["status"], "EXPIRED")
        self.assertEqual(self.agent.proposals["p2"]["status"], "REJECTED")
        self.assertEqual(self.types(), ["SIMULATED_TAMPER", "RESET"])
        self.assertTrue(self.ledger.verify()["ok"])

    def test_noop_reset_writes_nothing(self):
        self.assertIsNone(self.agent.reset("auto"))
        self.assertEqual(self.types(), [])

    def test_auto_reset_waits_for_running_jobs(self):
        jobs = mock.Mock()
        ar = server.AutoReset(self.agent, jobs, minutes=15)
        due = ar.next_at + 1
        self.assertFalse(ar.tick(now=ar.next_at - 1))
        jobs.running.return_value = 1
        self.assertFalse(ar.tick(now=due))
        self.assertEqual(ar.next_at, due + 60)  # postponed, not skipped
        jobs.running.return_value = 0
        self.agent.simulate_tamper("one")
        self.assertTrue(ar.tick(now=ar.next_at))
        self.assertEqual(read(os.path.join(self.county, "results.json")), CERT)
        self.assertEqual(self.types()[-1], "RESET")

    def test_auto_reset_off(self):
        self.assertIsNone(server.AutoReset(self.agent, mock.Mock(), minutes=0).next_at)


class ApiTest(Env):
    def setUp(self):
        super().setUp()
        patches = [mock.patch.object(server, "AGENT", self.agent),
                   mock.patch.object(server, "TAMPER_LIMITS", server.demo.Limits(daily=10, per_ip=1))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        server.ApiHandler.log_message = lambda *a: None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ApiHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def test_simulate_tamper_is_rate_limited_and_validated(self):
        self.assertEqual(self.post("/api/simulate-tamper", {"mode": "rm -rf"})[0], 400)
        status, body = self.post("/api/simulate-tamper", {"mode": "one"})
        self.assertEqual(status, 200)
        self.assertRegex(body["by"], r"^visitor:[0-9a-f]{10}$")
        status, body = self.post("/api/simulate-tamper", {"mode": "one"})
        self.assertEqual(status, 429)
        self.assertIn("simulated tampers", body["error"])

    def test_status_exposes_demo_controls(self):
        with urllib.request.urlopen(self.base + "/api/status") as resp:
            data = json.load(resp)
        for key in ("approvals_enabled", "auto_reset", "evidence_path"):
            self.assertIn(key, data)


class CountyMirrorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.data = os.path.join(cls.tmp, "data")
        os.makedirs(cls.data)
        with open(os.path.join(cls.data, "results.json"), "wb") as fh:
            fh.write(b'{"live": true}')
        with open(os.path.join(cls.tmp, "outside.txt"), "w") as fh:
            fh.write("secret")
        Mirror = type("Mirror", (WebHandler,), {"county_public": os.path.join(ROOT, "public"), "county_data": cls.data,
                                                 "upstream": "http://127.0.0.1:9", "log_message": lambda *a: None})
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Mirror)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, True)

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method, data=b"x" if method in ("POST", "PUT") else None)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.headers, exc.read()

    def test_pages_from_public_results_from_the_volume(self):
        status, headers, body = self.get("/county/results.html")
        self.assertEqual(status, 200)
        self.assertIn(b"Unofficial results", body)
        self.assertIn("form-action 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(self.get("/county/data/results.json")[2], b'{"live": true}')
        self.assertEqual(self.get("/county/")[0], 200)
        self.assertEqual(self.get("/county")[0], 200)  # 301 followed

    def test_read_only_and_no_escape(self):
        self.assertEqual(self.get("/county/results.html", "POST")[0], 405)
        self.assertEqual(self.get("/county/data/results.json", "PUT")[0], 405)
        self.assertEqual(self.get("/api/status", "DELETE")[0], 405)
        for path in ("/county/../outside.txt", "/county/data/../outside.txt", "/county/%2e%2e/outside.txt",
                     "/county/data/../../../etc/passwd"):
            self.assertEqual(self.get(path)[0], 404, path)


if __name__ == "__main__":
    unittest.main()
