"""Sandbox runner (orchestrator side): one fresh, locked-down container per task.

The orchestrator never touches the Docker socket. It speaks the Docker Engine API over HTTP
to socket-proxy (DOCKER_HOST=tcp://socket-proxy:2375), whose policy only allows this shape
of container. Every run:

  * writes the task to <jobs>/<job>/in/task.json, mounted READ-ONLY at /job/in (volume subpath)
  * gives the sandbox <jobs>/<job>/out as /job/out (its own subpath; nothing else is mounted)
  * runs as uid 10001, read-only rootfs, tmpfs /tmp and /dev/shm, --memory 1g (no swap),
    1 CPU, 256 PIDs, cap_drop ALL, no-new-privileges, empty env, network eig_sandbox_net
  * hard timeout (EIG_SANDBOX_TIMEOUT_S, default 60 s) -> kill -> status TIMEOUT
  * is removed afterwards, always (also on timeout or error), and the removal is confirmed
  * one run at a time (process-wide lock)

Everything the sandbox wrote is untrusted: safe_read() refuses symlinks and non-regular files
and caps sizes, so a hostile page cannot make the orchestrator read its own files.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import stat
import threading
import time
import uuid
from typing import Optional
from urllib.parse import urlsplit

IMAGE = os.environ.get("EIG_SANDBOX_IMAGE", "eig-sandbox:latest")
NETWORK = os.environ.get("EIG_SANDBOX_NETWORK", "eig_sandbox_net")
VOLUME = "eig_sandbox_out"          # the only volume the socket-proxy lets a sandbox mount
JOBS_DIR = os.environ.get("EIG_JOBS_DIR", "/var/eig/jobs")
SANDBOX_UID = 10001
MEMORY = 1024 ** 3
TMPFS = "rw,nosuid,nodev,size=256m"
KEEP_JOBS_S = 2 * 3600
_NAME = re.compile(r"^[a-z0-9_.-]{1,60}$")
_RUN_LOCK = threading.Lock()


def timeout_s() -> float:
    try:
        return float(os.environ.get("EIG_SANDBOX_TIMEOUT_S", "60"))
    except ValueError:
        return 60.0


def new_job_id() -> str:
    return "j" + uuid.uuid4().hex[:16]


def build_spec(job_id: str, image: str = IMAGE, network: str = NETWORK) -> dict:
    """Docker Engine API body for POST /containers/create. Pure function (tested)."""
    if not re.fullmatch(r"j[0-9a-f]{16}", job_id):
        raise ValueError("bad job id")
    mount = lambda sub, target, ro: {"Type": "volume", "Source": VOLUME, "Target": target,  # noqa: E731
                                     "ReadOnly": ro, "VolumeOptions": {"Subpath": f"{job_id}/{sub}"}}
    return {
        "Image": image,
        "User": f"{SANDBOX_UID}:{SANDBOX_UID}",
        "Env": [],                                   # no secrets, no config: everything is in task.json
        "Labels": {"eig.role": "sandbox", "eig.job": job_id},
        "NetworkDisabled": False,
        "StopTimeout": 1,
        "HostConfig": {
            "NetworkMode": network,
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": TMPFS, "/dev/shm": TMPFS},
            "Memory": MEMORY,
            "MemorySwap": MEMORY,                    # = Memory: no swap
            "NanoCpus": 10 ** 9,
            "PidsLimit": 256,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Init": True,
            "RestartPolicy": {"Name": "no"},
            "Mounts": [mount("in", "/job/in", True), mount("out", "/job/out", False)],
        },
    }


class DockerAPI:
    """Just enough of the Engine API, over TCP to socket-proxy."""

    def __init__(self, host: Optional[str] = None):
        url = urlsplit((host or os.environ.get("DOCKER_HOST", "tcp://socket-proxy:2375")).replace("tcp://", "http://"))
        self.host, self.port = url.hostname, url.port or 2375

    def call(self, method: str, path: str, body=None, timeout: float = 30.0) -> tuple:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def create(self, spec: dict, name: str) -> str:
        status, raw = self.call("POST", f"/containers/create?name={name}", spec)
        if status != 201:
            raise RuntimeError(f"create refused ({status}): {raw[:200]!r}")
        return json.loads(raw)["Id"]

    def start(self, cid: str) -> None:
        status, raw = self.call("POST", f"/containers/{cid}/start")
        if status not in (204, 304):
            raise RuntimeError(f"start failed ({status}): {raw[:200]!r}")

    def wait(self, cid: str, timeout: float) -> int:
        """Exit code; raises TimeoutError when the container outlives `timeout` seconds."""
        try:
            status, raw = self.call("POST", f"/containers/{cid}/wait?condition=not-running", timeout=timeout)
        except (socket.timeout, TimeoutError) as exc:
            raise TimeoutError("sandbox timeout") from exc
        if status != 200:
            raise RuntimeError(f"wait failed ({status})")
        return int(json.loads(raw).get("StatusCode", -1))

    def kill(self, cid: str) -> None:
        self.call("POST", f"/containers/{cid}/kill")

    def inspect(self, cid: str) -> Optional[dict]:
        status, raw = self.call("GET", f"/containers/{cid}/json")
        return json.loads(raw) if status == 200 else None

    def logs(self, cid: str) -> str:
        status, raw = self.call("GET", f"/containers/{cid}/logs?stdout=1&stderr=1&tail=50")
        return demux(raw) if status == 200 else ""

    def remove(self, cid: str) -> int:
        return self.call("DELETE", f"/containers/{cid}?force=1&v=1")[0]


def demux(raw: bytes) -> str:
    """Docker log stream without a TTY has an 8-byte header before each chunk."""
    out, i = [], 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(out).decode(errors="replace")[-4000:]


def safe_read(directory: str, name: str, max_bytes: int = 4_000_000) -> Optional[bytes]:
    """Read one sandbox output file: plain name, regular file, no symlink, size-capped. None if absent."""
    if not _NAME.match(name):
        raise ValueError(f"bad file name {name!r}")
    path = os.path.join(directory, name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:  # ELOOP: it is a symlink
        raise ValueError(f"{name}: refused ({exc.strerror})") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{name}: not a regular file")
        if st.st_size > max_bytes:
            raise ValueError(f"{name}: larger than {max_bytes} bytes")
        with os.fdopen(fd, "rb") as fh:
            fd = None
            return fh.read(max_bytes + 1)[:max_bytes]
    finally:
        if fd is not None:
            os.close(fd)


def job_dir(job_id: str, jobs_dir: Optional[str] = None) -> str:
    return os.path.join(jobs_dir or JOBS_DIR, job_id)


def cleanup_old_jobs(jobs_dir: Optional[str] = None, keep_s: float = KEEP_JOBS_S) -> None:
    root = jobs_dir or JOBS_DIR
    if not os.path.isdir(root):
        return
    cutoff = time.time() - keep_s
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if re.fullmatch(r"j[0-9a-f]{16}", name) and os.path.isdir(path) and os.path.getmtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)


def run_job(task: dict, job_id: Optional[str] = None, api: Optional[DockerAPI] = None,
            jobs_dir: Optional[str] = None, timeout: Optional[float] = None, image: str = IMAGE) -> dict:
    """Run one task in a fresh container. Never raises for sandbox failures; returns a record:

    {"job_id", "container_id", "status": OK|ERROR|TIMEOUT|OOM, "exit_code", "lifecycle": [...],
     "duration_ms", "result": <result.json or None>, "out_dir", "logs_tail", "error"}
    """
    api = api or DockerAPI()
    job_id = job_id or new_job_id()
    timeout = timeout or timeout_s()
    root = jobs_dir or JOBS_DIR
    base = job_dir(job_id, root)
    in_dir, out_dir = os.path.join(base, "in"), os.path.join(base, "out")
    record = {"job_id": job_id, "container_id": None, "status": "ERROR", "exit_code": None, "lifecycle": [],
              "duration_ms": 0, "result": None, "out_dir": out_dir, "logs_tail": "", "error": None,
              "limits": {"timeout_s": timeout, "memory": "1g", "cpus": 1, "pids": 256, "network": NETWORK,
                         "read_only": True, "cap_drop": "ALL", "env": "empty"}}
    event = lambda name, **kw: record["lifecycle"].append(  # noqa: E731
        {"t": round(time.time(), 3), "event": name, **kw})

    cleanup_old_jobs(root)
    os.makedirs(in_dir, mode=0o755)
    os.makedirs(out_dir, mode=0o755)
    with open(os.path.join(in_dir, "task.json"), "w", encoding="utf-8") as fh:
        json.dump({**task, "job_id": job_id}, fh, ensure_ascii=False)
    os.chmod(os.path.join(in_dir, "task.json"), 0o444)
    os.chmod(in_dir, 0o555)

    started = time.monotonic()
    with _RUN_LOCK:
        cid = None
        try:
            cid = api.create(build_spec(job_id, image=image), f"eig-sbx-{job_id}")
            record["container_id"] = cid
            event("created", id=cid[:12])
            api.start(cid)
            event("started")
            try:
                code = api.wait(cid, timeout)
                record["exit_code"] = code
                event("exited", code=code)
                info = api.inspect(cid) or {}
                oom = (info.get("State") or {}).get("OOMKilled")
                record["status"] = "OOM" if oom else ("OK" if code == 0 else "ERROR")
            except TimeoutError:
                api.kill(cid)
                event("killed", reason=f"timeout after {timeout:.0f}s")
                record["status"] = "TIMEOUT"
            record["logs_tail"] = api.logs(cid)
        except Exception as exc:  # create refused, proxy down, ...
            record["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            event("error", detail=record["error"][:200])
        finally:
            if cid:
                status = api.remove(cid)
                gone = api.inspect(cid) is None
                event("removed", http=status, confirmed_gone=gone)
            record["duration_ms"] = int((time.monotonic() - started) * 1000)

    try:
        raw = safe_read(out_dir, "result.json", 2_000_000)
        record["result"] = json.loads(raw) if raw else None
    except (ValueError, json.JSONDecodeError) as exc:
        record["error"] = (record["error"] or "") + f" result.json unreadable: {exc}"
    return record
