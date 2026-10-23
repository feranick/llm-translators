#!/usr/bin/env python3
"""llmt_router.py — one MCP server for a translator, spread over several machines (docs/ROUTER.md).
Version: 2026.10.22.3

    bin/router KEY        (what the Claude app runs; stdio)

The Claude app sees one server (e.g. cfdt) with the translator's own tools plus machines(). Behind it, the router
keeps one connection per machine to the translator's real server (ssh HOST <root>/bin/mcp KEY, or locally), places
runs on the machine where they should finish first, routes follow-up calls by the request's machine, and merges
per-machine views. Python standard library only: it speaks MCP's stdio protocol (JSON-RPC, one message a line)
on both sides. Nothing but MCP messages goes to stdout; diagnostics go to stderr.
"""
from __future__ import annotations

import copy
import datetime as _dt
import importlib.util
import json
import math
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

__version__ = "2026.10.22.3"

HERE = Path(__file__).resolve().parent


def _load_llmt():
    loader = SourceFileLoader("llmt", str(HERE / "llmt"))
    spec = importlib.util.spec_from_loader("llmt", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


L = _load_llmt()

STATUS_TTL = 60.0
START_TTL = 30.0
CALL_TIMEOUT = 600.0
PLACE_ARGS = ("studies", "goals")
PATH_ARGS = ("request", "bench", "study_folder", "path")
ALL_TOOLS = {"jobs", "list_geometry"}
BROADCAST_TOOLS = {"add_geometry"}
READ_ONLY = {"get_file", "get_image"}
PATHS_FILE = L.CONFIG_DIR / "router_paths.json"


def log(msg: str) -> None:
    print(f"[router] {msg}", file=sys.stderr, flush=True)


def ssh_base(host: dict) -> list:
    """ssh with a shared connection per host (ControlMaster), for the short status and queue calls."""
    ctl = Path.home() / ".ssh" / "llmt-%C"
    return [L.ssh_exe(), *L.SSH_OPTS, "-o", "ControlMaster=auto", "-o", f"ControlPath={ctl}",
            "-o", "ControlPersist=600", "-o", "ConnectTimeout=10", host["ssh"]]


def remote_cmd(host: dict, script: str, timeout: float = 120) -> subprocess.CompletedProcess:
    """Runs a shell script on the host (sh -c locally when ssh is empty)."""
    if host.get("ssh"):
        cmd = ssh_base(host) + [script]
    else:
        cmd = ["sh", "-c", script]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


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
        self.tools: list | None = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, init_params: dict) -> None:
        root = self.host["root"].rstrip("/")
        if self.host.get("ssh"):
            cmd = [L.ssh_exe(), *L.SSH_OPTS, "-o", "ConnectTimeout=10", self.host["ssh"], f"{root}/bin/mcp", self.key]
        else:
            cmd = [str(Path(os.path.expanduser(root)) / "bin" / "mcp"), self.key]
        self.err = []
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        params = {"protocolVersion": init_params.get("protocolVersion", "2025-06-18"), "capabilities": {},
                  "clientInfo": {"name": "llmt-router", "version": __version__}}
        r = self.request("initialize", params, timeout=120)
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
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            try:
                msg = q.get(timeout=timeout)
            except queue.Empty:
                raise ChildError(f"{self.host['name']}: no answer to {method} within {timeout:.0f} s")
            if msg is None:
                raise ChildError(f"{self.host['name']}: the server ended{self.tail()}")
            if "error" in msg:
                raise ChildError(f"{self.host['name']}: {msg['error'].get('message')}")
            return msg["result"]
        finally:
            self.waiting.pop(rid, None)

    def close(self) -> None:
        if self.alive():
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()


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
        self.start_cache: dict = {}
        self.init_params: dict = {}
        self.instructions = ""
        self.tools: list = []
        self.paths = self._load_paths()
        self.paths_lock = threading.Lock()

    # ---- machines
    def host(self, name: str) -> dict | None:
        return next((h for h in self.hosts if h["name"] == name), None)

    def ordered(self) -> list:
        pref = [self.host(n) for n in self.prefer if self.host(n)]
        return [h for h in pref + [h for h in self.hosts if h not in pref] if h["name"] not in self.exclude]

    def child(self, name: str) -> Child:
        with self.child_lock:
            c = self.children.get(name)
            if c is None or not c.alive():
                c = Child(self.host(name), self.key)
                c.start(self.init_params)
                self.children[name] = c
            return c

    def status(self, name: str, fresh: bool = False) -> dict:
        """What the machine says about itself (llmt status --json KEY, load), cached STATUS_TTL seconds."""
        hit = self.status_cache.get(name)
        if hit and not fresh and time.time() - hit[0] < STATUS_TTL:
            return hit[1]
        h = self.host(name)
        script = (f"r={root_expr(h['root'])}; \"$r/llmt\" status --json {self.key} 2>/dev/null; echo; echo @@LOAD; "
                  f"cat /proc/loadavg 2>/dev/null || sysctl -n vm.loadavg 2>/dev/null; "
                  f"nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null")
        out = {"name": name, "reachable": False}
        try:
            r = remote_cmd(h, script, timeout=120)
            text, _, load = r.stdout.partition("@@LOAD")
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
                out.update(reachable=True, mode=data.get("mode"), release=(data.get("release") or {}).get("release"),
                           version=d.get("version"), backends_missing=miss, executor=ex,
                           slurm={k: sl.get(k) for k in ("cpus", "cpus_allocated", "time_limit", "state")} if sl else None,
                           queue={"running": sum(1 for j in jobs if j.get("state") == "RUNNING"),
                                  "waiting": sum(1 for j in jobs if j.get("state") != "RUNNING")} if sl else None)
            nums = [x for x in re.split(r"[\s{}]+", load) if x]
            try:
                out["load"] = float(nums[0])
                out["cores"] = int(nums[-1])
            except (ValueError, IndexError):
                pass
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            out["error"] = str(exc)[:300]
        self.status_cache[name] = (time.time(), out)
        return out

    def forget(self, name: str) -> None:
        """Drops what the router knows about a machine's load, so the next placement asks it again."""
        self.status_cache.pop(name, None)
        for k in [k for k in self.start_cache if k[0] == name]:
            self.start_cache.pop(k, None)

    def statuses(self, names: list, fresh: bool = False) -> dict:
        res: dict = {}
        ths = [threading.Thread(target=lambda n=n: res.__setitem__(n, self.status(n, fresh))) for n in names]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        return res

    def candidates(self, sts: dict) -> list:
        return [n for n in (h["name"] for h in self.ordered())
                if sts.get(n, {}).get("reachable") and sts[n].get("version") and not sts[n].get("backends_missing")]

    def expected_start(self, name: str, st: dict, procs: int, minutes: float, mem_mb: float | None) -> tuple:
        """Minutes until a job of this size would start on the machine, and why."""
        ex = st.get("executor") or {}
        if ex.get("kind") == "slurm" and st.get("slurm"):
            kind = (self.t.get("executor") or {}).get("kind", "mpi")
            size = f"-n {procs}" if kind == "mpi" else f"-n 1 -c {procs}"
            mins = max(1, int(math.ceil(minutes or ex.get("time_limit_min") or 60)))
            mem = f" --mem={int(mem_mb)}M" if mem_mb else ""
            ck = (name, procs, mins, mem)
            hit = self.start_cache.get(ck)
            if hit and time.time() - hit[0] < START_TTL:
                return hit[1]
            script = (f'd=$(sbatch --test-only {size} -t {mins}{mem} --wrap=true 2>&1); echo "$d"; '
                      f's=$(echo "$d" | sed -n "s/.*to start at \\([^ ]*\\).*/\\1/p" | head -1); '
                      f'[ -n "$s" ] && echo "@@START $(date -d "$s" +%s 2>/dev/null) $(date +%s)"')
            try:
                r = remote_cmd(self.host(name), script, timeout=60)
                m = re.search(r"@@START (\d+) (\d+)", r.stdout)
                if m:
                    wait = max(0.0, (int(m.group(1)) - int(m.group(2))) / 60.0)
                    sl = st["slurm"]
                    free = (sl.get("cpus") or 0) - (sl.get("cpus_allocated") or 0)
                    why = ("starts now" if wait < 0.5 else f"SLURM expects a start in {wait:.0f} min") + \
                          f" ({free} of {sl.get('cpus')} CPUs free)"
                    res = (wait, why)
                else:
                    res = (None, "sbatch --test-only gave no start time: " + (r.stdout + r.stderr).strip()[-160:])
            except (OSError, subprocess.TimeoutExpired) as exc:
                res = (None, f"sbatch --test-only failed: {exc}")
            self.start_cache[ck] = (time.time(), res)
            if res[0] is not None:
                return res
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
    def call(self, name: str, tool: str, args: dict) -> dict:
        return self.child(name).request("tools/call", {"name": tool, "arguments": args})

    def call_all(self, names: list, tool: str, args: dict) -> dict:
        res: dict = {}

        def one(n):
            try:
                r = self.call(n, tool, args)
                data = self.parse(r)
                self.learn(data, n)
                res[n] = data if data is not None else r
            except ChildError as exc:
                res[n] = {"unreachable": str(exc)}
        ths = [threading.Thread(target=one, args=(n,)) for n in names]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        return res

    def validate_tool(self) -> str | None:
        names = {t["name"] for t in self.tools}
        return next((n for n in ("validate_studies", "validate_goals") if n in names), None)

    def place(self, tool: str, args: dict) -> tuple:
        """-> (machine name, args to send, router info) for a call that starts work."""
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
        sts = self.statuses([h["name"] for h in self.ordered()])
        cands = self.candidates(sts)
        unreach = [n for n, s in sts.items() if not s.get("reachable")]
        if unreach:
            warnings.append(f"unreachable: {', '.join(unreach)}")
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
        aff = self.affinity(studies) if arg else None
        if aff and not manual:
            if aff in cands:
                cands = [aff]
                warnings.append(f"kept on {aff}: the studies refer to a request there")
            else:
                warnings.append(f"the studies refer to a request on {aff}, which cannot take it now")
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
        if vt and studies:
            vres = self.call_all(cands, vt, {arg: studies})
            ok = []
            for n in cands:
                v = vres.get(n)
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
        rows = []
        for i, n in enumerate(cands):
            st = sts[n]
            ex = st.get("executor") or {}
            e = est.get(n, {})
            procs = int(e.get("procs") or ex.get("procs") or 1)
            minutes = e.get("minutes")
            start, why = self.expected_start(n, st, procs, minutes or 0, e.get("mem_mb"))
            start = start if start is not None else 0.0
            run_min = (minutes or 0) * float(self.speed.get(n, 1.0))
            small = minutes is not None and minutes < self.small
            score = start + (0 if small else run_min) - (0.5 * (len(self.prefer) - self.prefer.index(n))
                                                          if n in self.prefer else 0)
            rows.append({"machine": n, "start_min": round(start, 1), "run_min": round(run_min, 1) if minutes else None,
                         "score": round(score, 2), "why": why + (f", estimate {minutes:.0f} min" if minutes else ""),
                         "version": st.get("version")})
        rows.sort(key=lambda r: (r["score"], cands.index(r["machine"])))
        best = rows[0]
        info = {"chose": best["machine"], "why": best["why"], "candidates": rows}
        if manual:
            info["why"] = f"chosen in run.host; {best['why']}"
        if warnings:
            info["warnings"] = warnings
        return best["machine"], send, info

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
                r = remote_cmd(h, f"test -e {shlex.quote(path)} && echo yes", timeout=30)
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
        if tool == "machines":
            return self.text_result(self.machines())
        try:
            if tool in ALL_TOOLS or tool in BROADCAST_TOOLS:
                sts = self.statuses([h["name"] for h in self.ordered()])
                names = [n for n in self.candidates(sts)] or [n for n, s in sts.items() if s.get("reachable")]
                return self.text_result({"machines": self.call_all(names, tool, args)})
            starts_work = tool == "run_benchmarks" or (not tool.startswith(("validate_", "get_")) and
                                                        any(isinstance(args.get(a), dict) for a in PLACE_ARGS))
            if starts_work:
                name, send, info = self.place(tool, args)
                r = self.call(name, tool, send)
                self.forget(name)                     # it has new work: the next placement asks it again
                self.learn(self.parse(r), name)
                return self.with_fields(r, {"machine": name, "router": info})
            name, send = self.route_path(args)
            if name:
                r = self.call(name, tool, send)
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
                r = self.call(n, tool, send)
                self.learn(self.parse(r), n)
                return self.with_fields(r, {"machine": n})
            name = self.default_machine()
            r = self.call(name, tool, args)
            self.learn(self.parse(r), name)
            return self.with_fields(r, {"machine": name})
        except (ValueError, ChildError) as exc:
            return self.text_result({"error": str(exc), "router": "llmt-router (machines() shows the machines)"}, True)

    def machines(self) -> dict:
        sts = self.statuses([h["name"] for h in self.hosts], fresh=True)
        out = []
        for h in self.hosts:
            st = sts.get(h["name"], {})
            row = {"machine": h["name"], "ssh": h.get("ssh") or "(this machine)", "root": h["root"],
                   "preferred": h["name"] in self.prefer, "excluded": h["name"] in self.exclude,
                   "speed": self.speed.get(h["name"], 1.0)}
            row.update({k: v for k, v in st.items() if k not in ("name", "executor")})
            ex = st.get("executor") or {}
            if ex:
                row["executor"] = {k: ex.get(k) for k in ("kind", "procs", "time_limit_min", "caps") if k in ex}
            if st.get("reachable") and st.get("version"):
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
        for h in self.ordered() or self.hosts:
            try:
                c = self.child(h["name"])
                self.instructions = (c.init_result or {}).get("instructions") or ""
                self.tools = c.request("tools/list", {}).get("tools", [])
                proto = (c.init_result or {}).get("protocolVersion") or params.get("protocolVersion")
                break
            except ChildError as exc:
                errors.append(str(exc))
        else:
            proto = params.get("protocolVersion", "2025-06-18")
            self.instructions = (f"llm-translators router for {self.key}: no machine answered ({'; '.join(errors)}). "
                                 f"Call machines() to see why.")
        names = ", ".join(h["name"] for h in self.hosts)
        note = (f"\nMachines: this server runs {self.key} on several machines ({names}). Calls that start work are "
                f"placed on the machine where they should finish first; answers say which ('machine', 'router'). "
                f"Paths in answers work with every tool here. A study's \"run\": {{\"host\": name or [names]}} "
                f"chooses the machine; machines() shows each one's load, queue and version.")
        return {"protocolVersion": proto, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": f"{self.key} (llmt-router)", "version": __version__},
                "instructions": self.instructions + note}

    def tools_list(self) -> dict:
        own = {"name": "machines",
               "description": "The machines this server can run on, as the router sees them: reachable, version, "
                              "backends, executor (local or SLURM), CPUs and their use, this translator's queued and "
                              "running jobs, load, the expected start of a typical job, speed, preferred or excluded.",
               "inputSchema": {"type": "object", "properties": {}}}
        return {"tools": [t for t in self.tools if t.get("name") != "machines"] + [own]}

    def close(self) -> None:
        for c in self.children.values():
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
                res = router.tool_call(params.get("name", ""), params.get("arguments") or {})
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
