#!/usr/bin/env python3
"""llmt_router.py — one MCP server for a translator, spread over several machines (docs/ROUTER.md).
Version: 2026.11.13.2

    bin/router KEY        (what the Claude app runs; stdio)

The Claude app sees one server (e.g. cfdt) with the translator's own tools plus machines(). Behind it, the router
keeps one connection per machine to the translator's real server (ssh HOST <root>/bin/mcp KEY, or locally), places
runs on the machine where they should finish first, routes follow-up calls by the request's machine, and merges
per-machine views. Files on this computer named in a call (a STEP file in a study) are copied to the machine first
and the call gets their path there ('staged'). Python standard library only: it speaks MCP's stdio protocol (JSON-RPC, one message a line)
on both sides. Nothing but MCP messages goes to stdout; diagnostics go to stderr.
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import importlib.util
import json
import math
import os
import queue
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

__version__ = "2026.11.13.2"

HERE = Path(__file__).resolve().parent


def _load_llmt():
    loader = SourceFileLoader("llmt", str(HERE / "llmt"))
    spec = importlib.util.spec_from_loader("llmt", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


L = _load_llmt()

POOL = 3                 # connections per machine at most
STUCK_AFTER = 20.0       # a connection whose oldest request waits longer than this is not given new ones
STATUS_TTL = 60.0        # older than this: refreshed in the background, the older answer is used meanwhile
STATUS_MAX_AGE = 1800.0  # older than this: not used (asked again, waiting for the answer)
STATUS_WAIT = 12.0       # how long a placement waits for machines it has no answer from yet
BUDGET = 50.0            # every tool call is answered within this: the Claude app gives up after about 60 s
START_TTL = 30.0
NO_START_MIN = 300 * 1440   # SLURM puts a start it cannot plan a year ahead (behind a job with no time limit): no start
NO_START_WHY = "a job ahead of it (or running on the node) has no time limit; it starts when that one ends"
CALL_TIMEOUT = 600.0
STALE_MARK = "STALE_SERVER"    # a translator server refusing a call because a newer release is installed (it exits)
PLACE_ARGS = ("studies", "goals")
PATH_ARGS = ("request", "bench", "study_folder", "path")
ALL_TOOLS = {"jobs", "list_geometry"}
BROADCAST_TOOLS = {"add_geometry"}
READ_ONLY = {"get_file", "get_image"}
PATHS_FILE = L.CONFIG_DIR / "router_paths.json"
MB = 1024 * 1024
UPLOAD_MB = 500.0        # largest local file the router copies to a machine, unless <KEY>_MAX_UPLOAD_MB says otherwise
STAGE_ALL_MB = 25.0      # placement copies local files to every candidate (to validate there) only up to this in total
CHUNK = 1 << 20
BUSY_STATUS_WAIT = 20.0  # study_status / bench_status wait at most this on a machine whose server is busy
INIT_FROM_KEYS = {"cfdt"}  # translators whose studies may start from a run of another machine (solve.init_from)
COPY_MB_S = 50.0         # assumed speed of a copy between two machines through this computer (placement's estimate)
INIT_CHECK_TTL = 60.0


def parse_json_out(text: str):
    """The JSON object a translator's CLI prints (from the first '{')."""
    i = (text or "").find("{")
    if i < 0:
        return None
    try:
        return json.loads(text[i:])
    except json.JSONDecodeError:
        return None


def init_from_refs(study) -> list:
    """[(keys, ref, fields)]: where a study names a run to start from (solve.transient.init_from, else
    solve.init_from), when it names another request ('<request>/<study>' or an absolute study path)."""
    if not isinstance(study, dict):
        return []
    s = study.get("solve") if isinstance(study.get("solve"), dict) else {}
    tr = s.get("transient") if isinstance(s.get("transient"), dict) else {}
    fields = tr.get("init_fields") or s.get("init_fields") or "last"
    for keys, ref in ((("solve", "transient", "init_from"), tr.get("init_from")), (("solve", "init_from"), s.get("init_from"))):
        if isinstance(ref, str) and ref:
            return [(keys, ref, fields)] if "/" in ref.rstrip("/") else []
    return []


def set_in(d: dict, keys: tuple, value) -> None:
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def log(msg: str) -> None:
    print(f"[router] {msg}", file=sys.stderr, flush=True)


def vkey(v) -> tuple:
    """A version for comparison: 2026.11.02.1 == 2026.11.2.1 < 2026.11.10.1."""
    return tuple((0, int(x)) if x.isdigit() else (1, x) for x in re.split(r"[.\-+]", str(v or "")) if x)


def parse_mem_mb(v) -> float | None:
    """A memory size as SLURM reads --mem, in MB: a number with K, M, G or T (M without one; 'GB', 'Gi' too).
    None for unset, 'auto' and what is not a size."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:I?B)?\s*", str(v), re.I)
    if not m:
        return None
    mb = float(m.group(1)) * {"K": 1 / 1024, "": 1, "M": 1, "G": 1024, "T": 1024 * 1024}[m.group(2).upper()]
    return mb if mb > 0 else None


def fmt_mb(mb: float | None) -> str:
    if mb is None:
        return "?"
    return f"{round(mb / 1024, 1):g}G" if mb >= 1024 else f"{mb:.0f}M"


def on_node(job: dict, nodes: set) -> bool:
    """A translator job of llmt's slurm_jobs() is on (running) or asked for (pending) one of these nodes. A running
    job's node is 'nodes' (%N; older llmt: 'reason', which is %R = the node list for a running job); a pending
    job's is the --nodelist it asked for ('req_nodes', %n)."""
    s = (job.get("nodes") or job.get("reason") or "") if job.get("state") == "RUNNING" else (job.get("req_nodes") or "")
    return bool(nodes & {x for x in re.split(r"[,\s]+", str(s).strip()) if x})


def ssh_base(host: dict) -> list:
    """ssh with a shared connection per host (ControlMaster), for the short status and queue calls."""
    ctl = Path.home() / ".ssh" / "llmt-%C"
    return [L.ssh_exe(), *L.SSH_OPTS, "-o", "ControlMaster=auto", "-o", f"ControlPath={ctl}",
            "-o", "ControlPersist=600", "-o", "ConnectTimeout=10", host["ssh"]]


def remote_cmd(host: dict, script: str, timeout: float = 120) -> subprocess.CompletedProcess:
    """Runs a shell script on the host (sh -c locally when ssh is empty). stdin is /dev/null (and ssh -n): the
    router's own stdin is the app's MCP channel; ssh would read it (eating requests) and set it non-blocking, and
    the router's read loop would then see end of input and exit quietly right after initialize."""
    if host.get("ssh"):
        cmd = ssh_base(host)
        cmd = cmd[:-1] + ["-n", cmd[-1], script]
    else:
        cmd = ["sh", "-c", script]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def root_expr(root: str) -> str:
    """The root for a remote shell: ~ expanded there."""
    r = root.rstrip("/")
    return '"$HOME' + r[1:] + '"' if r.startswith("~") else shlex.quote(r)


# ---------------------------------------------------------------- one machine's server

class ChildError(Exception):
    pass


class Child:
    """The translator's MCP server on one machine, over ssh (or local), as a JSON-RPC client."""

    def __init__(self, host: dict, key: str):
        self.host, self.key = host, key
        self.proc = None
        self.lock = threading.Lock()
        self.next_id = 0
        self.waiting: dict = {}
        self.err: list = []
        self.init_result: dict | None = None
        self.inflight: dict = {}
        self.tools: list | None = None
        self.started = 0.0                  # when this server process was started (its release is resolved then)
        self.serving: str | None = None     # the translator version this server process runs ("translator")
        self.retire = False                 # runs another version than the installed one: closed once idle

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, init_params: dict, timeout: float = 120.0) -> None:
        root = self.host["root"].rstrip("/")
        if self.host.get("ssh"):
            cmd = [L.ssh_exe(), *L.SSH_OPTS, "-o", "ConnectTimeout=10", self.host["ssh"], f"{root}/bin/mcp", self.key]
        else:
            cmd = [str(Path(os.path.expanduser(root)) / "bin" / "mcp"), self.key]
        self.err = []
        self.started = time.time()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        params = {"protocolVersion": init_params.get("protocolVersion", "2025-06-18"), "capabilities": {},
                  "clientInfo": {"name": "llmt-router", "version": __version__}}
        r = self.request("initialize", params, timeout=timeout)
        self.init_result = r
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self) -> None:
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                self.err.append("(not MCP on stdout) " + raw.decode(errors="replace").strip()[:200])
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                q = self.waiting.get(msg["id"])
                if q is not None:
                    q.put(msg)
                else:                                   # a late answer to a request we gave up on: free again
                    self.inflight.pop(msg["id"], None)
            elif "id" in msg and "method" in msg:            # a request from the server to us: we offer nothing
                reply = {"jsonrpc": "2.0", "id": msg["id"]}
                reply.update({"result": {}} if msg["method"] == "ping" else
                             {"error": {"code": -32601, "message": "not supported by llmt-router"}})
                self._send(reply)
        for q in list(self.waiting.values()):
            q.put(None)

    def _read_err(self) -> None:
        for raw in self.proc.stderr:
            self.err.append(raw.decode(errors="replace").rstrip())
            del self.err[:-40]

    def _send(self, msg: dict) -> None:
        data = (json.dumps(msg) + "\n").encode()
        with self.lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise ChildError(f"{self.host['name']}: the connection is closed ({exc}) {self.tail()}")

    def tail(self) -> str:
        return ("; " + " | ".join(self.err[-5:])) if self.err else ""

    def request(self, method: str, params: dict, timeout: float = CALL_TIMEOUT) -> dict:
        with self.lock:
            self.next_id += 1
            rid = self.next_id
        q: queue.Queue = queue.Queue()
        self.waiting[rid] = q
        self.inflight[rid] = time.time()
        late = False
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            try:
                msg = q.get(timeout=timeout)
            except queue.Empty:
                late = True                             # the server is still on it: keep counting it as busy
                raise ChildError(f"{self.host['name']}: no answer to {method} within {timeout:.0f} s")
            if msg is None:
                raise ChildError(f"{self.host['name']}: the server ended{self.tail()}")
            if "error" in msg:
                raise ChildError(f"{self.host['name']}: {msg['error'].get('message')}")
            return msg["result"]
        finally:
            self.waiting.pop(rid, None)
            if not late:
                self.inflight.pop(rid, None)
            if self.retire and not self.inflight:   # an old-release server: closed as soon as its calls finish
                threading.Thread(target=self.close, daemon=True).start()

    def stuck_for(self) -> float:
        """Seconds the oldest unanswered request has been waiting (0: none)."""
        return max((time.time() - t for t in list(self.inflight.values())), default=0.0)

    def close(self) -> None:
        if self.alive():
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if self.proc is not None:
            for f in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    f.close()
                except (OSError, ValueError):
                    pass


# ---------------------------------------------------------------- local input files, copied to a machine

class Upload:
    """One local file sent to one machine in the background. Resumable: it is appended to <dir>/.<name>.part from
    the size already there, checked with sha256 on the machine, and only then renamed to <dir>/<name>. Plain ssh
    (the shared ControlMaster connection), so neither rsync nor scp is needed at either end."""

    def __init__(self, host: dict, local: str, remote: str, size: int, sha: str):
        self.host, self.local, self.remote, self.size, self.sha = host, local, remote, size, sha
        self.sent = 0
        self.resumed_at = 0
        self.started = time.time()
        self.done = False
        self.error: str | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def progress(self) -> dict:
        rate = (self.sent - self.resumed_at) / max(0.5, time.time() - self.started)
        left = (self.size - self.sent) / rate if rate > 0 else None
        return {"to": self.remote, "mb": round(self.size / MB, 1), "sent_pct": int(100 * self.sent / max(1, self.size)),
                "eta_s": None if left is None else int(left) + 1}

    def _run(self) -> None:
        q = shlex.quote
        d, name = self.remote.rsplit("/", 1)
        part = f"{d}/.{name}.part"
        try:
            r = remote_cmd(self.host, f"mkdir -p {q(d)} || exit 1; if [ -f {q(part)} ]; then wc -c < {q(part)}; "
                                      f"else echo 0; fi", timeout=60)
            if r.returncode != 0:
                raise OSError(f"cannot create {d}: {(r.stderr or '').strip()[-200:]}")
            try:
                off = int(r.stdout.split()[-1])
            except (ValueError, IndexError):
                off = 0
            if off > self.size:
                off = 0
            self.sent = self.resumed_at = off
            cmd = ssh_base(self.host) + [f"cat {'>>' if off else '>'} {q(part)}"]
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                with open(self.local, "rb") as f:
                    f.seek(off)
                    for b in iter(lambda: f.read(CHUNK), b""):
                        p.stdin.write(b)
                        self.sent += len(b)
                p.stdin.close()
            except BrokenPipeError:
                pass
            err = p.stderr.read().decode(errors="replace")
            if p.wait() != 0:
                raise OSError(f"ssh ended with {p.returncode}: {err.strip()[-200:]}")
            r = remote_cmd(self.host, f"p={q(part)}; s=$( (sha256sum \"$p\" 2>/dev/null || shasum -a 256 \"$p\") "
                                      f"| cut -d' ' -f1); if [ \"$s\" = {self.sha} ]; then mv -f \"$p\" {q(self.remote)} "
                                      f"&& echo @@OK; else rm -f \"$p\"; echo \"@@BAD $s\"; fi", timeout=900)
            if "@@OK" not in r.stdout:
                raise OSError(f"the copy on the machine differs (sha256 {r.stdout.strip()[-70:] or r.stderr.strip()[-100:]}); "
                              f"it was removed: try again")
            self.done = True
            log(f"{self.host['name']}: copied {self.local} to {self.remote} ({self.size / MB:.1f} MB)")
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            self.error = str(exc)[:300]
            log(f"{self.host['name']}: copying {self.local} failed: {self.error}")


def popen_cmd(host: dict, script: str) -> list:
    """A command that runs script on the machine (ssh, or sh here), for a pipe."""
    return ssh_base(host) + [script] if host.get("ssh") else ["sh", "-c", script]


def remote_sha256(host: dict, path: str) -> str:
    r = remote_cmd(host, f"p={shlex.quote(path)}; (sha256sum \"$p\" 2>/dev/null || shasum -a 256 \"$p\") | cut -d' ' -f1",
                   timeout=1800)
    s = r.stdout.strip().split()[-1] if r.stdout.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{64}", s):
        raise OSError(f"{host['name']}: no sha256 of {path} ({(r.stderr or '').strip()[-150:]})")
    return s


class Transfer:
    """A finished run of machine N (init_from of a study placed on M), copied to M in the background: the
    translator packs it on N (`<cli> pack REQUEST --studies A,B --init --out ARCHIVE`), the archive goes N -> this
    computer -> M over the two ssh connections (resumable: appended to <archive>.part from the size already there,
    checked with sha256 against N's), then M imports it (`<cli> import ARCHIVE --name <request>@<N>`). Temporary
    archives are deleted on both machines once imported; after a failure they stay, so the next attempt resumes."""

    def __init__(self, cli: str, src: dict, dst: dict, folder: str, studies: list, out_src: str, out_dst: str,
                 hint_bytes: int | None, tag: str):
        self.cli, self.src, self.dst, self.folder, self.studies = cli, src, dst, folder, list(studies)
        self.req = folder.rstrip("/").rsplit("/", 1)[-1]
        self.name = f"{self.req}@{src['name']}"
        self.tag = tag
        self.tmp_src = f"{out_src}/.llmt-transfer/{self.req}.{tag}.tgz"
        self.tmp_dst = f"{out_dst}/.llmt-transfer/{self.req}.{tag}.tgz"
        self.total = hint_bytes or 0
        self.sent = self.resumed_at = 0
        self.stage = "packing"
        self.started = time.time()
        self.copy_started = None
        self.error: str | None = None
        self.result: dict | None = None        # source study -> its init_from ref on M
        self.request: str | None = None        # the imported request folder on M
        self.done = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def progress(self) -> dict:
        p = {"from": f"{self.src['name']}:{self.folder}", "to": self.dst["name"], "studies": self.studies,
             "stage": self.stage, "mb": round(self.total / MB, 1) if self.total else None}
        if self.stage == "copying" and self.total:
            rate = (self.sent - self.resumed_at) / max(0.5, time.time() - (self.copy_started or time.time()))
            p["sent_pct"] = int(100 * self.sent / self.total)
            p["eta_s"] = int((self.total - self.sent) / rate) + 1 if rate > 0 else None
        return p

    def _run(self) -> None:
        q = shlex.quote
        root_s, root_d = root_expr(self.src["root"]), root_expr(self.dst["root"])
        try:
            # 1. pack on N (once: an archive marked .ok from an earlier attempt is reused)
            r = remote_cmd(self.src, f"a={q(self.tmp_src)}; if [ -f \"$a.ok\" ]; then echo '{{\"bytes\": '$(wc -c < \"$a\")'}}'; "
                                     f"else mkdir -p \"$(dirname \"$a\")\" && {root_s}/bin/run {q(self.cli)} pack "
                                     f"{q(self.folder)} --studies {q(','.join(self.studies))} --init --out \"$a\" && "
                                     f"touch \"$a.ok\"; fi", timeout=6 * 3600)
            p = parse_json_out(r.stdout)
            if not p or p.get("error") or r.returncode != 0:
                raise OSError(f"pack on {self.src['name']}: {(p or {}).get('error') or (r.stderr or r.stdout).strip()[-300:]}")
            self.total = int(p.get("bytes") or 0)
            sha = remote_sha256(self.src, self.tmp_src)
            # 2. N -> here -> M, appended to <archive>.part from what M already has
            self.stage = "copying"
            part = self.tmp_dst + ".part"
            for attempt in range(3):
                r = remote_cmd(self.dst, f"mkdir -p \"$(dirname {q(part)})\" || exit 1; "
                                         f"if [ -f {q(part)} ]; then wc -c < {q(part)}; else echo 0; fi", timeout=60)
                off = int((r.stdout.split() or ["0"])[-1]) if r.returncode == 0 else 0
                if off > self.total:
                    off = 0
                self.sent = self.resumed_at = off
                self.copy_started = time.time()
                if off < self.total:
                    a = subprocess.Popen(popen_cmd(self.src, f"tail -c +{off + 1} {q(self.tmp_src)}"),
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    b = subprocess.Popen(popen_cmd(self.dst, f"cat {'>>' if off else '>'} {q(part)}"),
                                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    try:
                        for chunk in iter(lambda: a.stdout.read(CHUNK), b""):
                            b.stdin.write(chunk)
                            self.sent += len(chunk)
                        b.stdin.close()
                    except BrokenPipeError:
                        pass
                    a.wait()
                    b.wait()
                if self.sent >= self.total:
                    break
                log(f"copy of {self.name} to {self.dst['name']} stopped at {self.sent}/{self.total} bytes: resuming")
            self.stage = "checking"
            if remote_sha256(self.dst, part) != sha:
                remote_cmd(self.dst, f"rm -f {q(part)}", timeout=60)
                raise OSError(f"the copy on {self.dst['name']} differs from {self.src['name']}'s archive (sha256): "
                              f"removed; the next call copies it again")
            remote_cmd(self.dst, f"mv -f {q(part)} {q(self.tmp_dst)}", timeout=60)
            # 3. import on M
            self.stage = "importing"
            r = remote_cmd(self.dst, f"{root_d}/bin/run {q(self.cli)} import {q(self.tmp_dst)} --name {q(self.name)}",
                           timeout=3 * 3600)
            imp = parse_json_out(r.stdout)
            if not imp or imp.get("error") or not isinstance(imp.get("init_from"), dict):
                raise OSError(f"import on {self.dst['name']}: {(imp or {}).get('error') or (r.stderr or r.stdout).strip()[-300:]}")
            self.result = {s: imp["init_from"][s] for s in self.studies if s in imp["init_from"]}
            self.request = imp.get("request")
            for tmp, h in ((self.tmp_src, self.src), (self.tmp_dst, self.dst)):
                remote_cmd(h, f"rm -f {q(tmp)} {q(tmp + '.ok')} {q(tmp + '.part')}", timeout=60)
            self.stage = "done"
            self.done = True
            log(f"init_from: {self.src['name']}:{self.folder} ({', '.join(self.studies)}) is {self.request} on "
                f"{self.dst['name']} ({self.total / MB:.0f} MB)")
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            self.error = str(exc)[:400]
            log(f"init_from copy {self.name} -> {self.dst['name']} failed: {self.error}")


# ---------------------------------------------------------------- the router

class Router:
    def __init__(self, key: str):
        if key not in L.REGISTRY:
            raise SystemExit(f"llmt-router: unknown translator '{key}'")
        self.key = key
        self.t = L.REGISTRY[key]
        cfg = (L.load_setup().get("router") or {})
        self.hosts = [h for h in cfg.get("hosts", []) if h.get("name") and h.get("root")]
        self.prefer = list(cfg.get("prefer", []))
        self.exclude = set(cfg.get("exclude", []))
        self.speed = dict(cfg.get("speed", {}))
        self.small = float(cfg.get("small_minutes", 5))
        self.children: dict = {}
        self.child_lock = threading.Lock()
        self.status_cache: dict = {}
        self.refreshing: set = set()
        self.ref_lock = threading.Lock()
        self.host_locks: dict = {}
        self.wait_defaults: dict = {}
        self.start_cache: dict = {}
        self.init_params: dict = {}
        self.instructions = ""
        self.tools: list = []
        self.tools_version: str | None = None    # the translator version the tool definitions come from
        self.tools_refreshing = False
        self.tools_lock = threading.Lock()
        self.notify = None                        # set by serve(): tells the client the tool list changed
        self.ready = False                        # initialize() has answered
        self.paths = self._load_paths()
        self.paths_lock = threading.Lock()
        self.uploads: dict = {}          # (machine, remote path) -> Upload
        self.up_lock = threading.Lock()
        self.hash_cache: dict = {}       # (path, size, mtime) -> sha256
        self.unstaged: dict = {}         # a staged file's path on a machine -> the local file it came from
        self.transfers: dict = {}        # (M, N, request folder on N, studies) -> Transfer (init_from)
        self.init_checks: dict = {}      # (N, study folder, fields) -> (time, init-check answer)

    # ---- machines
    def host(self, name: str) -> dict | None:
        return next((h for h in self.hosts if h["name"] == name), None)

    def ordered(self) -> list:
        pref = [self.host(n) for n in self.prefer if self.host(n)]
        return [h for h in pref + [h for h in self.hosts if h not in pref] if h["name"] not in self.exclude]

    def child(self, name: str, timeout: float = 120.0) -> Child:
        """A connection to the machine's server that is not stuck. A server busy with a long request (e.g. a
        compile inside a call) answers nothing else meanwhile, so the router opens another connection (up to
        POOL per machine) instead of queueing behind it. Runs are found from any server process (files, queue)."""
        with self.child_lock:
            lock = self.host_locks.setdefault(name, threading.Lock())
        if not lock.acquire(timeout=max(1.0, timeout)):
            raise ChildError(f"{name}: still connecting (no answer to the connection within {timeout:.0f} s)")
        try:
            pool = [c for c in self.children.get(name, []) if c.alive()]
            for c in [c for c in pool if c.retire and not c.inflight]:
                log(f"{name}: closing a server that runs {self.key} {c.serving} (installed: "
                    f"{(self.status_cache.get(name, (0, {}))[1] or {}).get('version')}); the next one runs the installed release")
                pool.remove(c)
                c.close()
            live = [c for c in pool if not c.retire]
            for c in live:
                if c.stuck_for() < STUCK_AFTER:
                    self.children[name] = pool
                    return c
            if len(live) < POOL:
                if live:
                    log(f"{name}: its server has been busy for {live[0].stuck_for():.0f} s: opening another connection")
                c = Child(self.host(name), self.key)
                try:
                    c.start(self.init_params, timeout=max(3.0, timeout))
                except ChildError:
                    c.close()
                    raise
                pool.append(c)
                self.children[name] = pool
                threading.Thread(target=self.probe_serving, args=(name, c), daemon=True).start()
                return c
            self.children[name] = pool
            return min(live or pool, key=lambda c: c.stuck_for())
        finally:
            lock.release()

    def warm(self, name: str) -> None:
        """Connects to a machine in the background, so its first call does not pay for ssh and the server start."""
        def run():
            try:
                self.child(name)
            except ChildError as exc:
                log(f"{name}: {exc}")
        threading.Thread(target=run, daemon=True).start()

    def status(self, name: str, fresh: bool = False) -> dict:
        """What the machine says about itself (llmt status --json KEY, load). A cached answer is used while it is
        younger than STATUS_MAX_AGE; past STATUS_TTL a background refresh starts (a translator's status can take
        seconds: OpenModelica loads its library, OpenFOAM sources its environment)."""
        hit = self.status_cache.get(name)
        if hit and not fresh:
            age = time.time() - hit[0]
            if age > STATUS_TTL:
                self.refresh(name)
            if age < STATUS_MAX_AGE:
                return hit[1]
        return self.fetch_status(name)

    def refresh(self, name: str) -> None:
        with self.ref_lock:
            if name in self.refreshing:
                return
            self.refreshing.add(name)

        def run():
            try:
                self.fetch_status(name)
            finally:
                self.refreshing.discard(name)
        threading.Thread(target=run, daemon=True).start()

    def fetch_status(self, name: str) -> dict:
        h = self.host(name)
        var = f"{self.key.upper()}_OUT"
        dflt = self.default_out() or f"{self.key}_runs"
        script = (f"r={root_expr(h['root'])}; python3 \"$r/llmt\" status --json {self.key} 2>/dev/null; echo; echo @@LOAD; "
                  f"cat /proc/loadavg 2>/dev/null || sysctl -n vm.loadavg 2>/dev/null; "
                  f"nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null; "
                  f"( _llmt_root=\"$r\"; [ -r \"$r/bin/llmt-env.sh\" ] && . \"$r/bin/llmt-env.sh\" >/dev/null 2>&1; "
                  f"echo \"@@OUT ${{{var}:-$HOME/{dflt}}}\" )")
        out = {"name": name, "reachable": False, "_asked": time.time()}
        try:
            r = remote_cmd(h, script, timeout=120)
            text, _, load = r.stdout.partition("@@LOAD")
            load, _, outdir = load.partition("@@OUT")
            if outdir.strip().startswith("/"):
                out["out_dir"] = outdir.strip().splitlines()[0].rstrip("/")
            data = json.loads(text.strip()) if text.strip() else None
            if data is None:
                out["error"] = (r.stderr or "no answer").strip()[-300:]
            else:
                d = data.get("translators", {}).get(self.key, {})
                st = d.get("status") if isinstance(d.get("status"), dict) else {}
                miss = [n for n, v in (d.get("backends") or {}).items() if not v.get("found")]
                ex = (st or {}).get("executor") or {}
                sl = data.get("slurm") or {}
                jobs = [j for j in sl.get("jobs", []) if j.get("key") == self.key]
                rel = data.get("release") or {}
                out.update(llmt_commit=data.get("commit") or (rel.get("umbrella") or "")[:7] or None,
                           translator_commit=((rel.get("commits") or {}).get(self.t["repo"]) or "")[:7] or None,
                           # llmt-runtime: the version the translator reports, and the release's commit of it
                           runtime=(((d.get("status") or {}).get("executor") or {}).get("runtime")
                                    if isinstance(d.get("status"), dict) else None),
                           runtime_commit=((rel.get("runtime") or {}).get("commit") or "")[:7] or None)
                if d.get("reinstall"):         # the install there lacks dependencies of its code (llmt status says)
                    out["needs_reinstall"] = f"{d.get('problem')}: run {d.get('fix')} there"
                out.update(reachable=True, mode=data.get("mode"), release=rel.get("release"),
                           version=d.get("version"), backends_missing=miss, executor=ex,
                           slurm={k: sl.get(k) for k in ("cpus", "cpus_allocated", "time_limit", "state", "nodes_usable",
                                                         "node", "cluster_nodes", "nodes", "shared_home",
                                                         "cluster_cpus", "cluster_cpus_allocated")}
                           if sl else None,
                           queue=self.count_jobs(jobs) if sl else None)
                # a node of a cluster of several (the sparks): its own jobs; the whole cluster's apart
                node = self.nodelist({"executor": ex, "slurm": sl}) if sl else None
                if node and (sl.get("cluster_nodes") or 1) > 1:
                    nodes = {x for x in node.split(",") if x}
                    out["queue"] = self.count_jobs([j for j in jobs if on_node(j, nodes)])
                    out["cluster_queue"] = self.count_jobs(jobs)
                    out["pending_for_node"] = out["queue"]["waiting"]
            nums = [x for x in re.split(r"[\s{}]+", load) if x]
            try:
                out["load"] = float(nums[0])
                out["cores"] = int(nums[-1])
            except (ValueError, IndexError):
                pass
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            out["error"] = str(exc)[:300]
        self.status_cache[name] = (time.time(), out)
        for c in list(self.children.get(name, [])):
            self.check_serving(name, c)
        if out.get("version"):
            self.maybe_refresh_tools(name, out["version"])
        return out

    @staticmethod
    def count_jobs(jobs: list) -> dict:
        return {"running": sum(1 for j in jobs if j.get("state") == "RUNNING"),
                "waiting": sum(1 for j in jobs if j.get("state") != "RUNNING")}

    # ---- the tool definitions follow the newest version (a deploy adds arguments; clients drop unknown ones)
    def set_tools(self, tools: list) -> None:
        self.tools = tools
        self.wait_defaults = {}
        for t in tools:
            props = ((t.get("inputSchema") or {}).get("properties") or {})
            if "wait_s" in props:
                self.wait_defaults[t["name"]] = props["wait_s"].get("default", 45)

    def maybe_refresh_tools(self, name: str, version: str | None) -> None:
        """A machine runs a newer version than the one the tool definitions come from: read them again there, in
        the background."""
        if not self.ready or not version or \
                (self.tools and self.tools_version and vkey(version) <= vkey(self.tools_version)):
            return
        with self.tools_lock:
            if self.tools_refreshing:
                return
            self.tools_refreshing = True
        threading.Thread(target=self.refresh_tools, args=(name, version), daemon=True).start()

    def refresh_tools(self, name: str, version: str) -> bool:
        """tools/list from a new server process on the machine (it runs the installed release, not an older one
        kept by a connection); when the definitions change, the client is told (notifications/tools/list_changed).
        -> whether they changed."""
        changed = False
        c = Child(self.host(name), self.key)
        try:
            c.start(self.init_params, timeout=90.0)
            tools = c.request("tools/list", {}, timeout=60.0).get("tools", [])
            if tools:
                changed = tools != self.tools
                if changed:
                    self.set_tools(tools)
                self.tools_version = version
                log(f"{name}: tool definitions of {self.key} {version}" + (" (changed: the client is told)" if changed else ""))
        except ChildError as exc:
            log(f"{name}: could not read the tool definitions of {self.key} {version}: {exc}")
        finally:
            c.close()
            self.tools_refreshing = False
        if changed and self.notify:
            try:
                self.notify()
            except Exception as exc:  # noqa: BLE001
                log(f"tools/list_changed not sent: {exc}")
        return changed

    # ---- the version a server process runs (docs/ROUTER.md, "Servers that run an old release")
    def probe_serving(self, name: str, c: Child, timeout: float = 20.0) -> str | None:
        """Asks this server process which translator version it runs: "translator" in its jobs() answer (or its
        study_format status). Then compares it with the installed version."""
        if c.serving is None:
            tools = {t.get("name") for t in self.tools}
            for tool, args in (("jobs", {"limit": 1}), ("study_format", {}), ("goal_format", {})):
                if tools and tool not in tools:
                    continue
                try:
                    r = c.request("tools/call", {"name": tool, "arguments": args}, timeout=timeout)
                except ChildError:
                    continue
                self.note_serving(c, self.parse(r))
                if c.serving:
                    break
        self.check_serving(name, c)
        self.maybe_refresh_tools(name, c.serving)
        return c.serving

    @staticmethod
    def note_serving(c: Child, data) -> None:
        if isinstance(data, dict):
            v = data.get("translator")
            if not isinstance(v, str):
                v = (data.get("status") or {}).get("translator") if isinstance(data.get("status"), dict) else None
            if isinstance(v, str) and v:
                c.serving = v

    def check_serving(self, name: str, c: Child) -> None:
        """A server process that runs another version than the one installed (it started before a deploy, and
        is older than the stale-server rule, so it does not exit by itself) is retired: closed now if idle, else
        as soon as its calls finish; the next call starts a server on the installed release. The installed
        version must come from a status asked after this server started, else the status is asked again first."""
        hit = self.status_cache.get(name)
        if not c.serving or not hit or not hit[1].get("version"):
            return
        st = hit[1]
        if c.serving == st["version"]:
            c.retire = False
            return
        if st.get("_asked", 0) < c.started:
            self.refresh(name)
            return
        if not c.retire:
            log(f"{name}: the server in use runs {self.key} {c.serving}, the installed one is {st['version']} "
                f"(release {st.get('release') or '-'}): replacing it " + ("now" if not c.inflight else "once idle"))
        c.retire = True
        with self.child_lock:
            pool = self.children.get(name, [])
            if c in pool and c.inflight:
                return                                # closed by its last call (Child.request)
        if not c.inflight:
            with self.child_lock:
                pool = self.children.get(name, [])
                if c in pool:
                    pool.remove(c)
            c.close()

    def forget(self, name: str) -> None:
        """After sending a machine work: its queue estimate is asked again at the next placement, and its status
        is refreshed in the background."""
        hit = self.status_cache.get(name)
        if hit:
            self.status_cache[name] = (0.0, hit[1])
        for k in [k for k in self.start_cache if k[0] == name]:
            self.start_cache.pop(k, None)
        self.refresh(name)

    def statuses(self, names: list, fresh: bool = False, wait: float = STATUS_WAIT) -> dict:
        """Each machine's status, waiting at most `wait` seconds: a machine without an answer by then is reported
        as still being checked (its earlier answer is used if there is one)."""
        res: dict = {}
        ths = {n: threading.Thread(target=lambda n=n: res.__setitem__(n, self.status(n, fresh)), daemon=True)
               for n in names}
        for t in ths.values():
            t.start()
        end = time.time() + max(wait, 0.5)
        for t in ths.values():
            t.join(max(0.0, end - time.time()))
        for n in names:
            if n not in res:
                hit = self.status_cache.get(n)
                res[n] = hit[1] if hit else {"name": n, "reachable": False,
                                             "error": f"no answer within {wait:.0f} s (still being checked)"}
        return res

    def candidates(self, sts: dict) -> list:
        return [n for n in (h["name"] for h in self.ordered())
                if sts.get(n, {}).get("reachable") and sts[n].get("version") and not sts[n].get("backends_missing")]

    @staticmethod
    def nodelist(st: dict) -> str | None:
        """The node(s) this machine's jobs are kept on, for the start estimate (docs/EXECUTION.md, P_SLURM_NODE):
        what the translator reports, else a --nodelist in its SLURM arguments, else (a cluster of several nodes
        without a shared home) the machine's own node. None: SLURM may choose any node."""
        ex = st.get("executor") or {}
        sl = st.get("slurm") or {}
        node = ex.get("node")
        if isinstance(node, dict):
            if node.get("setting") == "any" or (node.get("nodelist") is None and "setting" in node):
                return node.get("nodelist")
            if node.get("nodelist"):
                return node["nodelist"]
        elif isinstance(node, str) and node and node not in ("local", "any"):
            return node
        m = re.search(r"(?:--nodelist[= ]|-w\s*)(\S+)", str(ex.get("args") or ex.get("slurm_args") or ""))
        if m:
            return m.group(1)
        if (sl.get("cluster_nodes") or 1) > 1 and sl.get("node") and not sl.get("shared_home"):
            return sl["node"]
        return None

    def expected_start(self, name: str, st: dict, procs: int, minutes: float, mem_mb: float | None) -> tuple:
        """Minutes until a job of this size would start on the machine, and why."""
        ex = st.get("executor") or {}
        if ex.get("kind") == "slurm" and st.get("slurm") and st["slurm"].get("nodes_usable") == 0:
            return None, f"its SLURM node is {st['slurm'].get('state')}"
        if ex.get("kind") == "slurm" and st.get("slurm"):
            kind = (self.t.get("executor") or {}).get("kind", "mpi")
            size = f"-n {procs}" if kind == "mpi" else f"-n 1 -c {procs}"
            mins = max(1, int(math.ceil(minutes or ex.get("time_limit_min") or 60)))
            mem = f" --mem={int(mem_mb)}M" if mem_mb else ""
            nl = self.nodelist(st)
            mem += f" --nodelist={nl}" if nl else ""
            ck = (name, procs, mins, mem)
            hit = self.start_cache.get(ck)
            if hit and time.time() - hit[0] < START_TTL:
                return hit[1]
            script = (f'd=$(sbatch --test-only {size} -t {mins}{mem} --wrap=true 2>&1); echo "$d"; '
                      f's=$(echo "$d" | sed -n "s/.*to start at \\([^ ]*\\).*/\\1/p" | head -1); '
                      f'[ -n "$s" ] && echo "@@START $(date -d "$s" +%s 2>/dev/null) $(date +%s)"')
            if nl:
                # a node of a cluster: SLURM's estimate counts every running job's whole time limit, those on the
                # other nodes included. All CPUs of the node idle and nothing pending for it: it starts now.
                nodes = [x for x in nl.split(",") if x]
                checks = " ".join(f'$(sinfo -h -n {shlex.quote(n)} -o %C 2>/dev/null | head -n 1)' for n in nodes)
                pend = "|".join(re.escape(n) for n in nodes)
                script = (f'c="{checks}"; p=$(squeue -h -t PD -o %n 2>/dev/null | grep -cE "(^|,)({pend})(,|$)"); '
                          f'echo "@@IDLE $p $c"; ok=1; [ "$p" = 0 ] || ok=0; [ -n "$c" ] || ok=0; '
                          f'for x in $c; do a=${{x%%/*}}; t=${{x##*/}}; y=${{x#*/}}; i=${{y%%/*}}; '
                          f'{{ [ "$a" = 0 ] && [ "$i" = "$t" ]; }} || ok=0; done; '
                          f'if [ "$ok" = 0 ]; then {script}; fi')
            try:
                r = remote_cmd(self.host(name), script, timeout=60)
                m = re.search(r"@@START (\d+) (\d+)", r.stdout)
                mi = re.search(r"@@IDLE (\d+)((?: \d+/\d+/\d+/\d+)+)", r.stdout)
                idle_all = bool(mi) and mi.group(1) == "0" and all(
                    a == "0" and i == t for a, i, _, t in (x.split("/") for x in mi.group(2).split()))
                if idle_all:
                    tot = sum(int(x.split("/")[3]) for x in mi.group(2).split())
                    res = (0.0, f"starts now (all {tot} CPUs idle on node {nl}, nothing pending for it)")
                elif m and (int(m.group(1)) - int(m.group(2))) / 60.0 >= NO_START_MIN:
                    res = (None, NO_START_WHY)                  # SLURM's one-year placeholder: no start time
                elif m:
                    wait = max(0.0, (int(m.group(1)) - int(m.group(2))) / 60.0)
                    sl = st["slurm"]
                    free = (sl.get("cpus") or 0) - (sl.get("cpus_allocated") or 0)
                    why = ("starts now" if wait < 0.5 else f"SLURM expects a start in {wait:.0f} min") + \
                          f" ({free} of {sl.get('cpus')} CPUs free" + (f" on node {nl}" if nl else "") + ")"
                    res = (wait, why)
                else:
                    res = (None, "sbatch --test-only gave no start time: " + (r.stdout + r.stderr).strip()[-160:])
            except (OSError, subprocess.TimeoutExpired) as exc:
                res = (None, f"sbatch --test-only failed: {exc}")
            self.start_cache[ck] = (time.time(), res)
            return res                       # a SLURM machine without a start time is not judged by its load
        load, cores = st.get("load"), st.get("cores")
        if load is None or not cores:
            return 0.0, "load unknown"
        busy = load / cores
        wait = max(0.0, (busy - 0.7) * 30.0)
        return wait, f"load {load:.1f} on {cores} cores" + ("" if wait == 0 else f" (busy: ~{wait:.0f} min)")

    # ---- the request-folder map
    def _load_paths(self) -> dict:
        try:
            return json.loads(PATHS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def remember(self, path: str, name: str) -> None:
        if not isinstance(path, str) or not path.startswith("/"):
            return
        with self.paths_lock:
            if self.paths.get(path) == name:
                return
            self.paths[path] = name
            if len(self.paths) > 3000:
                for k in list(self.paths)[:-2000]:
                    self.paths.pop(k, None)
            try:
                PATHS_FILE.parent.mkdir(parents=True, exist_ok=True)
                PATHS_FILE.write_text(json.dumps(self.paths))
            except OSError:
                pass

    def learn(self, obj, name: str) -> None:
        """Records the request folders an answer mentions (request, bench, folder fields), recursively."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("request", "bench", "folder", "study_folder") and isinstance(v, str):
                    self.remember(v.rstrip("/"), name)
                else:
                    self.learn(v, name)
        elif isinstance(obj, list):
            for v in obj:
                self.learn(v, name)

    def owner(self, path: str) -> str | None:
        best, who = "", None
        for p, n in self.paths.items():
            if (path == p or path.startswith(p + "/")) and len(p) > len(best):
                best, who = p, n
        return who

    def affinity(self, obj) -> str | None:
        """A machine that holds a request folder mentioned anywhere in the studies."""
        if isinstance(obj, str) and obj.startswith("/"):
            return self.owner(obj)
        if isinstance(obj, dict):
            obj = list(obj.values())
        if isinstance(obj, list):
            for v in obj:
                n = self.affinity(v)
                if n:
                    return n
        return None

    # ---- local input files (docs/ROUTER.md, "Files on this computer")
    def upload_limit_mb(self) -> tuple:
        """-> (limit in MB, variable): <KEY>_MAX_UPLOAD_MB from the router's environment or this install's settings."""
        var = f"{self.key.upper()}_MAX_UPLOAD_MB"
        v = os.environ.get(var)
        if v is None:
            try:
                v = L.env_effective().get(var)
            except Exception:  # noqa: BLE001
                v = None
        try:
            return (float(v) if v not in (None, "") else UPLOAD_MB), var
        except ValueError:
            log(f"{var}={v!r} is not a number: using {UPLOAD_MB:g}")
            return UPLOAD_MB, var

    def local_files(self, args) -> dict:
        """{string as written in the arguments: real local path} for every string naming a regular file under the home
        folder of this computer, at any depth (a study's geometry.step, a list of objects). The top-level request /
        file arguments (PATH_ARGS) name results on a machine and are left alone. A path given earlier as a staged
        copy counts as the local file it came from, so it can go to another machine."""
        home = str(Path.home()).rstrip("/") + "/"
        found: dict = {}

        def walk(o, top=False):
            if isinstance(o, dict):
                for k, v in o.items():
                    if not (top and k in PATH_ARGS):
                        walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
            elif isinstance(o, str) and 1 < len(o) < 4096 and "\n" not in o:
                s = self.unstaged.get(o, o)
                if s.startswith("~/"):
                    s = os.path.expanduser(s)
                if not s.startswith(home):
                    return
                try:
                    if os.path.isfile(s):
                        found[o] = os.path.realpath(s)
                except (OSError, ValueError):
                    pass
        walk(args, True)
        return found

    def check_sizes(self, files: dict) -> int:
        """Total bytes of the local files; a file over the limit is refused with what to do."""
        lim, var = self.upload_limit_mb()
        total = 0
        for lp in set(files.values()):
            size = os.path.getsize(lp)
            total += size
            if size > lim * MB:
                raise ValueError(f"{lp} is {size / MB:.0f} MB, more than the router copies to a machine ({var}={lim:g} MB): "
                                 f"copy it there yourself and give its path on that machine (with run.host), or raise {var}")
        return total

    def default_out(self) -> str | None:
        """The translator's own default output folder, relative to the home folder ('cfdt_runs'), from the registry's
        description of <KEY>_OUT ("output folder (default ~/cfdt_runs)"): the code's default, which a machine uses
        when its settings files don't set <KEY>_OUT."""
        desc = str((self.t.get("env") or {}).get(f"{self.key.upper()}_OUT") or "")
        m = re.search(r"default\s+~/([A-Za-z0-9._/-]+?)[)\s,;]", desc + " ")
        return m.group(1).rstrip("/") if m and ".." not in m.group(1) else None

    def file_hash(self, path: str) -> tuple:
        st = os.stat(path)
        k = (path, st.st_size, st.st_mtime_ns)
        if k not in self.hash_cache:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for b in iter(lambda: f.read(CHUNK), b""):
                    h.update(b)
            self.hash_cache[k] = h.hexdigest()
        return st.st_size, self.hash_cache[k]

    @staticmethod
    def rewrite(obj, mapping: dict, top: bool = True):
        if isinstance(obj, dict):
            return {k: (v if top and k in PATH_ARGS else Router.rewrite(v, mapping, False)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [Router.rewrite(v, mapping, False) for v in obj]
        if isinstance(obj, str):
            return mapping.get(obj, obj)
        return obj

    def uploading_to(self, files: dict) -> list:
        """Machines that hold, or are receiving, every one of these local files."""
        lps = set(files.values())
        with self.up_lock:
            have: dict = {}
            for (m, _), u in self.uploads.items():
                if not u.error:
                    have.setdefault(m, set()).add(u.local)
        return [m for m, s in have.items() if lps <= s]

    def stage(self, name: str, tool: str, args: dict, deadline: float | None = None, margin: float = 5.0) -> tuple:
        """Copies the local files named in args to the machine (unless the same path exists there with the same size:
        a shared folder) to <KEY>_OUT/uploads/<sha256[:12]>/<name> there; without <KEY>_OUT in its settings, the
        translator's default output folder (~/cfdt_runs/uploads/...), or ~/.llmt/uploads/... when it has none.
        A copy already there (same hash) is not sent again. Waits until deadline - margin.
        -> (args with the paths on the machine, staged {local: remote}, uploading {local: progress} or None)."""
        h = self.host(name)
        if tool in READ_ONLY or not h or not h.get("ssh"):
            return args, {}, None
        files = self.local_files(args)
        if not files:
            return args, {}, None
        self.check_sizes(files)
        info: dict = {}
        for lp in sorted(set(files.values())):
            size, sha = self.file_hash(lp)
            info[lp] = (size, sha, re.sub(r"[^A-Za-z0-9._+-]", "_", os.path.basename(lp)).lstrip(".") or "file")
        lps = sorted(info)
        q = shlex.quote
        var = f"{self.key.upper()}_OUT"
        dflt = self.default_out()
        fallback = f'"$HOME"/{q(dflt + "/uploads")}' if dflt else '"$HOME/.llmt/uploads"'
        lines = [f"_llmt_root={root_expr(h['root'])}",
                 '[ -r "$_llmt_root/bin/llmt-env.sh" ] && . "$_llmt_root/bin/llmt-env.sh" >/dev/null 2>&1',
                 f'if [ -n "${{{var}:-}}" ]; then b="${var}/uploads"; else b={fallback}; fi',
                 'echo "@@BASE $b"']
        for i, lp in enumerate(lps):
            size, sha, nm = info[lp]
            lines.append(f'if [ -f {q(lp)} ]; then echo "@@SAME {i} $(wc -c < {q(lp)})"; fi')
            lines.append(f'd="$b/{sha[:12]}"; if [ -f "$d"/{q(nm)} ]; then echo "@@HAVE {i}"; else '
                         f'o=$(ls "$d" 2>/dev/null | grep -v "^\\." | head -n 1); '
                         f'if [ -n "$o" ]; then echo "@@OTHER {i} $o"; fi; fi')
        try:
            r = remote_cmd(h, "\n".join(lines), timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ChildError(f"{name}: cannot check its copies of the input files: {exc}")
        base, same, have, other = None, {}, set(), {}
        for line in r.stdout.splitlines():
            p = line.split(" ", 2)
            try:
                if p[0] == "@@BASE":
                    base = line[len("@@BASE "):].strip()
                elif p[0] == "@@SAME":
                    same[int(p[1])] = int(p[2].strip())
                elif p[0] == "@@HAVE":
                    have.add(int(p[1]))
                elif p[0] == "@@OTHER":
                    other[int(p[1])] = p[2].strip()
            except (ValueError, IndexError):
                pass
        if not base or not base.startswith("/"):
            raise ChildError(f"{name}: cannot find where to put the input files ({(r.stderr or '').strip()[-200:]})")
        remote_of: dict = {}
        todo, links = [], []
        for i, lp in enumerate(lps):
            size, sha, nm = info[lp]
            if same.get(i) == size:
                continue                           # the same path is there (a shared folder): left as it is
            d = f"{base}/{sha[:12]}"
            remote_of[lp] = f"{d}/{nm}"
            if i in have:
                continue
            if i in other:                         # the same content under another name: linked, not sent again
                links.append(f'd={q(d)}; ln -f "$d"/{q(other[i])} "$d"/{q(nm)} 2>/dev/null || '
                             f'cp -p "$d"/{q(other[i])} "$d"/{q(nm)} || exit 1')
                continue
            todo.append(lp)
        if links:
            try:
                rl = remote_cmd(h, "\n".join(links) + "\necho @@OK", timeout=120)
                if "@@OK" not in rl.stdout:
                    raise OSError((rl.stderr or "").strip()[-200:])
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ChildError(f"{name}: cannot link a copy of an input file: {exc}")
        ups = []
        with self.up_lock:
            for lp in todo:
                k = (name, remote_of[lp])
                u = self.uploads.get(k)
                if u is None or u.error or u.done:     # done but not there any more: sent again
                    u = Upload(h, lp, remote_of[lp], info[lp][0], info[lp][1])
                    self.uploads[k] = u
                ups.append(u)
        end = None if deadline is None else deadline - margin
        while any(not u.done and not u.error for u in ups) and (end is None or time.time() < end):
            time.sleep(0.1)
        bad = [u for u in ups if u.error]
        if bad:
            raise ChildError(f"{name}: copying {bad[0].local} there failed: {bad[0].error}")
        staged = dict(remote_of)
        for lp, dest in staged.items():
            self.unstaged[dest] = lp
        pending = {u.local: u.progress() for u in ups if not u.done}
        if pending:
            return args, staged, pending
        return self.rewrite(args, {o: remote_of[lp] for o, lp in files.items() if lp in remote_of}), staged, None

    # ---- answers
    @staticmethod
    def parse(result: dict):
        for item in result.get("content") or []:
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except (json.JSONDecodeError, TypeError):
                    return None
        return None

    @staticmethod
    def with_fields(result: dict, fields: dict) -> dict:
        """The result with fields added to its JSON text (and structuredContent)."""
        out = copy.deepcopy(result)
        for item in out.get("content") or []:
            if item.get("type") == "text":
                try:
                    data = json.loads(item["text"])
                except (json.JSONDecodeError, TypeError):
                    break
                if isinstance(data, dict):
                    item["text"] = json.dumps({**fields, **{k: v for k, v in data.items() if k not in fields}},
                                              indent=1)
                break
        sc = out.get("structuredContent")
        if isinstance(sc, dict):
            out["structuredContent"] = {**fields, **sc}
        return out

    @staticmethod
    def text_result(data, error: bool = False) -> dict:
        return {"content": [{"type": "text", "text": json.dumps(data, indent=1, default=str)}], "isError": error}

    # ---- calls
    def call(self, name: str, tool: str, args: dict, deadline: float | None = None) -> dict:
        """Calls the tool on a machine, after copying the local files the arguments name there ('staged' in the
        answer). A copy that does not finish in time goes on in the background, and the answer says to repeat the
        call (status 'uploading'): nothing ran yet, and files already there are not sent again."""
        args, staged, uploading = self.stage(name, tool, args, deadline,
                                             margin=12.0 if tool in self.wait_defaults else 6.0)
        if uploading:
            eta = max([u.get("eta_s") or 30 for u in uploading.values()])
            return self.text_result({
                "status": "uploading", "machine": name, "uploading": uploading, "staged": staged,
                "next": f"the input files are being copied to {name}; nothing ran yet. Call {tool} again with the same "
                        f"arguments in about {eta} s: the copy goes on meanwhile, and files already there are not sent "
                        f"again."})
        r = self._call(name, tool, args, deadline)
        return self.with_fields(r, {"staged": staged}) if staged else r

    def _call(self, name: str, tool: str, args: dict, deadline: float | None = None) -> dict:
        """Calls the tool on a machine. With a deadline, the tool's own wait (wait_s) is shortened to fit, and the
        call gives up at the deadline (it may still finish there: the answer says how to check)."""
        timeout = CALL_TIMEOUT
        if deadline is not None:
            rem = deadline - time.time()
            if tool in self.wait_defaults:
                want = args.get("wait_s", self.wait_defaults[tool])
                try:
                    want = float(want)
                except (TypeError, ValueError):
                    want = 0.0
                if tool in ("study_status", "bench_status") and any(c.inflight for c in self.children.get(name, [])):
                    want = min(want, BUSY_STATUS_WAIT)  # a busy machine answers slowly: end well inside the minute
                args = dict(args, wait_s=max(0.0, min(want, rem - 8.0)))
            timeout = max(3.0, rem)
        try:
            for attempt in (0, 1):
                c = self.child(name, timeout=timeout)
                try:
                    r = c.request("tools/call", {"name": tool, "arguments": args}, timeout=timeout)
                except ChildError as exc:
                    if attempt or not self.stale_child(c, str(exc)):
                        raise
                    continue
                if attempt or not (r.get("isError") and self.stale_child(c, json.dumps(r.get("content", "")))):
                    if not r.get("isError"):
                        self.note_serving(c, self.parse(r))
                        self.check_serving(name, c)
                    return r
            return r
        except ChildError as exc:
            if "no answer" in str(exc):
                raise ChildError(f"{name} did not answer {tool} in time (an MCP call must end within a minute). It may "
                                 f"still be working there: look with jobs() or study_status before running it again")
            raise

    def stale_child(self, c: Child, text: str) -> bool:
        """True when the server refused the call (or ended) because a newer release is installed there (it says
        STALE_SERVER and exits while idle, so nothing of the call ran): the child is dropped and the call can be
        repeated on a new server, which runs the installed release."""
        if "server ended" in text or "connection is closed" in text:
            time.sleep(0.3)                             # its last stderr line may still be on the way
            text += c.tail()
        if STALE_MARK not in text:
            return False
        log(f"{c.host['name']}: its server runs an old release; starting a new one")
        with self.child_lock:
            pool = self.children.get(c.host["name"], [])
            if c in pool:
                pool.remove(c)
        c.close()
        return True

    def call_all(self, names: list, tool: str, args: dict, deadline: float | None = None) -> dict:
        res: dict = {}

        def one(n):
            try:
                r = self.call(n, tool, args, deadline)
                data = self.parse(r)
                self.learn(data, n)
                res[n] = data if data is not None else r
            except ChildError as exc:
                res[n] = {"unreachable": str(exc)}
            except ValueError as exc:
                res[n] = {"error": str(exc)}
        ths = {n: threading.Thread(target=one, args=(n,), daemon=True) for n in names}
        for t in ths.values():
            t.start()
        for t in ths.values():
            t.join(None if deadline is None else max(0.0, deadline - time.time() + 1.0))
        for n in names:
            res.setdefault(n, {"unreachable": f"{n}: no answer in time"})
        return res

    def tool_args(self, tool: str) -> dict:
        """The arguments a tool declares (its input schema's properties), from the current definitions."""
        t = next((t for t in self.tools if t.get("name") == tool), None)
        return ((t or {}).get("inputSchema") or {}).get("properties") or {}

    @staticmethod
    def request_mem(studies: dict) -> tuple:
        """-> (MB, as written) of the largest run.mem among the studies; (None, None) when none sets one."""
        best = (None, None)
        for s in (studies or {}).values():
            run = s.get("run") if isinstance(s, dict) else None
            v = run.get("mem") if isinstance(run, dict) else None
            mb = parse_mem_mb(v)
            if mb and (best[0] is None or mb > best[0]):
                best = (mb, str(v))
        return best

    @staticmethod
    def mem_cap_mb(st: dict) -> float | None:
        """The most memory one job can get on the machine: executor.caps.mem_mb, else caps.mem parsed. None:
        unknown (an older version, or no limit reported)."""
        caps = ((st.get("executor") or {}).get("caps") or {})
        v = caps.get("mem_mb")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return float(v)
        return parse_mem_mb(caps.get("mem"))

    # ---- init_from: a study starting from a finished run of another machine (cfdt 2026.11.13.6)
    def ref_hits(self, ref: str) -> list:
        """[(machine, request folder there, study)] for '<request>/<study>' or an absolute study path, from the
        request map (the last one learned last)."""
        ref = ref.rstrip("/")
        if ref.startswith("/"):
            folder, _, study = ref.rpartition("/")
            n = self.paths.get(folder)
            return [(n, folder, study)] if n and study else []
        req, _, study = ref.partition("/")
        if not req or not study or "/" in study:
            return []
        return [(n, p, study) for p, n in list(self.paths.items()) if p.rstrip("/").rsplit("/", 1)[-1] == req]

    def init_refs(self, studies: dict) -> list:
        """The studies' init_from refs to a request the router knows: [{study, keys, ref, fields, hits}]."""
        if self.key not in INIT_FROM_KEYS or not isinstance(studies, dict):
            return []
        out = []
        for name, st in studies.items():
            for keys, ref, fields in init_from_refs(st):
                hits = self.ref_hits(ref)
                if hits:
                    out.append({"study": name, "keys": keys, "ref": ref, "fields": fields, "hits": hits})
        return out

    @staticmethod
    def foreign(r: dict, machine: str) -> tuple | None:
        """(N, request folder on N, study) when r's request is on another machine than machine, else None."""
        if any(h[0] == machine for h in r["hits"]):
            return None
        return r["hits"][-1]

    def init_check(self, n: str, folder: str, study: str, fields: str) -> dict:
        """`<cli> init-check <folder>/<study>` on machine n (cached a minute when ok), with the source result's
        modification time (to tell whether a copy made earlier is still current)."""
        key = (n, f"{folder}/{study}", fields)
        hit = self.init_checks.get(key)
        if hit and time.time() - hit[0] < INIT_CHECK_TTL and hit[1].get("ok"):
            return hit[1]
        h = self.host(n)
        path = f"{folder}/{study}"
        q = shlex.quote
        script = (f"{root_expr(h['root'])}/bin/run {q(self.t['cli'])} init-check {q(path)} --fields {q(fields)}; "
                  f"echo; echo @@MTIME; python3 -c 'import os,sys; print(os.path.getmtime(sys.argv[1]))' "
                  f"{q(f'{path}/{study}.json')} 2>/dev/null")
        try:
            r = remote_cmd(h, script, timeout=120)
            text, _, mt = r.stdout.partition("@@MTIME")
            c = parse_json_out(text) or {"ok": False, "problems": [f"no answer from {self.t['cli']} init-check on {n}: "
                                                                   f"{(r.stderr or text).strip()[-200:]}"]}
            try:
                c["result_mtime"] = float(mt.split()[0])
            except (ValueError, IndexError):
                pass
        except (OSError, subprocess.TimeoutExpired) as exc:
            c = {"ok": False, "problems": [f"cannot check on {n}: {exc}"]}
        self.init_checks[key] = (time.time(), c)
        return c

    def check_refs(self, refs: list) -> None:
        """init-check on each ref's machine, in parallel; the answers go into the refs ('check')."""
        def one(r):
            n, folder, study = r["hits"][-1]
            r["check"] = self.init_check(n, folder, study, r["fields"])
        ths = [threading.Thread(target=one, args=(r,), daemon=True) for r in refs]
        for t in ths:
            t.start()
        for t in ths:
            t.join(150)

    def fix_init_validation(self, v, machine: str, refs: list) -> None:
        """A validation answer of machine for studies whose init_from is a run of another machine: its 'no case
        ... on this machine' goes (the router copies the run when the study runs) when that machine's check is ok;
        that machine's problems are added when it is not."""
        if not isinstance(v, dict) or not isinstance(v.get("studies"), dict):
            return
        for r in refs:
            src = self.foreign(r, machine)
            rec = v["studies"].get(r["study"])
            if not src or not isinstance(rec, dict):
                continue
            c = r.get("check") or {}
            errs = [e for e in rec.get("errors") or [] if not ("init_from" in e and "no case in" in e)]
            if c.get("ok"):
                rec["init_from"] = {"from": f"{src[0]}:{r['ref']}", "bytes": c.get("init_bytes"),
                                    "note": f"copied from {src[0]} when the study runs"}
            else:
                errs += [f"init_from {src[0]}:{r['ref']}: {p}" for p in c.get("problems") or ["cannot be checked"]]
            rec["errors"] = errs
            rec["ok"] = not errs
        v["ok"] = all(x.get("ok") for x in v["studies"].values() if isinstance(x, dict)) and not v.get("errors")

    @staticmethod
    def source_mtime(check) -> float | None:
        """When what init_from takes from a source run last changed: its latest time directory (init-check's
        fields_mtime, cfdt 2026.11.13.8), else its result file. A reprocess rewrites the result, not the fields, so
        with fields_mtime it does not make an imported copy out of date (no new multi-GB copy)."""
        c = check if isinstance(check, dict) else {}
        for k in ("fields_mtime", "result_mtime"):
            v = c.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
        return None

    def imported_on(self, m: str, n: str, folder: str, studies: dict) -> dict:
        """{study: init_from ref on m} for studies (name -> when the source last changed: source_mtime) already
        imported on m from n's request folder by an earlier call, and packed after that."""
        h = self.host(m)
        out_m = (self.status(m) or {}).get("out_dir")
        if not out_m:
            return {}
        name = f"{folder.rstrip('/').rsplit('/', 1)[-1]}@{n}"
        prog = ("import glob,json,os,sys\n"
                "a=json.loads(sys.argv[1]); res={}\n"
                "ds=[d for d in glob.glob(os.path.join(a['out'],a['name']+'*')) if os.path.basename(d)==a['name'] "
                "or os.path.basename(d).startswith(a['name']+'_import')]\n"
                "for d in sorted(ds,key=os.path.getmtime,reverse=True):\n"
                " try: m=json.load(open(os.path.join(d,'request.json')))\n"
                " except Exception: continue\n"
                " pk=(m.get('imported') or {}).get('packed') or 0\n"
                " for s,mt in a['studies'].items():\n"
                "  if s not in res and os.path.isdir(os.path.join(d,s,'case')) and pk>=(mt or 0): "
                "res[s]=os.path.basename(d)+'/'+s\n"
                "print(json.dumps(res))\n")
        arg = json.dumps({"out": out_m, "name": name, "studies": studies})
        try:
            r = remote_cmd(h, f"python3 -c {shlex.quote(prog)} {shlex.quote(arg)}", timeout=60)
            return parse_json_out(r.stdout) or {}
        except (OSError, subprocess.TimeoutExpired):
            return {}

    def stage_inits(self, m: str, studies: dict, refs: list, deadline: float | None, margin: float = 12.0) -> tuple:
        """The init_from runs of other machines, copied to m (or found there from an earlier call).
        -> (studies with their init_from on m, staged {study: {from, to, bytes}}, uploading {...} or None)."""
        foreign = [(r, self.foreign(r, m)) for r in refs]
        foreign = [(r, s) for r, s in foreign if s]
        if not foreign:
            return studies, {}, None
        groups: dict = {}
        for r, (n, folder, study) in foreign:
            groups.setdefault((n, folder), []).append((r, study))
        new = copy.deepcopy(studies)
        staged, pending = {}, {}
        out_m = (self.status(m) or {}).get("out_dir")
        if not out_m:
            raise ValueError(f"init_from: cannot tell {m}'s output folder ({self.key.upper()}_OUT): machines() shows why")
        for (n, folder), items in groups.items():
            src = sorted({s for _, s in items})
            mtimes = {s: next(self.source_mtime(r.get("check")) for r, s2 in items if s2 == s) for s in src}
            got = self.imported_on(m, n, folder, mtimes)
            need = [s for s in src if s not in got]
            nbytes = {s: next(((r.get("check") or {}).get("init_bytes") for r, s2 in items if s2 == s), None) for s in src}
            if need:
                key = (m, n, folder, tuple(need))
                # the source's state names the copy: a finished one is reused only while the source is unchanged
                tag = hashlib.sha1(json.dumps([n, folder, need, [mtimes[s] for s in need]]).encode()).hexdigest()[:10]
                with self.up_lock:
                    t = self.transfers.get(key)
                    if t is None or t.error or (t.done and t.tag != tag):
                        out_n = (self.status(n) or {}).get("out_dir")
                        if not out_n:
                            raise ValueError(f"init_from: cannot tell {n}'s output folder: machines() shows why")
                        t = Transfer(self.t["cli"], self.host(n), self.host(m), folder, need, out_n, out_m,
                                     sum(int(nbytes[s] or 0) for s in need) or None, tag)
                        self.transfers[key] = t
                end = None if deadline is None else deadline - margin
                while not t.done and not t.error and (end is None or time.time() < end):
                    time.sleep(0.2)
                if t.error:
                    raise ValueError(f"init_from: copying {n}:{folder} ({', '.join(need)}) to {m} failed: {t.error}")
                if not t.done:
                    pending[f"{n}:{folder}"] = t.progress()
                    continue
                if t.request:
                    self.remember(t.request, m)
                miss = [s for s in need if s not in (t.result or {})]
                if miss:
                    raise ValueError(f"init_from: {m} imported {t.name} without {', '.join(miss)}")
                got.update(t.result)
                for s in need:
                    nbytes[s] = nbytes[s] or (t.total if len(need) == 1 else None)
            for r, s in items:
                set_in(new[r["study"]], r["keys"], got[s])
                staged[r["study"]] = {"from": f"{n}:{r['ref']}", "to": got[s], "bytes": nbytes.get(s)}
        return new, staged, (pending or None)

    def post_validate(self, machine: str, tool: str, args: dict, r: dict) -> dict:
        """validate_studies answered by machine: studies whose init_from is a run of another machine are checked
        there (init-check; nothing is copied) instead of failing for 'no case ... on this machine'."""
        if tool != self.validate_tool() or r.get("isError"):
            return r
        arg = next((a for a in PLACE_ARGS if isinstance(args.get(a), dict)), None)
        refs = [x for x in (self.init_refs(args[arg]) if arg else []) if self.foreign(x, machine)]
        data = self.parse(r)
        if not refs or not isinstance(data, dict):
            return r
        self.check_refs(refs)
        self.fix_init_validation(data, machine, refs)
        out = self.text_result(data)
        if isinstance(r.get("structuredContent"), dict):
            out["structuredContent"] = data
        return out

    def transferring_to(self, refs: list, active: bool = False) -> list:
        """Machines that received (active: are receiving) the init_from runs of these refs: no copy to make there;
        a call repeated while one copies stays there."""
        want = {(r["hits"][-1][0], r["hits"][-1][1]) for r in refs}
        with self.up_lock:
            return list(dict.fromkeys(m for (m, n, folder, _), t in self.transfers.items()
                                      if (n, folder) in want and not t.error and not (active and t.done)))

    def validate_tool(self) -> str | None:
        names = {t["name"] for t in self.tools}
        return next((n for n in ("validate_studies", "validate_goals") if n in names), None)

    def place(self, tool: str, args: dict, deadline: float | None = None) -> tuple:
        """-> (machine name, args to send, router info) for a call that starts work. Placement takes at most about
        half of the call's time budget; the rest is the run's own wait."""
        deadline = deadline or (time.time() + BUDGET)
        p_end = time.time() + (deadline - time.time()) * 0.5
        warnings: list = []
        arg = next((a for a in PLACE_ARGS if isinstance(args.get(a), dict)), None)
        studies = copy.deepcopy(args.get(arg)) if arg else {}
        manual: list | None = None
        for s in studies.values():
            run = s.get("run") if isinstance(s, dict) else None
            if isinstance(run, dict) and "host" in run:
                want = run.pop("host")
                want = [want] if isinstance(want, str) else list(want or [])
                if not run:
                    s.pop("run")
                if manual is not None and want != manual:
                    raise ValueError(f"the studies ask for different machines ({manual} and {want}): "
                                     f"one call goes to one machine; split it")
                manual = want
        send = dict(args, **({arg: studies} if arg else {}))
        sts = self.statuses([h["name"] for h in self.ordered()], wait=min(STATUS_WAIT, max(1.0, p_end - time.time())))
        cands = self.candidates(sts)
        unreach = [f"{n} ({str(s.get('error') or 'no answer')[:80]})" for n, s in sts.items() if not s.get("reachable")]
        if unreach:
            warnings.append(f"left out: {'; '.join(unreach)}")
        if manual:
            unknown = [m for m in manual if not self.host(m)]
            if unknown:
                raise ValueError(f"run.host: unknown machine {', '.join(unknown)} (known: "
                                 f"{', '.join(h['name'] for h in self.hosts)})")
            usable = [m for m in manual if m in cands]
            if not usable:
                raise ValueError(f"run.host: {', '.join(manual)} cannot run {self.key} now: " +
                                 "; ".join(f"{m}: {sts.get(m, {}).get('error') or 'translator or backends missing'}"
                                           for m in manual))
            cands = usable
        # memory: a machine whose largest job memory (executor.caps) is below the request's run.mem is left out,
        # before anything is validated or copied there
        need, need_txt = self.request_mem(studies) if arg else (None, None)
        if need:
            fit, short = [], []
            for n in cands:
                cap = self.mem_cap_mb(sts.get(n) or {})
                (short if cap is not None and cap < need else fit).append((n, cap))
            if short and not fit:
                raise ValueError(f"no machine can give run.mem {need_txt}: " + ", ".join(
                    f"{n} {fmt_mb(c)}" for n, c in short) + ". Lower run.mem, or split the study.")
            for n, cap in short:
                warnings.append(f"{n} left out: run.mem {need_txt} > its {fmt_mb(cap)}")
            cands = [n for n, _ in fit]
        # init_from a finished run of another machine: checked there first (an unfinished one refuses the call
        # before anything is copied); it does not pin the call to that machine, the copy's time counts instead
        refs = self.init_refs(studies) if arg else []
        if refs:
            self.check_refs(refs)
            bad = [f"{r['study']}: init_from {r['hits'][-1][0]}:{r['ref']}: {p}" for r in refs
                   if not (r.get("check") or {}).get("ok") for p in (r.get("check") or {}).get("problems") or ["not ready"]]
            if bad:
                raise ValueError("; ".join(bad) + ". Nothing was submitted or copied.")
            if not manual:
                there = [n for n in self.transferring_to(refs, active=True) if n in cands]
                if there:
                    cands = there[:1]
                    warnings.append(f"kept on {there[0]}: the init_from run is copied there")
        no_init = copy.deepcopy(studies)
        for r in refs:
            set_in(no_init[r["study"]], r["keys"], None)
        aff = self.affinity(no_init) if arg else None
        if aff and not manual:
            if aff in cands:
                cands = [aff]
                warnings.append(f"kept on {aff}: the studies refer to a request there")
            else:
                warnings.append(f"the studies refer to a request on {aff}, which cannot take it now")
        # files on this computer: small ones are copied to every candidate to validate there; large ones only to the
        # chosen machine, and a call repeated while they copy stays on the machine receiving them
        files = self.local_files({arg: studies}) if arg else {}
        big = bool(files) and self.check_sizes(files) > STAGE_ALL_MB * MB
        if big and not manual and not aff:
            there = [n for n in cands if n in self.uploading_to(files)]
            if there:
                cands = there[:1]
                warnings.append(f"kept on {there[0]}: the input files are copied there")
        if not cands:
            raise ValueError("no machine can run " + self.key + " now: " +
                             "; ".join(f"{n}: {s.get('error') or ('backends missing: ' + ', '.join(s.get('backends_missing') or [])) if s.get('reachable') else s.get('error', 'unreachable')}"
                                       for n, s in sts.items()))
        vers = {n: sts[n].get("version") for n in cands}
        if len(set(vers.values())) > 1:
            warnings.append(f"{self.key} versions differ: " + ", ".join(f"{n} {v}" for n, v in vers.items()))
        # validation on each candidate: validity and estimates
        est: dict = {}
        vt = self.validate_tool() if arg else None
        if big and len(cands) > 1:
            vt = None
            warnings.append(f"the input files ({self.check_sizes(files) / MB:.0f} MB) are copied only to the chosen "
                            f"machine: placed by the queues, without validating on each machine first")
        if vt and studies:
            vres = self.call_all(cands, vt, {arg: studies}, deadline=min(p_end, time.time() + 15.0))
            ok = []
            for n in cands:
                v = vres.get(n)
                if refs:
                    self.fix_init_validation(v, n, refs)
                if isinstance(v, dict) and "unreachable" not in v:
                    src = v.get("studies") if isinstance(v.get("studies"), dict) else \
                        v.get("goals") if isinstance(v.get("goals"), dict) else v
                    per = {k: x for k, x in src.items() if isinstance(x, dict) and "ok" in x}
                    if per and all(x.get("ok") for x in per.values()):
                        ok.append(n)
                    es = [x.get("estimate") for x in per.values() if isinstance(x.get("estimate"), dict)]
                    if es:
                        est[n] = {"minutes": sum(float(e.get("minutes") or 0) for e in es),
                                  "procs": max(int(e.get("procs") or 1) for e in es),
                                  "mem_mb": max(float(e.get("mem_mb") or 0) for e in es) or None}
            if ok and len(ok) < len(cands):
                warnings.append("not valid on " + ", ".join(n for n in cands if n not in ok) +
                                " (an older version?): left out")
                cands = ok
        starts: dict = {}

        def one_start(n):
            st_, e_ = sts[n], est.get(n, {})
            pr = int(e_.get("procs") or (st_.get("executor") or {}).get("procs") or 1)
            starts[n] = self.expected_start(n, st_, pr, e_.get("minutes") or 0, e_.get("mem_mb"))
        ths = {n: threading.Thread(target=one_start, args=(n,), daemon=True) for n in cands}
        for t in ths.values():
            t.start()
        for t in ths.values():
            t.join(max(0.0, p_end - time.time()))
        rows = []
        for i, n in enumerate(cands):
            st = sts[n]
            e = est.get(n, {})
            minutes = e.get("minutes")
            start, why = starts.get(n, (None, "no queue estimate in time"))
            if start is None:
                if (st.get("executor") or {}).get("kind") == "slurm":
                    start = 1e6                # SLURM gave no start time (node down? limits?): only as a last resort
                    why = "SLURM gives no start time: " + why
                else:
                    start = 0.0
            run_min = (minutes or 0) * float(self.speed.get(n, 1.0))
            small = minutes is not None and minutes < self.small
            # init_from runs of another machine: copied here first (at COPY_MB_S), unless already imported here
            copy_b = sum(int((r.get("check") or {}).get("init_bytes") or 0) for r in refs
                         if self.foreign(r, n) and n not in self.transferring_to([r]))
            copy_min = copy_b / (COPY_MB_S * MB) / 60.0
            score = start + (0 if small else run_min) + copy_min - (0.5 * (len(self.prefer) - self.prefer.index(n))
                                                                     if n in self.prefer else 0)
            rows.append({"machine": n, "start_min": round(start, 1), "run_min": round(run_min, 1) if minutes else None,
                         "score": round(score, 2), "why": why + (f", estimate {minutes:.0f} min" if minutes else "") +
                         (f", copies {copy_b / MB / 1024:.1f} GB of init_from first (~{copy_min:.0f} min)" if copy_b else ""),
                         "version": st.get("version")})
        rows.sort(key=lambda r: (r["score"], cands.index(r["machine"])))
        best = rows[0]
        info = {"chose": best["machine"], "why": best["why"], "candidates": rows}
        if manual:
            info["why"] = f"chosen in run.host; {best['why']}"
        if warnings:
            info["warnings"] = warnings
        if refs:
            info["_init_refs"] = refs           # for tool_call: the copies (taken out of the answer)
        return best["machine"], send, info

    @staticmethod
    def take_host(args: dict) -> tuple:
        """-> (machines named in run.host, args without run.host) for studies / goals / study arguments."""
        pinned: list = []
        out = dict(args)
        for a in (*PLACE_ARGS, "study"):
            v = args.get(a)
            if not isinstance(v, dict):
                continue
            items = {"_": v} if a == "study" else v
            new = {}
            for k, st in items.items():
                if isinstance(st, dict) and isinstance(st.get("run"), dict) and "host" in st["run"]:
                    st = copy.deepcopy(st)
                    want = st["run"].pop("host")
                    for m in ([want] if isinstance(want, str) else list(want or [])):
                        if m not in pinned:
                            pinned.append(m)
                    if not st["run"]:
                        st.pop("run")
                new[k] = st
            out[a] = new["_"] if a == "study" else new
        return pinned, out

    @staticmethod
    def put_host(args: dict, pinned: list) -> dict:
        """Puts run.host back on every study, for place() to read (it removes it again before sending)."""
        out = dict(args)
        for a in PLACE_ARGS:
            if isinstance(args.get(a), dict):
                out[a] = {k: ({**st, "run": {**(st.get("run") or {}), "host": pinned}} if isinstance(st, dict) else st)
                          for k, st in args[a].items()}
        return out

    def route_path(self, args: dict) -> tuple:
        """-> (machine or None, args) for a call about an existing request or file."""
        for a in PATH_ARGS:
            v = args.get(a)
            if isinstance(v, str):
                m = re.match(r"^([A-Za-z0-9_.-]+):(/.*)$", v)
                if m and self.host(m.group(1)):
                    return m.group(1), dict(args, **{a: m.group(2)})
                return self.owner(v.rstrip("/")), args
        return None, args

    def locate(self, path: str) -> list:
        """The machines (in preference order) where path exists."""
        hits: dict = {}

        def one(h):
            try:
                r = remote_cmd(h, f"test -e {shlex.quote(path)} && echo yes", timeout=15)
                hits[h["name"]] = "yes" in r.stdout
            except (OSError, subprocess.TimeoutExpired):
                hits[h["name"]] = False
        ths = [threading.Thread(target=one, args=(h,)) for h in self.ordered()]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        return [h["name"] for h in self.ordered() if hits.get(h["name"])]

    def default_machine(self) -> str:
        sts = self.statuses([h["name"] for h in self.ordered()])
        c = self.candidates(sts) or [n for n, s in sts.items() if s.get("reachable")]
        if not c:
            raise ValueError("no machine answers: " + "; ".join(f"{n}: {s.get('error')}" for n, s in sts.items()))
        return c[0]

    def tool_call(self, tool: str, args: dict) -> dict:
        deadline = time.time() + BUDGET
        if tool == "machines":
            return self.text_result(self.machines())
        try:
            if tool in ALL_TOOLS or tool in BROADCAST_TOOLS:
                sts = self.statuses([h["name"] for h in self.ordered()])
                names = [n for n in self.candidates(sts)] or [n for n, s in sts.items() if s.get("reachable")]
                return self.text_result({"machines": self.call_all(names, tool, args, deadline)})
            pinned, args = self.take_host(args)
            if pinned and not (tool == "run_benchmarks" or (not tool.startswith(("validate_", "get_")) and
                                                           any(isinstance(args.get(a), dict) for a in PLACE_ARGS))):
                sts = self.statuses([h["name"] for h in self.ordered()])
                n = next((m for m in pinned if sts.get(m, {}).get("reachable")), None)
                if n is None:
                    raise ValueError(f"run.host: {', '.join(pinned)} does not answer now")
                r = self.post_validate(n, tool, args, self.call(n, tool, args, deadline))
                return self.with_fields(r, {"machine": n})
            if pinned:                                                   # placement reads it back from the studies
                args = self.put_host(args, pinned)
            starts_work = tool == "run_benchmarks" or (not tool.startswith(("validate_", "get_")) and
                                                        any(isinstance(args.get(a), dict) for a in PLACE_ARGS))
            if starts_work:
                # a request name chosen here: if the machine does not answer in time, the call may still have
                # submitted, and the answer can say where to look (repeating the name never submits twice)
                rname = args.get("request_name")
                if rname is None and "request_name" in self.tool_args(tool):
                    rname = f"run_{_dt.datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(3)}"
                    args = dict(args, request_name=rname)
                name, send, info = self.place(tool, args, deadline)
                refs = info.pop("_init_refs", [])
                staged_init = {}
                if refs:                                   # init_from runs of another machine: copied here first
                    arg = next(a for a in PLACE_ARGS if isinstance(send.get(a), dict))
                    new, staged_init, pend = self.stage_inits(name, send[arg], refs, deadline)
                    if pend:
                        eta = max([p.get("eta_s") or 60 for p in pend.values()])
                        return self.text_result({
                            "status": "uploading", "machine": name, "router": info, "uploading": {"init_from": pend},
                            "next": f"the runs to start from are being copied to {name}; nothing was submitted yet. "
                                    f"Call {tool} again with the same arguments in about {eta} s: the copy goes on "
                                    f"meanwhile, and the next call uses it."})
                    send = dict(send, **{arg: new})
                req = None
                if isinstance(rname, str):
                    out = (self.status_cache.get(name, (0, {}))[1] or {}).get("out_dir") or \
                        f"~/{self.default_out() or self.key + '_runs'}"
                    req = f"{out}/{rname}"
                    self.remember(req, name)
                try:
                    r = self.call(name, tool, send, deadline)
                except ChildError as exc:
                    if not req or "in time" not in str(exc):
                        raise
                    return self.text_result({
                        "status": "submitted_unconfirmed", "machine": name, "request": req, "request_name": rname,
                        "router": info,
                        "next": f"{name} did not answer within the call's time; the studies may have been submitted. "
                                f"Call study_status(request='{req}') in a minute; do not submit again (repeating "
                                f"run_studies with request_name='{rname}' is safe: it returns the status)."})
                self.forget(name)                     # it has new work: the next placement asks it again
                self.learn(self.parse(r), name)
                extra = {"machine": name, "router": info}
                if staged_init:
                    prev = (self.parse(r) or {}).get("staged") if isinstance(self.parse(r), dict) else None
                    extra["staged"] = {**(prev if isinstance(prev, dict) else {}), "init_from": staged_init}
                return self.with_fields(r, extra)
            name, send = self.route_path(args)
            if name:
                r = self.call(name, tool, send, deadline)
                self.learn(self.parse(r), name)
                return self.with_fields(r, {"machine": name})
            if any(isinstance(args.get(a), str) for a in PATH_ARGS):     # a path the router has not seen: find it
                path = next(send[a] for a in PATH_ARGS if isinstance(send.get(a), str)).rstrip("/")
                where = self.locate(path)
                if not where:
                    return self.text_result({"error": f"no machine has {path}",
                                             "router": "machines() lists them; machine:/path chooses one"}, True)
                if len(where) > 1 and tool not in READ_ONLY:
                    return self.text_result({"error": f"{path} exists on {', '.join(where)} (a shared filesystem?): "
                                                      f"say which machine ran it, as machine:{path}"}, True)
                n = where[0]
                self.remember(path, n)
                r = self.call(n, tool, send, deadline)
                self.learn(self.parse(r), n)
                return self.with_fields(r, {"machine": n})
            name = self.default_machine()
            r = self.post_validate(name, tool, args, self.call(name, tool, args, deadline))
            self.learn(self.parse(r), name)
            return self.with_fields(r, {"machine": name})
        except (ValueError, ChildError) as exc:
            return self.text_result({"error": str(exc), "router": "llmt-router (machines() shows the machines)"}, True)

    def machines(self) -> dict:
        sts = self.statuses([h["name"] for h in self.hosts], fresh=True, wait=30.0)
        # which version each machine's server process runs (connecting if needed), in parallel
        ths = []
        for h in self.hosts:
            if sts.get(h["name"], {}).get("version"):
                def one(n=h["name"]):
                    try:
                        c = self.child(n, timeout=15.0)
                        self.probe_serving(n, c, timeout=12.0)
                    except ChildError:
                        pass
                t = threading.Thread(target=one, daemon=True)
                t.start()
                ths.append(t)
        end = time.time() + 15.0
        for t in ths:
            t.join(max(0.0, end - time.time()))
        out = []
        for h in self.hosts:
            st = sts.get(h["name"], {})
            row = {"machine": h["name"], "ssh": h.get("ssh") or "(this machine)", "root": h["root"],
                   "preferred": h["name"] in self.prefer, "excluded": h["name"] in self.exclude,
                   "speed": self.speed.get(h["name"], 1.0)}
            row.update({k: v for k, v in st.items() if k not in ("name", "executor", "_asked", "release")})
            # the shared install's bundle (docs/SHARED.md), not the translator's version: named so it can't be mistaken
            row["llmt_release"] = st.get("release")
            # the release's name counts deploys on that machine; the commit says what it contains (same everywhere)
            ex = st.get("executor") or {}
            if ex:
                row["executor"] = {k: ex.get(k) for k in ("kind", "procs", "time_limit_min", "caps", "runtime") if k in ex}
                cap = self.mem_cap_mb(st)
                if cap:                       # the most one job can get: a larger run.mem is placed elsewhere
                    row["max_job_mem"] = fmt_mb(cap)
            if st.get("reachable") and st.get("version"):
                serv = sorted({c.serving or "?" for c in self.children.get(h["name"], []) if c.alive()})
                row["serving"] = (serv[0] if len(serv) == 1 else serv) if serv else None
                stale = [v for v in serv if v not in ("?", st.get("version"))]
                if stale:
                    row["server_stale"] = True
                    row["note"] = (f"the server the router uses here runs {self.key} {', '.join(stale)}, but "
                                   f"{st.get('version')} is installed (llm-translators release {st.get('release') or '-'}): it is "
                                   f"replaced once idle, and jobs it submitted run the old code")
                ex = st.get("executor") or {}
                start, why = self.expected_start(h["name"], st, int(ex.get("procs") or 1), 10, None)
                row["typical_job_start_min"] = None if start is None else round(start, 1)
                row["typical_job"] = f"{int(ex.get('procs') or 1)} procs, 10 min: {why}"
            out.append(row)
        return {"translator": self.key, "machines": out,
                "placement": "lowest expected start + estimated minutes x speed (small jobs: start only); "
                             "run.host in a study chooses; preferred machines win ties (docs/ROUTER.md)"}

    # ---- the MCP server side
    def initialize(self, params: dict) -> dict:
        self.init_params = params or {}
        errors = []
        t_end = time.time() + 40.0
        # the tool definitions come from a machine with the newest version installed (a new server there runs it):
        # a client drops arguments its definitions lack, so an older machine's list would hide new ones
        order = self.ordered() or self.hosts
        sts = self.statuses([h["name"] for h in order], wait=15.0)
        order = sorted(order, reverse=True,                  # stable: preference order among equal versions
                       key=lambda h: vkey((sts.get(h["name"]) or {}).get("version"))
                       if (sts.get(h["name"]) or {}).get("reachable") else ())
        for h in order:
            try:
                c = self.child(h["name"], timeout=max(3.0, min(30.0, t_end - time.time())))
                self.instructions = (c.init_result or {}).get("instructions") or ""
                self.set_tools(c.request("tools/list", {}).get("tools", []))
                self.tools_version = (sts.get(h["name"]) or {}).get("version")
                proto = (c.init_result or {}).get("protocolVersion") or params.get("protocolVersion")
                break
            except ChildError as exc:
                errors.append(str(exc))
        else:
            proto = params.get("protocolVersion", "2025-06-18")
            self.instructions = (f"llm-translators router for {self.key}: no machine answered ({'; '.join(errors)}). "
                                 f"Call machines() to see why.")
        self.ready = True
        for h in self.ordered():           # warm the connections (and statuses not asked yet), so first calls don't wait
            if h["name"] not in self.status_cache:
                self.refresh(h["name"])
            self.warm(h["name"])
        names = ", ".join(h["name"] for h in self.hosts)
        note = (f"\nMachines: this server runs {self.key} on several machines ({names}). Calls that start work are "
                f"placed on the machine where they should finish first; answers say which ('machine', 'router'). "
                f"Paths in answers work with every tool here. A study's \"run\": {{\"host\": name or [names]}} "
                f"chooses the machine; machines() shows each one's load, queue and version. Files on this computer "
                f"(e.g. a STEP file under the home folder) can be given by their local path: the router copies them "
                f"to the machine and lists them in 'staged'; if it answers status 'uploading', repeat the same call "
                f"when it says.")
        return {"protocolVersion": proto, "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": f"{self.key} (llmt-router)", "version": __version__},
                "instructions": self.instructions + note}

    def tools_list(self) -> dict:
        own = {"name": "machines",
               "description": "The machines this server can run on, as the router sees them: reachable, version "
                              "(this translator's version installed; llmt_release is the llm-translators bundle, not it, named per machine: "
                              "llmt_commit tells whether two bundles are the same), serving (what the server in use runs; server_stale when they differ), "
                              "backends, executor (local or SLURM), CPUs and their use, this translator's queued and "
                              "running jobs, load, the expected start of a typical job, speed, preferred or excluded.",
               "inputSchema": {"type": "object", "properties": {}}}
        return {"tools": [t for t in self.tools if t.get("name") != "machines"] + [own]}

    def close(self) -> None:
        for pool in self.children.values():
            for c in pool:
                c.close()


def serve(key: str) -> None:
    router = Router(key)
    out_lock = threading.Lock()

    def send(msg: dict) -> None:
        data = json.dumps(msg) + "\n"
        with out_lock:
            sys.stdout.write(data)
            sys.stdout.flush()

    def handle(msg: dict) -> None:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        try:
            if method == "initialize":
                res = router.initialize(params)
            elif method == "tools/list":
                res = router.tools_list()
            elif method == "tools/call":
                tool = params.get("name", "")
                box: dict = {}
                t0 = time.time()

                def work():
                    try:
                        box["res"] = router.tool_call(tool, params.get("arguments") or {})
                    except Exception as exc:  # noqa: BLE001
                        box["exc"] = exc
                th = threading.Thread(target=work, daemon=True)
                th.start()
                th.join(BUDGET + 5.0)     # never let the Claude app time out: it would close this server for every chat
                if th.is_alive():
                    log(f"{tool}: no answer within {BUDGET + 5:.0f} s; answered with an error, the call goes on")
                    res = router.text_result({"error": f"{tool}: the machine did not answer within {BUDGET + 5:.0f} s. "
                                                       f"It may still be working: look with jobs() or study_status "
                                                       f"before running it again"}, True)
                elif "exc" in box:
                    raise box["exc"]
                else:
                    res = box["res"]
                    data = Router.parse(res) if isinstance(res, dict) else None
                    log(f"{tool} -> {(data or {}).get('machine', '?') if isinstance(data, dict) else '?'} "
                        f"({time.time() - t0:.1f} s{', error' if res.get('isError') else ''})")
            elif method == "ping":
                res = {}
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"{method} not supported"}})
                return
            send({"jsonrpc": "2.0", "id": mid, "result": res})
        except Exception as exc:  # noqa: BLE001
            log(f"{method}: {type(exc).__name__}: {exc}")
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}})

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log(f"not JSON: {raw[:120]}")
            continue
        if "method" not in msg or "id" not in msg:
            continue                                   # notifications (initialized, cancelled, ...)
        if msg["method"] == "initialize":
            handle(msg)                                # before anything else
            router.notify = lambda: send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        else:
            threading.Thread(target=handle, args=(msg,), daemon=True).start()
    router.close()


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    serve(sys.argv[1])


if __name__ == "__main__":
    main()
