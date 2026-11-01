"""Socket-proxy policy smoke test. Runs INSIDE the orchestrator container:

  docker compose exec -T orchestrator python scripts/smoke_proxy.py

Checks that forbidden Docker API calls are refused (403) and that one sandbox-shaped container
can be created, started, waited on, read and removed through the proxy. The probe container
uses the tag eig-sandbox:smoke (smoke.sh tags python:3.12-slim with it) and reports, from the
inside, its env var names, whether it reaches county-site, and whether it reaches the internet.
Prints one line per check; exit code 1 if any check fails.
"""

import base64
import http.client
import json
import os
import sys
import time
from urllib.parse import urlsplit

HOST = urlsplit(os.environ.get("DOCKER_HOST", "tcp://socket-proxy:2375").replace("tcp://", "http://"))
FAILED = []

PROBE = r"""
import json, os, urllib.request
out = {"env": sorted(os.environ), "uid": os.getuid()}
try:
    out["county"] = urllib.request.urlopen("http://county-site:8080/data/results.json", timeout=5).status
except Exception as e:
    out["county"] = type(e).__name__
try:
    urllib.request.urlopen("https://example.com", timeout=5)
    out["internet"] = "REACHED"
except Exception as e:
    out["internet"] = "blocked: " + type(e).__name__
try:
    open("/etc/eig-write-test", "w")
    out["rootfs"] = "WRITABLE"
except OSError as e:
    out["rootfs"] = "read-only"
print(json.dumps(out))
"""


def call(method, path, body=None):
    conn = http.client.HTTPConnection(HOST.hostname, HOST.port or 2375, timeout=30)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


def sandbox_spec(**host_overrides):
    host = {"NetworkMode": "eig_sandbox_net", "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"}, "Memory": 1024 ** 3, "MemorySwap": 1024 ** 3,
            "NanoCpus": 10 ** 9, "PidsLimit": 256, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"]}
    host.update(host_overrides)
    return {"Image": "eig-sandbox:smoke", "User": "65534:65534", "Env": [],
            # base64 so the create body has no backslashes (the proxy refuses any escape sequence)
            "Cmd": ["python", "-c", "import base64; exec(base64.b64decode('%s'))"
                    % base64.b64encode(PROBE.encode()).decode()], "HostConfig": host}


def demux(raw: bytes) -> str:
    """Docker log stream without a TTY: 8-byte frame headers before each chunk."""
    out, i = [], 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(out).decode(errors="replace")


def main():
    s, _ = call("GET", "/_ping")
    check("allowed: GET /_ping", s == 200, str(s))
    s, _ = call("GET", "/version")
    check("allowed: GET /version", s == 200, str(s))

    denied = [("POST", "/build", None), ("POST", "/images/create?fromImage=alpine", None),
              ("DELETE", "/images/python:3.12-slim", None), ("GET", "/volumes", None),
              ("POST", "/volumes/create", {"Name": "x"}), ("POST", "/networks/create", {"Name": "x"}),
              ("GET", "/info", None), ("POST", "/swarm/init", {}), ("GET", "/secrets", None),
              ("POST", "/containers/some-id/exec", {"Cmd": ["sh"]}), ("POST", "/exec/some-id/start", {}),
              ("PUT", "/containers/some-id/archive?path=/", None), ("POST", "/containers/some-id/commit", None),
              ("GET", "/events", None), ("POST", "/system/prune", None)]
    for method, path, body in denied:
        s, _ = call(method, path, body)
        check(f"denied: {method} {path}", s == 403, str(s))

    bad_creates = {
        "privileged": sandbox_spec(Privileged=True),
        "cap_add": sandbox_spec(CapAdd=["SYS_ADMIN"]),
        "host bind": sandbox_spec(Binds=["/:/host"]),
        "bind mount": sandbox_spec(Mounts=[{"Type": "bind", "Source": "/", "Target": "/host"}]),
        "other volume": sandbox_spec(Mounts=[{"Type": "volume", "Source": "eig_county_data", "Target": "/x"}]),
        "docker.sock": sandbox_spec(Mounts=[{"Type": "volume", "Source": "eig_sandbox_out",
                                             "Target": "/var/run/docker.sock"}]),
        "host network": sandbox_spec(NetworkMode="host"),
        "default bridge (internet)": sandbox_spec(NetworkMode="bridge"),
        "host pid": sandbox_spec(PidMode="host"),
        "seccomp unconfined": sandbox_spec(SecurityOpt=["seccomp=unconfined"]),
        "devices": sandbox_spec(Devices=[{"PathOnHost": "/dev/mem", "PathInContainer": "/dev/mem"}]),
    }
    for name, spec in bad_creates.items():
        s, _ = call("POST", "/containers/create", spec)
        check(f"denied create: {name}", s == 403, str(s))
    other_image = sandbox_spec()
    other_image["Image"] = "alpine:latest"
    s, _ = call("POST", "/containers/create", other_image)
    check("denied create: image other than eig-sandbox", s == 403, str(s))
    lowercase = sandbox_spec()
    lowercase["HostConfig"]["privileged"] = True  # Go matches JSON keys case-insensitively
    s, _ = call("POST", "/containers/create", lowercase)
    check("denied create: lowercase 'privileged'", s == 403, str(s))
    escaped = json.dumps(sandbox_spec()).replace('"Image"', '"\\u0049mage"')
    conn = http.client.HTTPConnection(HOST.hostname, HOST.port or 2375, timeout=30)
    conn.request("POST", "/containers/create", body=escaped.encode(), headers={"Content-Type": "application/json"})
    s = conn.getresponse().status
    conn.close()
    check("denied create: \\u-escaped key", s == 403, str(s))

    # Full lifecycle of one sandbox-shaped container through the proxy.
    s, raw = call("POST", "/containers/create?name=eig-smoke-%d" % int(time.time()), sandbox_spec())
    check("allowed: create sandbox-shaped container", s == 201, f"{s} {raw[:200]!r}")
    if s != 201:
        return
    cid = json.loads(raw)["Id"]
    print(f"      container {cid[:12]} created")
    s, _ = call("POST", f"/containers/{cid}/start")
    check("allowed: start", s == 204, str(s))
    s, raw = call("POST", f"/containers/{cid}/wait")
    check("allowed: wait", s == 200, str(s))
    s, raw = call("GET", f"/containers/{cid}/json")
    check("allowed: inspect", s == 200, str(s))
    hc = json.loads(raw)["HostConfig"] if s == 200 else {}
    check("limits applied: memory 1g, pids 256, cpus 1, read-only, cap_drop ALL",
          hc.get("Memory") == 1024 ** 3 and hc.get("PidsLimit") == 256 and hc.get("NanoCpus") == 10 ** 9
          and hc.get("ReadonlyRootfs") is True and hc.get("CapDrop") == ["ALL"], "")
    s, raw = call("GET", f"/containers/{cid}/logs?stdout=1&stderr=1")
    check("allowed: logs", s == 200, str(s))
    try:
        probe = json.loads(demux(raw).strip().splitlines()[-1])
    except (ValueError, IndexError):
        probe = {}
        check("probe output parsed", False, demux(raw)[:300])
    if probe:
        secretish = [n for n in probe["env"] if any(w in n.upper() for w in ("VULTR", "EIG_", "TOKEN", "SECRET", "API"))]
        check("sandbox env has no secret-like vars", not secretish, f"names: {probe['env']}")
        check("sandbox runs as non-root", probe["uid"] != 0, f"uid {probe['uid']}")
        check("sandbox reaches county-site", probe["county"] == 200, str(probe["county"]))
        check("sandbox cannot reach the internet", str(probe["internet"]).startswith("blocked"), probe["internet"])
        check("sandbox rootfs is read-only", probe["rootfs"] == "read-only", probe["rootfs"])
    s, _ = call("DELETE", f"/containers/{cid}?force=1")
    check("allowed: remove (container destroyed)", s == 204, str(s))
    s, _ = call("GET", f"/containers/{cid}/json")
    check("container is gone", s == 404, str(s))


if __name__ == "__main__":
    main()
    print(f"\n{'ALL PASS' if not FAILED else f'{len(FAILED)} FAILED: ' + ', '.join(FAILED)}")
    sys.exit(1 if FAILED else 0)
