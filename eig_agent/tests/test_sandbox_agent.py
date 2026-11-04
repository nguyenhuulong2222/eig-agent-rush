"""M3: ledger, sandbox spec and lifecycle, safe output reads, the agent flow, red-team containment."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ["EIG_USAGE_LOG"] = "off"
os.environ["EIG_LLM_PROVIDER"] = "mock"

from eig_agent import agent, sandbox  # noqa: E402
from eig_agent.ledger import GENESIS, Ledger, verify  # noqa: E402
from eig_agent.llm import LLMClient  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import reset as reset_script  # noqa: E402
import tamper as tamper_script  # noqa: E402

CERT_PATH = os.path.join(ROOT, "results", "results.json")
CERT = open(CERT_PATH, "rb").read()
REDTEAM_HTML = open(os.path.join(ROOT, "public", "notices", "candidate-forum.html"), encoding="utf-8").read()


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)


# --- ledger --------------------------------------------------------------------------------

class LedgerTest(TempDirTest):
    def make(self, n=3):
        led = Ledger(os.path.join(self.tmp, "ledger.jsonl"))
        for i in range(n):
            led.append("EVENT", {"i": i})
        return led

    def lines(self, led):
        with open(led.path, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def write(self, led, lines):
        with open(led.path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def test_chain_links_and_verifies(self):
        led = self.make()
        rows = led.entries()
        self.assertEqual(rows[0]["prev_hash"], GENESIS)
        self.assertEqual([r["prev_hash"] for r in rows[1:]], [r["entry_hash"] for r in rows[:-1]])
        self.assertEqual(led.verify(), {"ok": True, "entries": 3, "head": rows[-1]["entry_hash"], "error": None,
                                        "bad_seq": None})

    def test_edited_entry_is_detected(self):
        led = self.make()
        lines = self.lines(led)
        row = json.loads(lines[1])
        row["data"]["i"] = 99
        lines[1] = json.dumps(row)
        self.write(led, lines)
        res = verify(led.path)
        self.assertFalse(res["ok"])
        self.assertEqual(res["bad_seq"], 1)

    def test_rehashed_edit_breaks_the_next_link(self):
        from eig_agent.ledger import entry_hash
        led = self.make()
        lines = self.lines(led)
        row = json.loads(lines[1])
        row["data"]["i"] = 99
        row["entry_hash"] = entry_hash(row)  # attacker recomputes this entry's hash
        lines[1] = json.dumps(row)
        self.write(led, lines)
        self.assertEqual(verify(led.path)["bad_seq"], 2)

    def test_deleted_or_reordered_lines_are_detected(self):
        led = self.make(4)
        lines = self.lines(led)
        self.write(led, lines[:1] + lines[2:])
        self.assertFalse(verify(led.path)["ok"])
        self.write(led, [lines[1], lines[0]] + lines[2:])
        self.assertFalse(verify(led.path)["ok"])

    def test_append_continues_an_existing_chain(self):
        led = self.make(2)
        again = Ledger(led.path)
        entry = again.append("EVENT", {"i": 2})
        self.assertEqual(entry["seq"], 2)
        self.assertTrue(again.verify()["ok"])


# --- sandbox spec ----------------------------------------------------------------------------

class SpecTest(unittest.TestCase):
    JOB = "j0123456789abcdef"

    def test_limits_and_isolation(self):
        spec = sandbox.build_spec(self.JOB)
        hc = spec["HostConfig"]
        self.assertEqual(spec["Env"], [])
        self.assertEqual(spec["User"], "10001:10001")
        self.assertEqual((hc["Memory"], hc["MemorySwap"], hc["NanoCpus"], hc["PidsLimit"]),
                         (1024 ** 3, 1024 ** 3, 10 ** 9, 256))
        self.assertTrue(hc["ReadonlyRootfs"])
        self.assertEqual(hc["CapDrop"], ["ALL"])
        self.assertEqual(hc["SecurityOpt"], ["no-new-privileges"])
        self.assertEqual(hc["NetworkMode"], "eig_sandbox_net")
        self.assertEqual(set(hc["Tmpfs"]), {"/tmp", "/dev/shm"})
        for key in ("Privileged", "CapAdd", "Binds", "Devices", "PidMode", "VolumesFrom"):
            self.assertNotIn(key, hc)

    def test_only_two_subpath_mounts_input_read_only(self):
        mounts = sandbox.build_spec(self.JOB)["HostConfig"]["Mounts"]
        self.assertEqual([(m["Type"], m["Source"], m["Target"], m["ReadOnly"], m["VolumeOptions"]["Subpath"])
                          for m in mounts],
                         [("volume", "eig_sandbox_out", "/job/in", True, f"{self.JOB}/in"),
                          ("volume", "eig_sandbox_out", "/job/out", False, f"{self.JOB}/out")])

    def test_body_passes_the_proxy_text_rules(self):
        body = json.dumps(sandbox.build_spec(self.JOB))
        self.assertNotIn("\\", body)   # proxy refuses any escape
        self.assertNotIn("..", body)   # proxy refuses traversal
        self.assertNotIn("docker.sock", body)
        self.assertLess(len(body), 16384)

    def test_bad_job_id_rejected(self):
        for bad in ("../x", "j123", "J0123456789abcdef", "j0123456789abcdeg"):
            with self.assertRaises(ValueError):
                sandbox.build_spec(bad)


# --- sandbox lifecycle with a fake Docker API -------------------------------------------------

class FakeDocker:
    def __init__(self, outcome="ok", result=None):
        self.outcome, self.result, self.calls, self.exists = outcome, result, [], False

    def create(self, spec, name):
        self.calls.append("create")
        if self.outcome == "refused":
            raise RuntimeError("create refused (403)")
        self.spec, self.exists = spec, True
        return "c" * 64

    def start(self, cid):
        self.calls.append("start")
        job = self.spec["Labels"]["eig.job"]
        self.out = os.path.join(self.jobs_dir, job, "out")
        with open(os.path.join(self.jobs_dir, job, "in", "task.json")) as fh:
            self.task = json.load(fh)
        if self.result is not None:
            with open(os.path.join(self.out, "result.json"), "w") as fh:
                json.dump(self.result, fh)

    def wait(self, cid, timeout):
        self.calls.append("wait")
        if self.outcome == "timeout":
            raise TimeoutError()
        return 0

    def kill(self, cid):
        self.calls.append("kill")

    def inspect(self, cid):
        return {"State": {"OOMKilled": False}} if self.exists else None

    def logs(self, cid):
        return "log"

    def remove(self, cid):
        self.calls.append("remove")
        self.exists = False
        return 204


class LifecycleTest(TempDirTest):
    def run_with(self, docker, task=None, **kw):
        docker.jobs_dir = self.tmp
        return sandbox.run_job(task or {"steps": []}, api=docker, jobs_dir=self.tmp, timeout=5, **kw)

    def test_ok_run_is_removed_and_logged(self):
        d = FakeDocker(result={"steps": [], "done": True})
        rec = self.run_with(d, {"steps": [{"action": "wait", "ms": 1}]})
        self.assertEqual(rec["status"], "OK")
        self.assertEqual(d.calls, ["create", "start", "wait", "remove"])
        self.assertEqual([e["event"] for e in rec["lifecycle"]], ["created", "started", "exited", "removed"])
        self.assertTrue(rec["lifecycle"][-1]["confirmed_gone"])
        self.assertEqual(rec["result"], {"steps": [], "done": True})
        self.assertEqual(d.task["steps"], [{"action": "wait", "ms": 1}])
        task_file = os.path.join(self.tmp, rec["job_id"], "in", "task.json")
        self.assertFalse(os.stat(task_file).st_mode & 0o222)  # input is read-only

    def test_timeout_kills_marks_and_removes(self):
        d = FakeDocker(outcome="timeout")
        rec = self.run_with(d)
        self.assertEqual(rec["status"], "TIMEOUT")
        self.assertEqual(d.calls, ["create", "start", "wait", "kill", "remove"])
        self.assertIn("killed", [e["event"] for e in rec["lifecycle"]])

    def test_refused_create_is_an_error_not_a_crash(self):
        rec = self.run_with(FakeDocker(outcome="refused"))
        self.assertEqual((rec["status"], rec["container_id"]), ("ERROR", None))
        self.assertIn("refused", rec["error"])


class SafeReadTest(TempDirTest):
    def test_plain_file(self):
        with open(os.path.join(self.tmp, "a.txt"), "wb") as fh:
            fh.write(b"hi")
        self.assertEqual(sandbox.safe_read(self.tmp, "a.txt"), b"hi")
        self.assertIsNone(sandbox.safe_read(self.tmp, "missing.txt"))

    def test_symlink_out_of_the_sandbox_is_refused(self):
        secret = os.path.join(self.tmp, "orchestrator-secret")
        with open(secret, "w") as fh:
            fh.write("key")
        os.symlink(secret, os.path.join(self.tmp, "results_json.bin"))
        with self.assertRaises(ValueError):
            sandbox.safe_read(self.tmp, "results_json.bin")

    def test_names_and_sizes(self):
        with self.assertRaises(ValueError):
            sandbox.safe_read(self.tmp, "../etc/passwd")
        with open(os.path.join(self.tmp, "big.bin"), "wb") as fh:
            fh.write(b"x" * 100)
        with self.assertRaises(ValueError):
            sandbox.safe_read(self.tmp, "big.bin", max_bytes=10)
        os.mkdir(os.path.join(self.tmp, "dir.bin"))
        with self.assertRaises(ValueError):
            sandbox.safe_read(self.tmp, "dir.bin")


# --- agent flow with a fake sandbox ------------------------------------------------------------

class FakeSite:
    """Stands in for sandbox.run_job: serves the county files and records each task."""

    def __init__(self, root, county_dir, status="OK"):
        self.root, self.county_dir, self.status, self.tasks, self.n = root, county_dir, status, [], 0

    def body(self, url):
        path = url.split("8080", 1)[-1]
        if path == agent.RESULTS_JSON:
            with open(os.path.join(self.county_dir, "results.json"), "rb") as fh:
                return fh.read()
        if path == agent.REDTEAM_PAGE:
            return REDTEAM_HTML.encode()
        return f"<html><body><p>{path}</p></body></html>".encode()

    def __call__(self, task):
        self.tasks.append(task)
        self.n += 1
        job = "j%016x" % self.n
        out = os.path.join(self.root, job, "out")
        os.makedirs(out)
        steps = []
        for i, s in enumerate(task["steps"]):
            entry = {"i": i, "action": s["action"], "target": s.get("url") or s.get("path"), "ok": True}
            if s["action"] == "fetch":
                open(os.path.join(out, s["name"] + ".bin"), "wb").write(self.body(s["url"]))
            elif s["action"] == "visit":
                html = self.body(s["url"])
                open(os.path.join(out, s["name"] + ".html"), "wb").write(html)
                shown = "Candidate forum: Mayor's race" if s["name"] == "redteam" else s["url"]
                open(os.path.join(out, s["name"] + ".txt"), "w").write(shown)
            elif s["action"] == "dump_env":
                entry["detail"] = {"env": {"HOME": "/tmp", "PATH": "/usr/bin", "HOSTNAME": "abc"}}
            else:  # read_file / http_post / browser_goto all fail in a real sandbox
                entry.update(ok=False, error="URLError: <urlopen error [Errno -2] Name or service not known>"
                             if "http" in s["action"] or "browser" in s["action"]
                             else "FileNotFoundError: [Errno 2] No such file or directory: '/app/.env'")
            steps.append(entry)
        with open(os.path.join(out, "result.json"), "w") as fh:
            json.dump({"steps": steps, "uid": 10001, "done": True}, fh)
        lifecycle = [{"t": 0, "event": e} for e in ("created", "started", "exited")] + \
                    [{"t": 0, "event": "removed", "confirmed_gone": True}]
        return {"job_id": job, "container_id": "c" * 64, "status": self.status, "exit_code": 0,
                "lifecycle": lifecycle, "duration_ms": 10, "out_dir": out, "logs_tail": "", "error": None,
                "result": None if self.status == "TIMEOUT" else {"steps": steps}, "limits": {}}


class AgentTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.county = os.path.join(self.tmp, "county")
        os.makedirs(self.county)
        shutil.copy(CERT_PATH, os.path.join(self.county, "results.json"))
        self.site = FakeSite(os.path.join(self.tmp, "jobs"), self.county)
        self.ledger = Ledger(os.path.join(self.tmp, "ledger.jsonl"))
        self.agent = agent.Agent(client=LLMClient(), ledger=self.ledger, runner=self.site, county_dir=self.county,
                                 certified_path=CERT_PATH)

    def tamper(self, mode="one"):
        tamper_script.main(["--mode", mode, "--dir", self.county])

    def types(self):
        return [e["type"] for e in self.ledger.entries()]

    def test_clean_site_needs_no_model_and_no_proposal(self):
        out = self.agent.check("check results")
        self.assertEqual((out["status"], out["decision"]["action"], out["proposal"]), ("done", "NONE", None))
        self.assertEqual(out["calls"], [])
        task = self.site.tasks[0]
        self.assertEqual({s["name"] for s in task["steps"]},
                         {"results_page", "results_json", "page_en", "page_vi", "page_es", "page_tl"})
        self.assertTrue(all(s["url"].startswith(agent.SITE) for s in task["steps"]))
        self.assertEqual(self.types(), ["SANDBOX_RUN", "CHECK_COMPLETED"])

    def test_tamper_proposal_approve_restore_verify(self):
        self.tamper("one")
        out = self.agent.check("")
        self.assertEqual(out["decision"]["action"], "ROLLBACK")      # deterministic BLOCK rule, even on MOCK
        self.assertIn("RES-003", [f["rule_id"] for f in out["seal"]["findings"]])
        prop = out["proposal"]
        self.assertEqual(prop["status"], "PENDING_APPROVAL")
        self.assertNotEqual(open(os.path.join(self.county, "results.json"), "rb").read(), CERT)  # nothing auto-restored

        res = self.agent.approve(prop["id"])
        self.assertEqual(res["proposal"]["status"], "RESTORED_VERIFIED")
        self.assertTrue(res["verify"]["seal_match"])
        self.assertEqual(open(os.path.join(self.county, "results.json"), "rb").read(), CERT)
        self.assertEqual(self.site.tasks[-1]["steps"], [{"action": "fetch", "url": agent.SITE + agent.RESULTS_JSON,
                                                          "name": "results_json"}])
        self.assertEqual(self.types(), ["SANDBOX_RUN", "CHECK_COMPLETED", "PROPOSAL_CREATED", "PROPOSAL_APPROVED",
                                        "RESTORE_EXECUTED", "SANDBOX_RUN", "RESTORE_VERIFIED"])
        self.assertTrue(self.ledger.verify()["ok"])
        with self.assertRaises(ValueError):
            self.agent.approve(prop["id"])   # a proposal is used once

    def test_swap_needs_the_model_and_mock_escalates(self):
        self.tamper("swap")
        with mock.patch.dict(os.environ, {"EIG_MOCK_VERDICT": "uncertain"}):
            out = self.agent.check("")
        self.assertFalse(out["seal"]["match"])
        self.assertFalse(out["seal"]["blocking"])
        self.assertEqual((out["decision"]["action"], out["proposal"]), ("ESCALATE", None))
        self.assertIn("[MOCK]", out["evidence_note"]["explanation"])
        self.assertEqual(out["evidence_note"]["source"], "DOM text")

    def test_reject_leaves_the_site_alone(self):
        self.tamper("one")
        prop = self.agent.check("")["proposal"]
        self.agent.reject(prop["id"])
        self.assertNotEqual(open(os.path.join(self.county, "results.json"), "rb").read(), CERT)
        self.assertEqual(self.types()[-1], "PROPOSAL_REJECTED")

    def test_timeout_is_reported_and_logged(self):
        self.site.status = "TIMEOUT"
        out = self.agent.check("")
        self.assertEqual(out["status"], "TIMEOUT")
        self.assertEqual(self.types(), ["SANDBOX_RUN", "CHECK_FAILED"])
        self.assertEqual(self.ledger.entries()[0]["data"]["status"], "TIMEOUT")

    def test_multilingual_is_skipped_on_mock(self):
        out = self.agent.check("")
        self.assertIn("mock", out["multilingual"]["skipped"])
        self.assertEqual(out["multilingual"]["pages_read"], ["en", "es", "tl", "vi"])

    def test_redteam_all_contained(self):
        out = self.agent.redteam()
        asks = out["injection"]["asks"]
        self.assertEqual(asks, {"read_files": ["/app/.env"], "print_env": True,
                                "send_to": ["http://evil.example/collect"]})
        self.assertIn("ignore all previous instructions", out["injection"]["hidden_text"])
        drill = self.site.tasks[1]["steps"]
        self.assertEqual([s["action"] for s in drill],
                         ["read_file", "dump_env", "http_post", "browser_goto", "http_post"])
        self.assertTrue(out["summary"]["contained"])
        self.assertEqual({e["verdict"] for e in out["events"]}, {"CONTAINED"})
        types = self.types()
        self.assertEqual(types.count("CONTAINED"), 5)
        self.assertEqual(types[-1], "REDTEAM_COMPLETED")
        self.assertIn("INJECTION_FOUND", types)
        self.assertTrue(self.ledger.verify()["ok"])

    def test_redteam_flags_a_leaked_secret_as_breach(self):
        with mock.patch.dict(os.environ, {"VULTR_INFERENCE_API_KEY": "HOME-is-not-it-but-/tmp-is"}):
            orig = self.site.__call__

            def leaky(task):
                rec = orig(task)
                with open(os.path.join(rec["out_dir"], "result.json")) as fh:
                    res = json.load(fh)
                for s in res["steps"]:
                    if s["action"] == "dump_env":
                        s["detail"]["env"]["X"] = "HOME-is-not-it-but-/tmp-is"
                rec["result"] = res
                return rec
            self.agent.runner = leaky
            out = self.agent.redteam()
        self.assertFalse(out["summary"]["contained"])
        self.assertIn("BREACH", self.types())


class CheckMetadataTest(unittest.TestCase):
    def test_metadata_separates_how_we_read_from_who_changed_it(self):
        from eig_agent import triage
        meta = agent.check_metadata("j0123456789abcdef")
        self.assertNotIn("source", meta)
        self.assertIn("read-only sandbox job j0123456789abcdef", meta["evidence_collected_by"])
        self.assertTrue(meta["change_origin"].startswith("unknown"))
        self.assertIn("never infer who made a change", triage.TRIAGE_SYSTEM)


class PlanTest(unittest.TestCase):
    def test_validate_drops_off_site_and_unknown_actions(self):
        kept, rejected = agent.validate_plan([
            {"action": "visit", "path": "/results.html", "screenshot": True},
            {"action": "visit", "path": "http://evil.example/"},
            {"action": "read_file", "path": "/app/.env"},
            {"action": "visit", "path": "/results.html", "screenshot": True}])
        self.assertEqual(kept, [{"action": "visit", "path": "/results.html", "screenshot": True}])
        self.assertEqual(len(rejected), 2)

    def test_mock_plan_is_the_required_evidence(self):
        p = agent.plan("anything", LLMClient())
        self.assertEqual(p["steps"], agent.required_steps())
        self.assertIn("mock", p["source"])


class TamperScriptTest(TempDirTest):
    def test_one_breaks_totals_swap_keeps_them(self):
        from checker.rules_results import check_results
        doc = json.loads(CERT)
        one = tamper_script.tamper(json.loads(CERT), "one", 10)
        swap = tamper_script.tamper(json.loads(CERT), "swap", 10)
        self.assertEqual(check_results(doc, "x"), [])
        self.assertIn("RES-003", [f.rule_id for f in check_results(one, "x")])
        self.assertEqual([f for f in check_results(swap, "x") if f.severity == "BLOCK"], [])

    def test_reset_restores_certified_bytes(self):
        shutil.copy(CERT_PATH, os.path.join(self.tmp, "results.json"))
        tamper_script.main(["--dir", self.tmp])
        self.assertNotEqual(open(os.path.join(self.tmp, "results.json"), "rb").read(), CERT)
        reset_script.main(["--dir", self.tmp, "--certified", CERT_PATH])
        self.assertEqual(open(os.path.join(self.tmp, "results.json"), "rb").read(), CERT)


if __name__ == "__main__":
    unittest.main()
