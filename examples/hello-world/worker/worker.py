#!/usr/bin/env python3
"""rvbench minimal worker — examples/hello-world/worker.

Single file, Python standard library only. Implements the worker side of
`contracts/api/openapi.json` for push delivery:

    GET    /healthz
    GET    /capabilities      -> WorkerRegistration
    GET    /metrics           -> Prometheus text
    POST   /jobs              <- WorkerSubmission{request}  -> 202 JobRecord
    GET    /jobs/{id}?wait=N  -> JobRecord
    GET    /jobs              -> {"jobs": [JobRecord, ...]}
    DELETE /jobs/{id}         -> JobRecord (canceled)

Two implementations are probed at startup, independently:

    native-c  compile request.source with the configured C compiler,
              ABI: void candidate(const float *a, const float *b, float *out, int n)
    triton    import request.source as a Python module in a subprocess,
              entry: candidate(*inputs) -> tensor

A missing compiler or a missing torch environment is a warning, not a fatal
error. The worker still starts as long as one implementation is ready, and a
job asking for the unavailable one is rejected at admission with the probe
error in the message.

This is an example, not the production worker: inputs use a self-contained
RNG (``xorshift32_v1``), so numbers are not comparable across workers. The
generator identity is published in ``environment.generator``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from array import array
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROTOCOL_VERSION = "1.0.0"
WORKER_VERSION = "0.1.0"
GENERATOR = "xorshift32_v1"

MAX_SOURCE_BYTES = 1 << 20
MAX_ELEMENTS = 1 << 24
MAX_ENVELOPE_BYTES = 8 << 20
QUEUE_LIMIT = 32
MAX_WAIT_SECS = 25

DEFAULT_IMPLEMENTATION = {"k1": "native-c", "k3": "triton"}
IMPLEMENTATIONS = {
    "native-c": {"ops": ["add"], "dtypes": ["float32"]},
    "triton": {"ops": ["add"], "dtypes": ["float32", "float16"]},
}
TOLERANCE = {
    ("native-c", "float32"): (1e-6, 1e-5),
    ("triton", "float32"): (5e-3, 5e-3),
    ("triton", "float16"): (5e-2, 5e-2),
}
KINDS = ("func", "correctness", "performance")
STATES = ("queued", "running", "done", "failed", "timeout", "canceled")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact(root: Path, path: Path) -> dict:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def elements_of(shape: list[int]) -> int:
    total = 1
    for dim in shape:
        total *= dim
    return total


def _xorshift32(value: int) -> int:
    value ^= (value << 13) & 0xFFFFFFFF
    value ^= value >> 17
    value ^= (value << 5) & 0xFFFFFFFF
    return value & 0xFFFFFFFF


def generate_uniform(seed: int, count: int, low: float = -10.0, high: float = 10.0) -> array:
    """Deterministic uniform float32 values in [low, high)."""
    state = _xorshift32((seed & 0xFFFFFFFF) + 1) or 0x9E3779B9
    scale = (high - low) / 4294967296.0
    values = array("f")
    append = values.append
    for _ in range(count):
        state = _xorshift32(state)
        append(low + scale * state)
    return values


def atomic_write(path: Path, text: str) -> None:
    """Write then rename. Windows needs retries: a scanner may hold the new file."""
    tmp = path.with_name("%s.%d.tmp" % (path.name, threading.get_ident()))
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                path.write_text(text, encoding="utf-8")
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return
            time.sleep(0.05)


def read_floats(path: Path) -> array:
    values = array("f")
    values.frombytes(path.read_bytes())
    return values


class WorkerError(Exception):
    """A phase failed. The message becomes the job's error field."""


class Interrupted(Exception):
    """The job was canceled or timed out while a child process was running."""


# --------------------------------------------------------------------------
# embedded artifacts
# --------------------------------------------------------------------------

HARNESS_C = r"""#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void candidate(const float *a, const float *b, float *out, int n);

#ifdef _WIN32
#include <windows.h>
static double now(void) {
    LARGE_INTEGER freq, count;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&count);
    return (double)count.QuadPart / (double)freq.QuadPart;
}
#else
#include <time.h>
static double now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}
#endif

static float *load(const char *path, long n) {
    FILE *file = fopen(path, "rb");
    if (!file) return NULL;
    float *buffer = malloc((size_t)n * sizeof(float));
    if (!buffer) { fclose(file); return NULL; }
    if (fread(buffer, sizeof(float), (size_t)n, file) != (size_t)n) {
        fclose(file); free(buffer); return NULL;
    }
    fclose(file);
    return buffer;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "selfcheck") == 0) {
        float a = 1.5f, b = 2.25f, out = 0.0f;
        candidate(&a, &b, &out, 1);
        if (!(out > 3.74f && out < 3.76f)) {
            fprintf(stderr, "selfcheck mismatch: %f\n", out);
            return 4;
        }
        printf("{\"samples\":[],\"smoke\":%.6f}\n", out);
        return 0;
    }
    if (argc < 5) {
        fprintf(stderr, "usage: candidate n warmup reps mode\n");
        return 2;
    }
    long n = atol(argv[1]);
    int warmup = atoi(argv[2]);
    int reps = atoi(argv[3]);
    const char *mode = argv[4];
    float *a = load("a.bin", n), *b = load("b.bin", n);
    float *out = malloc((size_t)n * sizeof(float));
    if (!a || !b || !out) { fprintf(stderr, "load failed\n"); return 3; }

    candidate(a, b, out, (int)n);
    FILE *file = fopen("out.bin", "wb");
    if (!file) { fprintf(stderr, "open out.bin failed\n"); return 3; }
    fwrite(out, sizeof(float), (size_t)n, file);
    fclose(file);

    printf("{\"samples\":[");
    if (strcmp(mode, "performance") == 0) {
        for (int i = 0; i < warmup; i++) candidate(a, b, out, (int)n);
        for (int i = 0; i < reps; i++) {
            double start = now();
            candidate(a, b, out, (int)n);
            printf("%s%.6f", i ? "," : "", (now() - start) * 1000.0);
        }
    }
    printf("]}\n");
    return 0;
}
"""

TRITON_PROBE = r"""
import json, sys
info = {"torch": None, "triton": None, "flag_gems": None}
try:
    import torch
except Exception as exc:
    info["error"] = "%s: %s" % (type(exc).__name__, exc)
    json.dump(info, sys.stdout)
    raise SystemExit(3)
info["torch"] = torch.__version__
for name in ("triton", "flag_gems"):
    try:
        module = __import__(name)
        info[name] = getattr(module, "__version__", "installed")
    except Exception:
        pass
try:
    ones = torch.ones(4)
    info["smoke"] = float((ones + ones).sum().item())
except Exception as exc:
    info["error"] = "%s: %s" % (type(exc).__name__, exc)
    json.dump(info, sys.stdout)
    raise SystemExit(3)
json.dump(info, sys.stdout)
"""

TRITON_RUNNER = r'''"""Generated per job by worker.py. Runs one candidate.py in isolation."""
import json, os, struct, sys, time

MODE = sys.argv[1]
META = json.load(open("meta.json", encoding="utf-8"))
DTYPE = getattr(__import__("torch"), META["dtype"])


def pack(values):
    out = bytearray()
    for start in range(0, len(values), 65536):
        part = values[start:start + 65536]
        out += struct.pack("<%df" % len(part), *part)
    return bytes(out)


def main():
    import torch

    candidate = __import__("candidate").candidate
    if MODE == "smoke":
        ones = torch.ones(4, dtype=DTYPE)
        out = candidate(ones, ones)
        if isinstance(out, (tuple, list)):
            out = out[0]
        print(json.dumps({"samples": [], "smoke": float(out.reshape(-1)[0])}))
        return

    def load(name):
        raw = open(name, "rb").read()
        tensor = torch.frombuffer(bytearray(raw), dtype=torch.float32)
        return tensor.reshape(META["shape"]).contiguous().to(DTYPE)

    a, b = load("a.bin"), load("b.bin")
    out = candidate(a, b)
    if isinstance(out, (tuple, list)):
        out = out[0]
    out = out.detach().to(torch.float32).cpu().contiguous()
    with open("out.bin", "wb") as handle:
        handle.write(pack(out.reshape(-1).tolist()))

    samples = []
    if MODE == "performance":
        for _ in range(META["warmup"]):
            candidate(a, b)
        for _ in range(META["repetitions"]):
            start = time.perf_counter()
            candidate(a, b)
            samples.append((time.perf_counter() - start) * 1000.0)
    print(json.dumps({"samples": samples}))


main()
'''


# --------------------------------------------------------------------------
# child processes
# --------------------------------------------------------------------------


def run_process(cmd, cwd, timeout, cancel: threading.Event | None = None):
    """Run a command in its own process group. Kill the whole group on timeout or cancel."""
    kwargs = {"cwd": str(cwd), "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.2)
            return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                kill_tree(proc)
                proc.communicate()
                raise Interrupted("canceled by client")
            if time.monotonic() > deadline:
                kill_tree(proc)
                proc.communicate()
                raise TimeoutError("timeout after %ss" % timeout)


def kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        proc.kill()


# --------------------------------------------------------------------------
# startup probes
# --------------------------------------------------------------------------


def probe_native_c(cc: str) -> dict:
    base = {"ops": list(IMPLEMENTATIONS["native-c"]["ops"]), "dtypes": list(IMPLEMENTATIONS["native-c"]["dtypes"])}
    found = None
    for name in dict.fromkeys([cc, "gcc", "clang"]):
        path = shutil.which(name)
        if path:
            found = (name, path)
            break
    if found is None:
        return {
            **base,
            "ready": False,
            "error": "no C compiler found (tried %s): install gcc or clang" % ", ".join(dict.fromkeys([cc, "gcc", "clang"])),
        }
    name, path = found
    try:
        code, out, err = run_process([path, "--version"], Path.cwd(), 15)
        version = (out or err).splitlines()[0].strip() if (out or err) else name
    except Exception as exc:
        return {**base, "ready": False, "error": "cannot run %s: %s" % (name, exc)}
    head, _, tail = version.partition(" ")
    if head.lower().endswith(".exe"):
        version = ("%s %s" % (head[:-4], tail)).strip()
    source = (
        "void candidate(const float*a,const float*b,float*out,int n){for(int i=0;i<n;i++)out[i]=a[i]+b[i];}\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "candidate.c").write_text(source, encoding="utf-8")
        (tmpdir / "harness.c").write_text(HARNESS_C, encoding="utf-8")
        exe = "probe.exe" if os.name == "nt" else "probe"
        try:
            code, out, err = run_process(
                [path, "-O3", "-fno-fast-math", "-o", exe, "candidate.c", "harness.c", "-lm"], tmpdir, 120
            )
        except TimeoutError as exc:
            return {**base, "ready": False, "error": "compiler timeout: %s" % exc}
        if code != 0:
            return {**base, "ready": False, "error": "compile failed: %s" % (err.strip().splitlines() or ["?"])[-1]}
        try:
            code, out, err = run_process([str(tmpdir / exe), "selfcheck"], tmpdir, 30)
        except TimeoutError as exc:
            return {**base, "ready": False, "error": "smoke timeout: %s" % exc}
        if code != 0:
            return {**base, "ready": False, "error": "smoke failed: %s" % (err.strip() or "exit %d" % code)}
    return {**base, "ready": True, "compiler": name, "probe_compilation": "ok: %s" % version}


def probe_triton(python: str) -> dict:
    base = {"ops": list(IMPLEMENTATIONS["triton"]["ops"]), "dtypes": list(IMPLEMENTATIONS["triton"]["dtypes"])}
    try:
        code, out, err = run_process([python, "-c", TRITON_PROBE], Path.cwd(), 120)
    except TimeoutError as exc:
        return {**base, "ready": False, "error": "python probe timeout: %s" % exc}
    except FileNotFoundError:
        return {**base, "ready": False, "error": "python interpreter %r not found" % python}
    try:
        info = json.loads(out.strip().splitlines()[-1])
    except Exception:
        detail = (err.strip().splitlines() or ["no output"])[-1]
        return {**base, "ready": False, "error": "python probe failed: %s" % detail}
    if code != 0 or info.get("error"):
        return {**base, "ready": False, "error": info.get("error") or "python probe exited %d" % code}
    detail = "ok: torch %s" % info.get("torch")
    if info.get("triton"):
        detail += ", triton %s" % info["triton"]
    return {
        **base,
        "ready": True,
        "probe_compilation": detail,
        "torch": info.get("torch"),
        "triton": info.get("triton"),
        "flag_gems": info.get("flag_gems"),
    }


# --------------------------------------------------------------------------
# job
# --------------------------------------------------------------------------


class Job:
    def __init__(self, record: dict, root: Path):
        self.record = record
        self.dir = root / "jobs" / record["job_id"]
        self.enqueued = time.monotonic()
        self.cancel = threading.Event()
        self.running = False

    # -- record field access ------------------------------------------------
    @property
    def job_id(self) -> str:
        return self.record["job_id"]

    @property
    def request(self) -> dict:
        return self.record["request"]

    @property
    def state(self) -> str:
        return self.record["state"]

    @state.setter
    def state(self, value: str) -> None:
        self.record["state"] = value

    @property
    def implementation(self) -> str:
        return self.request.get("implementation") or DEFAULT_IMPLEMENTATION[self.request["backend"]]

    def log(self, line: str) -> None:
        stamp = utcnow()
        self.record["logs"] = (self.record.get("logs") or "") + "%s %s\n" % (stamp, line)

    def finish(self, state: str, error: str | None = None) -> None:
        self.state = state
        self.record["finished_at"] = utcnow()
        if error:
            self.record["error"] = error


# --------------------------------------------------------------------------
# worker
# --------------------------------------------------------------------------


class Worker:
    def __init__(self, config: dict, config_path: Path):
        self.config = config
        self.root = config_path.parent
        self.backend = config.get("backend", {}).get("backend", "k1")
        self.cc = config.get("backend", {}).get("cc", "cc")
        self.python = config.get("backend", {}).get("python", sys.executable)
        self.data_dir = (self.root / config.get("data_dir", "data")).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.worker_id = config["worker_id"]
        self.board_id = config["board_id"]
        self.instance_id = str(uuid.uuid4())
        self.kinds = list(config.get("kinds") or KINDS)
        self.manager = config.get("manager")
        self.impl: dict[str, dict] = {}
        self.jobs: dict[str, Job] = {}
        self.by_client_id: dict[str, tuple[str, str]] = {}
        self.lock = threading.RLock()
        self.queue: queue.Queue[Job] = queue.Queue(maxsize=QUEUE_LIMIT)
        self.counters = {state: 0 for state in STATES}
        self.stop = threading.Event()
        self.manager_online = False
        self._load_history()

    # -- startup ------------------------------------------------------------
    def probe(self) -> None:
        print("[rvbench-worker] probing implementations", file=sys.stderr)
        cc_probe = probe_native_c(self.cc)
        python = self.python
        if shutil.which(python) is None:
            print(
                "[rvbench-worker] WARNING: python %r not found, falling back to %s" % (python, sys.executable),
                file=sys.stderr,
            )
            python = sys.executable
        self.python = python
        triton_probe = probe_triton(python)
        self.impl = {"native-c": cc_probe, "triton": triton_probe}
        if cc_probe.get("compiler"):
            self.cc = cc_probe["compiler"]
        for name, probe in self.impl.items():
            if probe["ready"]:
                print("[rvbench-worker] %-8s ready (%s)" % (name, probe["probe_compilation"]), file=sys.stderr)
            else:
                print("[rvbench-worker] WARNING: %-8s unavailable: %s" % (name, probe["error"]), file=sys.stderr)
        if not any(probe["ready"] for probe in self.impl.values()):
            raise SystemExit(
                "[rvbench-worker] no usable implementation: install gcc/clang, or a python with torch"
            )

    def _load_history(self) -> None:
        jobs_dir = self.data_dir / "jobs"
        if not jobs_dir.is_dir():
            return
        for path in sorted(jobs_dir.glob("*/record.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if record.get("state") in ("queued", "running"):
                record["state"] = "failed"
                record["error"] = "worker restarted before the job finished"
                record["finished_at"] = utcnow()
            job = Job(record, self.data_dir)
            job.running = False
            self.jobs[job.job_id] = job
            self.counters[record["state"]] = self.counters.get(record["state"], 0) + 1

    # -- registration -------------------------------------------------------
    def registration(self) -> dict:
        implementations = {}
        for name, probe in self.impl.items():
            if probe["ready"]:
                implementations[name] = {
                    "ready": True,
                    "ops": probe.get("ops", []),
                    "dtypes": probe.get("dtypes", []),
                    "probe_compilation": probe.get("probe_compilation"),
                }
            else:
                implementations[name] = {"ready": False, "error": probe.get("error")}
        default = DEFAULT_IMPLEMENTATION[self.backend]
        default_probe = self.impl.get(default, {})
        return {
            "worker_id": self.worker_id,
            "board_id": self.board_id,
            "instance_id": self.instance_id,
            "backend": self.backend,
            "kinds": self.kinds,
            "delivery": "push",
            "url": self.config.get("url") or "http://%s" % self.config.get("listen", "127.0.0.1:18801"),
            "capacity": 1,
            "direct_active": 1 if self.running_count() else 0,
            "ready": any(probe["ready"] for probe in self.impl.values()),
            "environment": {
                "generator": GENERATOR,
                "architecture": platform.machine(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "cc": self.cc,
                "torch": self.impl.get("triton", {}).get("torch"),
                "triton": self.impl.get("triton", {}).get("triton"),
                "flag_gems": self.impl.get("triton", {}).get("flag_gems"),
                "ops": default_probe.get("ops", []),
                "dtypes": default_probe.get("dtypes", []),
                "implementations": implementations,
                "worker_version": WORKER_VERSION,
            },
            "protocol_version": PROTOCOL_VERSION,
        }

    def running_count(self) -> int:
        with self.lock:
            return sum(1 for job in self.jobs.values() if job.running)

    # -- admission ----------------------------------------------------------
    def admit(self, request: dict) -> tuple[Job | None, int, str | None]:
        """Return (job, http_status, error). job is None when rejected."""
        for field in ("backend", "kind", "op", "shape", "dtype", "seed", "warmup", "repetitions", "timeout_secs"):
            if field not in request:
                return None, 400, "request.%s is required" % field
        if request["backend"] != self.backend:
            return None, 409, "backend %r does not match this worker (%s)" % (request["backend"], self.backend)
        if request["kind"] not in self.kinds:
            return None, 409, "kind %r is not served here (%s)" % (request["kind"], ", ".join(self.kinds))
        implementation = request.get("implementation") or DEFAULT_IMPLEMENTATION[request["backend"]]
        if implementation not in IMPLEMENTATIONS:
            return None, 409, "unknown implementation %r" % implementation
        probe = self.impl[implementation]
        if not probe["ready"]:
            return None, 409, "implementation %r is unavailable: %s" % (implementation, probe.get("error"))
        if request["op"] not in probe["ops"]:
            return None, 409, "op %r is not supported by %s (%s)" % (
                request["op"],
                implementation,
                ", ".join(probe["ops"]),
            )
        if request["dtype"] not in probe["dtypes"]:
            return None, 409, "dtype %r is not supported by %s (%s)" % (
                request["dtype"],
                implementation,
                ", ".join(probe["dtypes"]),
            )
        shape = request["shape"]
        if not isinstance(shape, list) or not 1 <= len(shape) <= 6:
            return None, 400, "shape must be a list of 1 to 6 dimensions"
        total = elements_of(shape)
        if total > MAX_ELEMENTS:
            return None, 400, "shape has %d elements, limit is %d" % (total, MAX_ELEMENTS)
        source = request.get("source")
        if not isinstance(source, str) or not source.strip():
            return None, 400, "request.source is required: it carries the candidate program"
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            return None, 400, "source exceeds %d bytes" % MAX_SOURCE_BYTES

        client_id = request.get("client_request_id")
        digest = sha256_bytes(json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        if client_id:
            with self.lock:
                seen = self.by_client_id.get(client_id)
                if seen:
                    if seen[1] != digest:
                        return None, 409, "client_request_id %r was already used with a different request" % client_id
                    return self.jobs[seen[0]], 202, None

        record = {
            "job_id": str(uuid.uuid4()),
            "state": "queued",
            "request": request,
            "worker_id": self.worker_id,
            "board_id": self.board_id,
            "instance_id": self.instance_id,
            "submitted_at": utcnow(),
            "logs": "accepted implementation=%s kind=%s op=%s dtype=%s\n"
            % (implementation, request["kind"], request["op"], request["dtype"]),
        }
        job = Job(record, self.data_dir)
        with self.lock:
            self.jobs[job.job_id] = job
            self.counters["queued"] += 1
            if client_id:
                self.by_client_id[client_id] = (job.job_id, digest)
        try:
            self.queue.put_nowait(job)
        except queue.Full:
            with self.lock:
                self.jobs.pop(job.job_id, None)
                self.counters["queued"] -= 1
                if client_id:
                    self.by_client_id.pop(client_id, None)
            return None, 429, "queue is full (%d pending)" % QUEUE_LIMIT
        self.persist(job)
        return job, 202, None

    # -- persistence --------------------------------------------------------
    def persist(self, job: Job) -> None:
        job.dir.mkdir(parents=True, exist_ok=True)
        atomic_write(job.dir / "record.json", json.dumps(job.record, ensure_ascii=False, indent=2))

    # -- execution loop -----------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._loop, name="rvbench-executor", daemon=True).start()
        if self.manager:
            threading.Thread(target=self._heartbeat, name="rvbench-heartbeat", daemon=True).start()

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                job = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.execute(job)
            except Exception as exc:  # never let the executor die
                with self.lock:
                    if job.state not in ("canceled", "timeout"):
                        job.log("internal error: %s" % exc)
                        job.finish("failed", "internal error: %s" % exc)
                        self.persist(job)

    def execute(self, job: Job) -> None:
        with self.lock:
            if job.cancel.is_set():
                job.finish("canceled", "canceled before start")
                self.persist(job)
                return
            job.running = True
            job.record["started_at"] = utcnow()
            queue_wait = time.monotonic() - job.enqueued
            self.counters["queued"] -= 1
            self.counters["running"] += 1
            job.state = "running"
        job.log("started worker=%s board=%s instance=%s" % (self.worker_id, self.board_id, self.instance_id))
        self.persist(job)
        timings = {"queue_wait": round(queue_wait, 6)}
        try:
            compile_start = time.monotonic()
            compilation = self.compile(job)
            timings["compiler"] = round(time.monotonic() - compile_start, 6)
            with self.lock:
                job.record["compilation"] = compilation
            if job.request["kind"] == "func":
                job.log("func: compilation only, no numerical claim")
                self._finish(job, "done", timings)
                return
            eval_start = time.monotonic()
            evaluation = self.evaluate(job)
            timings["evaluator"] = round(time.monotonic() - eval_start, 6)
            with self.lock:
                job.record["evaluation"] = evaluation
            job.log("evaluation status=%s" % evaluation["status"])
            self._finish(job, "done" if evaluation["status"] == "passed" else "failed", timings)
        except Interrupted as exc:
            self._finish(job, "canceled" if job.cancel.is_set() else "timeout", timings, str(exc))
        except TimeoutError as exc:
            self._finish(job, "timeout", timings, str(exc))
        except WorkerError as exc:
            job.log("failed: %s" % (exc,))
            with self.lock:
                if "compilation" not in job.record:
                    job.record["compilation"] = {"status": "failed", "details": {"error": str(exc)}, "artifacts": []}
                elif "evaluation" not in job.record and job.request["kind"] != "func":
                    job.record["evaluation"] = {"status": "failed", "details": {"error": str(exc)}, "artifacts": []}
            self._finish(job, "failed", timings, str(exc))
        except Exception as exc:
            job.log("failed: %s: %s" % (type(exc).__name__, exc))
            self._finish(job, "failed", timings, "%s: %s" % (type(exc).__name__, exc))

    def _finish(self, job: Job, state: str, timings: dict, error: str | None = None) -> None:
        job.running = False
        with self.lock:
            self.counters["running"] -= 1
            self.counters[state] = self.counters.get(state, 0) + 1
            job.record["phase_timings_seconds"] = timings
            job.finish(state, error)
            self.persist(job)

    # -- phases -------------------------------------------------------------
    def compile(self, job: Job) -> dict:
        job.dir.mkdir(parents=True, exist_ok=True)
        (job.dir / "request.json").write_text(
            json.dumps(job.request, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if job.implementation == "native-c":
            return self._compile_native_c(job)
        return self._compile_triton(job)

    def _compile_native_c(self, job: Job) -> dict:
        source = job.request["source"]
        (job.dir / "candidate.c").write_text(source, encoding="utf-8")
        (job.dir / "harness.c").write_text(HARNESS_C, encoding="utf-8")
        exe_name = "candidate.exe" if os.name == "nt" else "candidate"
        code, out, err = self._run(
            job,
            [self.cc, "-O3", "-fno-fast-math", "-o", exe_name, "candidate.c", "harness.c", "-lm"],
            timeout=min(180, job.request["timeout_secs"] * 2),
            what="compile",
        )
        (job.dir / "compile.log").write_text(out + err, encoding="utf-8")
        if code != 0:
            raise WorkerError("compilation failed: %s" % (err.strip().splitlines() or ["exit %d" % code])[-1])
        artifacts = [artifact(self.data_dir, job.dir / "candidate.c"), artifact(self.data_dir, job.dir / "compile.log")]
        details = {"compiler": self.cc, "flags": ["-O3", "-fno-fast-math"], "abi": "void candidate(const float*, const float*, float*, int)"}
        if job.request["kind"] == "func":
            code, out, err = self._run(job, [str(job.dir / exe_name), "selfcheck"], timeout=30, what="smoke")
            if code != 0:
                raise WorkerError("smoke invocation failed: %s" % (err.strip() or "exit %d" % code))
            details["smoke"] = json.loads(out.strip().splitlines()[-1]).get("smoke")
        return {"status": "passed", "details": details, "artifacts": artifacts}

    def _compile_triton(self, job: Job) -> dict:
        (job.dir / "candidate.py").write_text(job.request["source"], encoding="utf-8")
        (job.dir / "runner.py").write_text(TRITON_RUNNER, encoding="utf-8")
        (job.dir / "meta.json").write_text(
            json.dumps(
                {
                    "shape": job.request["shape"],
                    "dtype": job.request["dtype"],
                    "elements": elements_of(job.request["shape"]),
                    "warmup": job.request["warmup"],
                    "repetitions": job.request["repetitions"],
                }
            ),
            encoding="utf-8",
        )
        code, out, err = self._run(
            job, [self.python, "runner.py", "smoke"], timeout=min(180, job.request["timeout_secs"]), what="jit"
        )
        (job.dir / "compile.log").write_text(out + err, encoding="utf-8")
        if code != 0:
            last = (err.strip().splitlines() or ["exit %d" % code])[-1]
            missing = any(token in err for token in ("ModuleNotFoundError", "No module named", "ImportError"))
            raise WorkerError("%s%s" % ("torch/triton environment problem: " if missing else "jit smoke failed: ", last))
        artifacts = [artifact(self.data_dir, job.dir / "candidate.py"), artifact(self.data_dir, job.dir / "compile.log")]
        return {
            "status": "passed",
            "details": {
                "python": self.python,
                "torch": self.impl["triton"].get("torch"),
                "triton": self.impl["triton"].get("triton"),
                "entry": "candidate(*inputs)",
            },
            "artifacts": artifacts,
        }

    def evaluate(self, job: Job) -> dict:
        request = job.request
        total = elements_of(request["shape"])
        stream = generate_uniform(request["seed"], total * 2)
        left, right = stream[:total], stream[total:]
        (job.dir / "a.bin").write_bytes(left.tobytes())
        (job.dir / "b.bin").write_bytes(right.tobytes())
        reference = array("f", [x + y for x, y in zip(left, right)])

        atol, rtol = TOLERANCE[(job.implementation, request["dtype"])]
        timed = request["kind"] == "performance"
        mode = "performance" if timed else "correctness"
        if job.implementation == "native-c":
            exe = "candidate.exe" if os.name == "nt" else "candidate"
            code, out, err = self._run(
                job,
                [str(job.dir / exe), str(total), str(request["warmup"]), str(request["repetitions"]), mode],
                timeout=request["timeout_secs"],
                what="evaluate",
            )
        else:
            code, out, err = self._run(
                job, [self.python, "runner.py", mode], timeout=request["timeout_secs"], what="evaluate"
            )
        (job.dir / "run.log").write_text(out + err, encoding="utf-8")
        if code != 0:
            raise WorkerError("candidate exited %d: %s" % (code, (err.strip().splitlines() or ["?"])[-1]))

        samples: list[float] = []
        for line in reversed(out.strip().splitlines()):
            try:
                samples = json.loads(line).get("samples") or []
                break
            except Exception:
                continue

        out_path = job.dir / "out.bin"
        if not out_path.is_file():
            raise WorkerError("candidate produced no output (out.bin missing)")
        produced = read_floats(out_path)
        if len(produced) != total:
            raise WorkerError("output has %d elements, expected %d" % (len(produced), total))

        max_abs, max_rel, bad, first_bad = 0.0, 0.0, 0, None
        for index, (want, got) in enumerate(zip(reference, produced)):
            diff = abs(want - got)
            if diff > max_abs:
                max_abs = diff
            if diff > atol + rtol * abs(want):
                bad += 1
                if first_bad is None:
                    first_bad = {"index": index, "reference": want, "candidate": got}
            if want:
                relative = diff / abs(want)
                if relative > max_rel:
                    max_rel = relative

        correct = bad == 0
        details = {
            "correct": correct,
            "elements": total,
            "dtype": request["dtype"],
            "generator": GENERATOR,
            "seed": request["seed"],
            "atol": atol,
            "rtol": rtol,
            "max_abs_error": round(max_abs, 9),
            "max_rel_error": round(max_rel, 9),
            "mismatches": bad,
            "first_mismatch": first_bad,
            "outputs": [{"name": "out", "shape": request["shape"], "dtype": "float32"}],
        }
        if timed and correct:
            samples = sorted(samples)
            median = samples[len(samples) // 2] if samples else None
            details.update(
                {
                    "warmup": request["warmup"],
                    "repetitions": request["repetitions"],
                    "samples_ms": samples,
                    "median_ms": round(median, 6) if median is not None else None,
                    "statistic": "median",
                    "compilation_excluded": True,
                }
            )
        artifacts = [
            artifact(self.data_dir, job.dir / "a.bin"),
            artifact(self.data_dir, job.dir / "b.bin"),
            artifact(self.data_dir, job.dir / "out.bin"),
            artifact(self.data_dir, job.dir / "run.log"),
        ]
        return {
            "status": "passed" if correct else "failed",
            "details": details,
            "artifacts": artifacts,
        }

    def _run(self, job: Job, cmd, timeout: float, what: str):
        if job.cancel.is_set():
            raise Interrupted("canceled by client")
        job.log("%s: %s" % (what, " ".join(cmd)))
        return run_process(cmd, job.dir, timeout, cancel=job.cancel)

    # -- manager heartbeat --------------------------------------------------
    def _heartbeat(self) -> None:
        url = self.manager.rstrip("/") + "/cluster/workers/register"
        while not self.stop.wait(2.0):
            body = json.dumps(self.registration(), ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    response.read()
                if not self.manager_online:
                    print("[rvbench-worker] registered with manager %s" % self.manager, file=sys.stderr)
                self.manager_online = True
            except Exception as exc:
                if self.manager_online or not getattr(self, "_warned", False):
                    print("[rvbench-worker] WARNING: manager %s unreachable: %s" % (self.manager, exc), file=sys.stderr)
                    self._warned = True
                self.manager_online = False

    # -- cancel -------------------------------------------------------------
    def cancel(self, job: Job) -> None:
        job.cancel.set()
        with self.lock:
            if job.state == "queued" and not job.running:
                self.counters["queued"] -= 1
                self.counters["canceled"] += 1
                job.finish("canceled", "canceled before start")
                self.persist(job)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "rvbench-worker/" + WORKER_VERSION
    protocol_version = "HTTP/1.1"
    worker: Worker

    def __init__(self, *args, worker: Worker, **kwargs):
        self.worker = worker
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        print("[rvbench-worker] %s %s" % (self.address_string(), fmt % args), file=sys.stderr)

    # -- helpers ------------------------------------------------------------
    def _send(self, status: int, payload, content_type="application/json") -> None:
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        else:
            body = str(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_ENVELOPE_BYTES:
            raise ValueError("envelope exceeds %d bytes" % MAX_ENVELOPE_BYTES)
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _path(self):
        parsed = urlparse(self.path)
        return parsed.path.rstrip("/") or "/", parse_qs(parsed.query)

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        path, query = self._path()
        if path == "/healthz":
            return self._send(200, {"status": "ok", "worker_id": self.worker.worker_id})
        if path == "/capabilities":
            return self._send(200, self.worker.registration())
        if path == "/metrics":
            return self._send(200, self._metrics(), "text/plain; version=0.0.4; charset=utf-8")
        if path == "/jobs":
            with self.worker.lock:
                return self._send(200, {"jobs": [job.record for job in self.worker.jobs.values()]})
        if path.startswith("/jobs/"):
            job = self.worker.jobs.get(path[len("/jobs/") :])
            if job is None:
                return self._error(404, "unknown job")
            wait = query.get("wait", ["0"])[0]
            try:
                deadline = time.monotonic() + min(int(wait), MAX_WAIT_SECS)
            except ValueError:
                return self._error(400, "wait must be an integer")
            while time.monotonic() < deadline and job.state in ("queued", "running"):
                time.sleep(0.05)
            with self.worker.lock:
                return self._send(200, job.record)
        if path.startswith("/attempts/"):
            job = self.worker.jobs.get(path[len("/attempts/") :])
            if job is None:
                return self._error(404, "unknown attempt")
            return self._send(200, job.record)
        return self._error(404, "no route for %s" % path)

    def do_POST(self):
        path, _ = self._path()
        if path != "/jobs":
            return self._error(404, "no route for %s" % path)
        try:
            body = self._body()
        except Exception as exc:
            return self._error(400, "invalid JSON body: %s" % exc)
        request = body.get("request")
        if not isinstance(request, dict):
            return self._error(400, "body must be WorkerSubmission: {\"request\": JobRequest}")
        assignment = body.get("assignment")
        job, status, message = self.worker.admit(request)
        if job is None:
            return self._error(status, message or "rejected")
        if assignment and isinstance(assignment, dict):
            with self.worker.lock:
                for field in ("assignment_id", "attempt_id", "lease_id"):
                    if assignment.get(field):
                        job.record[field] = assignment[field]
                self.worker.persist(job)
        return self._send(202, job.record)

    def do_DELETE(self):
        path, _ = self._path()
        if not path.startswith("/jobs/"):
            return self._error(404, "no route for %s" % path)
        job = self.worker.jobs.get(path[len("/jobs/") :])
        if job is None:
            return self._error(404, "unknown job")
        if job.state in ("queued", "running"):
            self.worker.cancel(job)
        with self.worker.lock:
            return self._send(200, job.record)

    # -- metrics ------------------------------------------------------------
    def _metrics(self) -> str:
        worker = self.worker
        lines = [
            "# HELP rvbench_worker_jobs_total Jobs by final state.",
            "# TYPE rvbench_worker_jobs_total counter",
        ]
        with worker.lock:
            counters = dict(worker.counters)
        for state in STATES:
            lines.append('rvbench_worker_jobs_total{state="%s"} %d' % (state, counters.get(state, 0)))
        lines += [
            "# HELP rvbench_worker_ready Implementation readiness, 1 ready 0 unavailable.",
            "# TYPE rvbench_worker_ready gauge",
        ]
        for name, probe in worker.impl.items():
            lines.append('rvbench_worker_ready{implementation="%s"} %d' % (name, 1 if probe["ready"] else 0))
        lines += [
            "# HELP rvbench_worker_queue_depth Queued jobs.",
            "# TYPE rvbench_worker_queue_depth gauge",
            "rvbench_worker_queue_depth %d" % worker.queue.qsize(),
            "# HELP rvbench_worker_info Build and identity.",
            "# TYPE rvbench_worker_info gauge",
            'rvbench_worker_info{worker_id="%s",board_id="%s",backend="%s",instance_id="%s",version="%s"} 1'
            % (worker.worker_id, worker.board_id, worker.backend, worker.instance_id, WORKER_VERSION),
        ]
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="rvbench minimal worker (hello-world example)")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--listen", help="override listen address, e.g. 0.0.0.0:18801")
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for field in ("listen", "worker_id", "board_id"):
        if field not in config:
            raise SystemExit("config.%s is required" % field)

    worker = Worker(config, config_path)
    worker.probe()
    worker.start()

    listen = args.listen or config["listen"]
    host, _, port = listen.rpartition(":")
    server = ThreadingHTTPServer((host or "0.0.0.0", int(port)), lambda *a, **kw: Handler(*a, worker=worker, **kw))
    server.daemon_threads = True
    print(
        "[rvbench-worker] %s listening on http://%s (backend=%s, kinds=%s)"
        % (worker.worker_id, listen, worker.backend, ",".join(worker.kinds)),
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
