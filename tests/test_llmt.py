"""Tests for llm-translators' own code: python3 -m unittest discover -s tests (standard library only).

    - the router notices a server process that runs another version than the installed one (serving,
      server_stale) and replaces it once idle;
    - llmt servers: the process list, with a symlinked prefix, a shell mentioning a server path, a SLURM job;
    - llmt deploy's pin check: a pin behind its branch's newest commit;
    - llmt status: an editable install behind its checkout (version, missing dependencies) and the fix it names;
    - the router copies local input files to the machine (fake ssh, two machines): nested arguments, the copy's
      place, no second copy, a link for the same content, the size limit, a shared path, a slow copy, resuming.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load(name: str, file: str):
    loader = SourceFileLoader(name, str(REPO / file))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(m)
    return m


L = load("llmt", "llmt")
R = load("llmt_router", "llmt_router.py")

FAKE_SERVER = textwrap.dedent('''\
    #!/usr/bin/env python3
    # A translator's MCP server, reduced to what the router needs: it reports the version installed when it started.
    import json, sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[1]
    VERSION = (ROOT / "INSTALLED").read_text().strip()
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        meth = m["method"]
        if meth == "initialize":
            r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                 "serverInfo": {"name": "fake", "version": "x"}, "instructions": "fake"}
        elif meth == "tools/list":
            r = {"tools": [{"name": n, "description": n + " " + VERSION, "inputSchema": {"type": "object", "properties": {}}}
                           for n in ("jobs", "study_status")]}
        elif meth == "tools/call":
            r = {"content": [{"type": "text", "text": json.dumps({"translator": VERSION, "requests": []})}]}
        else:
            r = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}) + "\\n")
        sys.stdout.flush()
''')

FAKE_LLMT = textwrap.dedent('''\
    #!/usr/bin/env python3
    # 'llmt status --json KEY' of a machine: the installed version is in INSTALLED.
    import json
    from pathlib import Path
    v = (Path(__file__).resolve().parent / "INSTALLED").read_text().strip()
    print(json.dumps({"mode": "release", "release": {"release": "rel-" + v},
                      "translators": {"lmpt": {"version": v, "backends": {}, "status": {"executor": {"kind": "local"}}}}}))
''')


class RouterServing(unittest.TestCase):
    """Item 1-2: machines() shows the version the server in use runs; a server on an old release is replaced."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "machine"
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "mcp").write_text(FAKE_SERVER)
        (root / "llmt").write_text(FAKE_LLMT)
        os.chmod(root / "bin" / "mcp", 0o755)
        os.chmod(root / "llmt", 0o755)
        (root / "INSTALLED").write_text("2026.10.22.10\n")
        self.root = root
        r = R.Router("lmpt")
        r.hosts = [{"name": "m", "ssh": "", "root": str(root)}]
        r.prefer, r.exclude = [], set()
        r.initialize({"protocolVersion": "2025-06-18"})
        self.r = r

    def tearDown(self):
        self.r.close()
        self.tmp.cleanup()

    def row(self) -> dict:
        return self.r.machines()["machines"][0]

    def wait_serving(self, c, timeout=10):
        end = time.time() + timeout
        while c.serving is None and time.time() < end:
            time.sleep(0.05)

    def test_serving_and_replacement(self):
        r = self.r
        c1 = r.child("m")
        self.wait_serving(c1)
        row = self.row()
        self.assertEqual(row["version"], "2026.10.22.10")
        self.assertEqual(row["serving"], "2026.10.22.10")
        self.assertNotIn("server_stale", row)

        # a deploy installs a newer version; the server in use is busy (a call in flight)
        (self.root / "INSTALLED").write_text("2026.10.25.6\n")
        c1.inflight[10 ** 6] = time.time()
        row = self.row()
        self.assertEqual(row["version"], "2026.10.25.6")
        self.assertIn("2026.10.22.10", json.dumps(row["serving"]))
        self.assertTrue(row["server_stale"])
        self.assertIn("replaced once idle", row["note"])
        self.assertTrue(c1.retire and c1.alive())

        # once idle, the next call closes it and a new server runs the installed version
        c1.inflight.clear()
        out = R.Router.parse(r.call("m", "study_status", {"request": "/x"}))
        self.assertEqual(out["translator"], "2026.10.25.6")
        self.assertFalse(c1.alive())
        row = self.row()
        self.assertEqual(row["serving"], "2026.10.25.6")
        self.assertNotIn("server_stale", row)

    def test_idle_old_server_closed_at_status(self):
        r = self.r
        c1 = r.child("m")
        self.wait_serving(c1)
        (self.root / "INSTALLED").write_text("2026.10.25.6\n")
        r.fetch_status("m")                       # the status is newer than the server, which is idle
        end = time.time() + 5
        while c1.alive() and time.time() < end:  # closed now, or as soon as a call in flight (a probe) ends
            time.sleep(0.05)
        st = r.status_cache["m"][1]
        self.assertFalse(c1.alive(), f"retire={c1.retire} inflight={c1.inflight} serving={c1.serving} "
                                     f"installed={st.get('version')} asked-started={st.get('_asked', 0) - c1.started:.3f}")


    def test_tools_follow_version(self):
        r = self.r
        self.assertEqual(r.tools_version, "2026.10.22.10")
        self.assertIn("2026.10.22.10", r.tools[0]["description"])
        told = []
        r.notify = lambda: told.append(1)
        (self.root / "INSTALLED").write_text("2026.10.25.6\n")       # a deploy: the definitions may have changed
        r.fetch_status("m")
        end = time.time() + 15
        while r.tools_version != "2026.10.25.6" and time.time() < end:
            time.sleep(0.1)
        self.assertEqual(r.tools_version, "2026.10.25.6")
        self.assertIn("2026.10.25.6", r.tools[0]["description"])
        self.assertEqual(told, [1])
        r.fetch_status("m")                                          # same version again: nothing to do
        time.sleep(0.5)
        self.assertEqual(told, [1])


class NewestTools(unittest.TestCase):
    """initialize: the tool definitions come from the machine with the newest version, whatever the order."""

    def test_newest(self):
        with tempfile.TemporaryDirectory() as t:
            hosts = []
            for name, v in (("old", "2026.10.22.10"), ("new", "2026.10.25.6")):
                root = Path(t) / name
                (root / "bin").mkdir(parents=True)
                (root / "bin" / "mcp").write_text(FAKE_SERVER)
                (root / "llmt").write_text(FAKE_LLMT)
                os.chmod(root / "bin" / "mcp", 0o755)
                (root / "INSTALLED").write_text(v + "\n")
                hosts.append({"name": name, "ssh": "", "root": str(root)})
            r = R.Router("lmpt")
            r.hosts, r.prefer, r.exclude = hosts, [], set()
            try:
                res = r.initialize({"protocolVersion": "2025-06-18"})
                self.assertTrue(res["capabilities"]["tools"]["listChanged"])
                self.assertEqual(r.tools_version, "2026.10.25.6")
                self.assertIn("2026.10.25.6", r.tools[0]["description"])
            finally:
                r.close()

    def test_vkey(self):
        self.assertEqual(R.vkey("2026.11.02.1"), R.vkey("2026.11.2.1"))
        self.assertLess(R.vkey("2026.11.9.1"), R.vkey("2026.11.10.1"))


FAKE_NODE_LLMT = textwrap.dedent('''\
    #!/usr/bin/env python3
    # spark1 of a two-node cluster: its translator jobs are pinned to spark-5da5; spark2's run on spark-0808
    import json
    jobs = [{"key": "cfdt", "state": "RUNNING", "reason": "spark-0808"},                    # older llmt: no 'nodes'
            {"key": "cfdt", "state": "RUNNING", "nodes": "spark-5da5", "reason": "spark-5da5"},
            {"key": "cfdt", "state": "PENDING", "req_nodes": "spark-5da5", "reason": "Resources"},
            {"key": "cfdt", "state": "PENDING", "req_nodes": "spark-0808", "reason": "Resources"},
            {"key": "lmpt", "state": "RUNNING", "nodes": "spark-5da5"}]
    print(json.dumps({"mode": "release", "release": {"release": "r1"},
                      "slurm": {"cpus": 20, "cpus_allocated": 10, "node": "spark-5da5", "cluster_nodes": 2,
                                "nodes_usable": 1, "jobs": jobs},
                      "translators": {"cfdt": {"version": "1", "backends": {}, "status": {"executor": {
                          "kind": "slurm", "procs": 10, "node": {"setting": "local", "nodelist": "spark-5da5"}}}}}}))
''')

FAKE_SLURM = {
    "sinfo": '#!/bin/sh\necho "$FAKE_C"\n',
    "squeue": '#!/bin/sh\n[ -n "$FAKE_PD" ] && echo "$FAKE_PD"\nexit 0\n',
    "sbatch": '#!/bin/sh\ntouch "$FAKE_MARK"\n'
              'echo "sbatch: Job 3 to start at $(date -d \'+90 min\' +%Y-%m-%dT%H:%M:%S) using 10 processors"\n',
}


class NodeQueue(unittest.TestCase):
    """A machine that is one node of a cluster: its own queue, and 'starts now' when its node is idle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "root").mkdir()
        (t / "root" / "llmt").write_text(FAKE_NODE_LLMT)
        (t / "bin").mkdir()
        for n, text in FAKE_SLURM.items():
            (t / "bin" / n).write_text(text)
            os.chmod(t / "bin" / n, 0o755)
        self.t = t
        self.saved = {k: os.environ.get(k) for k in ("PATH", "FAKE_C", "FAKE_PD", "FAKE_MARK")}
        os.environ.update(PATH=f"{t / 'bin'}:{os.environ['PATH']}", FAKE_MARK=str(t / "sbatch-ran"))
        self.r = R.Router("cfdt")
        self.r.hosts = [{"name": "spark1", "ssh": "", "root": str(t / "root")}]

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_queue_per_node(self):
        st = self.r.fetch_status("spark1")
        self.assertEqual(st["queue"], {"running": 1, "waiting": 1})                 # spark-5da5's cfdt jobs
        self.assertEqual(st["cluster_queue"], {"running": 2, "waiting": 2})
        self.assertEqual(st["pending_for_node"], 1)
        self.assertTrue(st["out_dir"].endswith("/cfdt_runs"))

    def start(self, c, pd=""):
        os.environ.update(FAKE_C=c, FAKE_PD=pd)
        (self.t / "sbatch-ran").unlink(missing_ok=True)
        self.r.start_cache.clear()
        st = {"executor": {"kind": "slurm", "node": {"setting": "local", "nodelist": "spark-5da5"}},
              "slurm": {"cpus": 20, "cpus_allocated": 0, "cluster_nodes": 2, "node": "spark-5da5", "nodes_usable": 1}}
        return self.r.expected_start("spark1", st, 10, 10, None), (self.t / "sbatch-ran").exists()

    def test_starts_now(self):
        (start, why), ran = self.start("0/20/0/20")
        self.assertEqual(start, 0.0)
        self.assertIn("all 20 CPUs idle on node spark-5da5", why)
        self.assertFalse(ran)                                       # no sbatch --test-only
        (start, why), ran = self.start("18/2/0/20")                # busy: SLURM's estimate
        self.assertTrue(ran)
        self.assertAlmostEqual(start, 90, delta=2)
        (start, why), ran = self.start("0/20/0/20", pd="spark-5da5")   # idle, but a job waits for the node
        self.assertTrue(ran)


FAKE_SLOW_SERVER = textwrap.dedent('''\
    #!/usr/bin/env python3
    # cfdt with request_name: run_studies takes longer than the router's time; it records what it received
    import json, os, sys, time
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[1]
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        meth, p = m["method"], m.get("params") or {}
        if meth == "initialize":
            r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                 "serverInfo": {"name": "fake", "version": "x"}, "instructions": "fake"}
        elif meth == "tools/list":
            obj = {"type": "object", "properties": {"studies": {"type": "object"}}}
            run = {"type": "object", "properties": {"studies": {"type": "object"}, "request_name": {"type": "string"},
                                                    "wait_s": {"type": "number", "default": 45}}}
            r = {"tools": [{"name": "jobs", "description": "", "inputSchema": {"type": "object", "properties": {}}},
                           {"name": "validate_studies", "description": "", "inputSchema": obj},
                           {"name": "run_studies", "description": "", "inputSchema": run}]}
        elif meth == "tools/call":
            name, args = p["name"], p.get("arguments") or {}
            if name == "run_studies":
                (ROOT / "received.json").write_text(json.dumps(args))
                time.sleep(8)
                out = {"translator": "1", "request": "x"}
            elif name == "validate_studies":
                out = {"studies": {k: {"ok": True} for k in args.get("studies", {})}}
            else:
                out = {"translator": "1", "requests": []}
            r = {"content": [{"type": "text", "text": json.dumps(out)}]}
        else:
            r = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}) + "\\n")
        sys.stdout.flush()
''')


class RequestName(unittest.TestCase):
    """run_studies gets a request name; when the machine does not answer in time the answer says where to look."""

    def test_submitted_unconfirmed(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "m"
            (root / "bin").mkdir(parents=True)
            (root / "bin" / "mcp").write_text(FAKE_SLOW_SERVER)
            os.chmod(root / "bin" / "mcp", 0o755)
            (root / "llmt").write_text(FAKE_STAGE_LLMT)
            saved = (R.BUDGET, R.PATHS_FILE)
            R.BUDGET, R.PATHS_FILE = 8.0, Path(t) / "paths.json"
            r = R.Router("cfdt")
            r.hosts, r.prefer, r.exclude, r.paths = [{"name": "m", "ssh": "", "root": str(root)}], [], set(), {}
            try:
                r.initialize({"protocolVersion": "2025-06-18"})
                out = R.Router.parse(r.tool_call("run_studies", {"studies": {"s": {}}}))
                self.assertEqual(out["status"], "submitted_unconfirmed", out)
                self.assertRegex(out["request_name"], r"^run_\d{8}_\d{6}_[0-9a-f]{6}$")
                self.assertTrue(out["request"].endswith("/cfdt_runs/" + out["request_name"]), out["request"])
                self.assertEqual(r.owner(out["request"]), "m")              # study_status goes there
                self.assertIn("do not submit again", out["next"])
                got = json.loads((root / "received.json").read_text())
                self.assertEqual(got["request_name"], out["request_name"])
            finally:
                R.BUDGET, R.PATHS_FILE = saved
                r.close()


class MemoryPlacement(unittest.TestCase):
    """run.mem against each machine's largest job memory (executor.caps), before validating or copying anything."""

    def router(self):
        r = R.Router("cfdt")
        r.hosts = [{"name": n, "ssh": f"u@{n}", "root": "/x"} for n in ("mochi", "spark1", "spark2", "carbonio")]
        r.prefer, r.exclude, r.speed, r.tools = [], set(), {}, []

        def st(caps):
            return {"reachable": True, "version": "1", "backends_missing": [], "load": 0.0, "cores": 20,
                    "executor": {"kind": "local", "procs": 2, "caps": caps}}
        sts = {"mochi": st({"mem": "62G", "mem_mb": 63488}), "spark1": st({"mem": "117G"}),
               "spark2": st({"mem_mb": 120000}), "carbonio": st({})}            # carbonio: memory unknown
        r.statuses = lambda names, fresh=False, wait=0: {n: sts[n] for n in names}
        r.validate_tool = lambda: None
        r.local_files = lambda args: (_ for _ in ()).throw(AssertionError("files looked at"))  # must not get here
        return r

    def test_parse(self):
        self.assertEqual(R.parse_mem_mb("90G"), 92160)
        self.assertEqual(R.parse_mem_mb("2048"), 2048)
        self.assertEqual(R.parse_mem_mb(4000), 4000)
        self.assertEqual(R.parse_mem_mb("512K"), 0.5)
        self.assertEqual(R.parse_mem_mb("1T"), 1048576)
        self.assertEqual(R.parse_mem_mb("16GB"), 16384)
        for v in ("auto", None, "", "lots", True, 0):
            self.assertIsNone(R.parse_mem_mb(v))
        self.assertEqual(R.fmt_mb(63488), "62G")

    def test_left_out(self):
        r = self.router()
        r.local_files = lambda args: {}
        name, send, info = r.place("run_studies", {"studies": {
            "a": {"run": {"mem": "8G"}},
            "b": {"run": {"mem": "90G", "host": ["mochi", "spark1", "spark2"]}},
            "c": {"run": {"host": ["mochi", "spark1", "spark2"]}}}})
        self.assertIn(name, ("spark1", "spark2"))
        self.assertIn("mochi left out: run.mem 90G > its 62G", info["warnings"])
        self.assertNotIn("mochi", [c["machine"] for c in info["candidates"]])
        self.assertEqual(send["studies"]["b"]["run"], {"mem": "90G"})          # run.host removed, mem kept
        # no host chosen: carbonio, whose limit is unknown, stays a candidate
        name, send, info = r.place("run_studies", {"studies": {"b": {"run": {"mem": "200G"}}}})
        self.assertEqual(name, "carbonio")
        self.assertEqual(sorted(w.split()[0] for w in info["warnings"]), ["mochi", "spark1", "spark2"])

    def test_none_left_errors_before_files(self):
        r = self.router()
        with self.assertRaises(ValueError) as cm:
            r.place("run_studies", {"studies": {"b": {"run": {"mem": "90G", "host": ["mochi"]}}}})
        self.assertIn("no machine can give run.mem 90G: mochi 62G", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            r.place("run_studies", {"studies": {"b": {"run": {"mem": "1T", "host": ["mochi", "spark1", "spark2"]}}}})
        self.assertIn("mochi 62G, spark1 117G, spark2 117.2G", str(cm.exception))
        # 'auto' or no run.mem: no filter
        r.local_files = lambda args: {}
        self.assertEqual(r.place("run_studies", {"studies": {"b": {"run": {"mem": "auto", "host": "mochi"}}}})[0],
                         "mochi")


class ServersList(unittest.TestCase):
    """Item 3: the process list; a symlinked prefix; no match for shells or jobs; own process skipped."""

    def test_scan(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            real = t / "data" / "llm-translators"
            for rel, key in (("2026.10.25.1", "cfdt"), ("2026.10.25.3", "lmpt")):
                b = real / "releases" / rel / ".venv" / "bin"
                b.mkdir(parents=True)
                (b / f"{key}-mcp").write_text("#!python\n")
                (b / "python").write_text("")
            (real / "current").symlink_to(Path("releases") / "2026.10.25.3")
            prefix = t / "opt-llm-translators"
            prefix.symlink_to(real)                                  # the prefix itself is a symlink
            home = t / "home" / "Software" / "llm-translators" / ".venv" / "bin"
            home.mkdir(parents=True)
            (home / "feat-mcp").write_text("")
            proc = t / "proc"
            uid = os.getuid()

            def p(pid, *argv):
                d = proc / str(pid)
                d.mkdir(parents=True)
                (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
                (d / "status").write_text(f"Name:\tx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
            old = f"{prefix}/releases/2026.10.25.1/.venv/bin"                  # through the symlinked prefix
            cur = f"{real}/releases/2026.10.25.3/.venv/bin"
            p(101, f"{old}/python", f"{old}/cfdt-mcp")
            p(102, f"{cur}/python", f"{cur}/lmpt-mcp")
            p(103, "bash", "-c", f"llmt servers --stop; {old}/cfdt-mcp")     # a shell mentioning the path
            p(104, f"{cur}/python", "-m", "cfd_translator.cli", "run-study", "/r", "s")   # a SLURM job
            p(105, f"{old}/python", f"{old}/cfdt-mcp")                       # 'own' process: skipped
            p(106, f"{home}/python", f"{home}/feat-mcp")                     # a personal checkout
            (proc / "self").mkdir()
            found = {e["pid"]: e for e in L.scan_servers(prefix, proc, skip={105})}
            self.assertEqual(sorted(found), [101, 102, 106])
            self.assertEqual((found[101]["release"], found[101]["old"], found[101]["key"]), ("2026.10.25.1", True, "cfdt"))
            self.assertEqual((found[102]["release"], found[102]["current"], found[102]["old"]), ("2026.10.25.3", True, False))
            self.assertEqual((found[106]["release"], found[106]["old"]), (None, False))


class Orphans(unittest.TestCase):
    """llmt servers / gc: multiprocessing helpers left behind by a server that ended (parent pid 1)."""

    def test_scan(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            prefix = t / "opt"
            for rel in ("2026.11.01.1", "2026.11.03.1"):
                b = prefix / "releases" / rel / ".venv" / "bin"
                b.mkdir(parents=True)
                (b / "python").write_text("")
            (prefix / "current").symlink_to(Path("releases") / "2026.11.03.1")
            proc = t / "proc"
            uid = os.getuid()

            def p(pid, ppid, *argv):
                d = proc / str(pid)
                d.mkdir(parents=True)
                (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
                (d / "status").write_text(f"Name:\tx\nPPid:\t{ppid}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
            old = f"{prefix}/releases/2026.11.01.1/.venv/bin"
            cur = f"{prefix}/current/.venv/bin"                       # through the 'current' symlink
            tracker = "from multiprocessing.resource_tracker import main;main(13)"
            spawn = "from multiprocessing.spawn import spawn_main; spawn_main(tracker_fd=14, pipe_handle=16)"
            p(503598, 1, f"{old}/python", "-c", tracker)               # leftovers of an ended server
            p(503599, 1, f"{old}/python", "-c", spawn, "--multiprocessing-fork")
            p(700, 1, f"{cur}/python", f"{cur}/feat-mcp")              # a live server ...
            p(701, 700, f"{cur}/python", "-c", tracker)                # ... and its helpers: left alone
            p(702, 700, f"{cur}/python", "-c", spawn, "--multiprocessing-fork")
            p(800, 1, "/usr/bin/python3", "-c", tracker)               # not a translator venv
            p(801, 1, "bash", "-c", "from multiprocessing x")
            found = {e["pid"]: e for e in L.scan_orphans(prefix, proc)}
            self.assertEqual(sorted(found), [503598, 503599])
            self.assertEqual((found[503598]["kind"], found[503598]["release"], found[503598]["current"]),
                             ("resource tracker", "2026.11.01.1", False))
            self.assertEqual(found[503599]["kind"], "worker")


class PinCheck(unittest.TestCase):
    """Item 4: a pin behind the newest commit of its branch."""

    def git(self, cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

    def test_behind(self):
        with tempfile.TemporaryDirectory() as t:
            up = Path(t) / "up"
            (up / "pkg").mkdir(parents=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(up)], check=True)
            for v in ("2026.10.22.10", "2026.10.25.6"):
                (up / "pkg" / "__init__.py").write_text(f'__version__ = "{v}"\n')
                self.git(up, "add", "-A")
                self.git(up, "commit", "-q", "-m", v)
            pin = Path(t) / "pin"
            subprocess.run(["git", "clone", "-q", str(up), str(pin)], check=True)
            self.git(pin, "checkout", "-q", "HEAD~1")                       # the umbrella pinned the older commit
            rep = L.pin_report(pin, str(up), "main", "pkg")
            self.assertEqual((rep["behind"], rep["ahead"]), (1, 0))
            self.assertEqual((rep["pinned_version"], rep["newest_version"]), ("2026.10.22.10", "2026.10.25.6"))

    def test_warning_when_behind(self):
        saved = (L.pin_report, L.submodule_url, L.submodule_branch, L.git)
        try:
            L.pin_report = lambda *a: {"pinned": "aaaaaaa", "pinned_version": "2026.10.22.10", "newest": "bbbbbbb",
                                       "newest_version": "2026.10.25.6", "behind": 3, "ahead": 0}
            L.submodule_url = lambda repo: ""
            L.submodule_branch = lambda repo: "main"
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                L.check_pins(["cfdt"], interactive=False)                    # warns, does not stop
            self.assertIn("BEHIND by 3", out.getvalue())
            self.assertIn("llmt pin cfdt --commit --push", err.getvalue())
        finally:
            L.pin_report, L.submodule_url, L.submodule_branch, L.git = saved


class Reinstall(unittest.TestCase):
    """llmt status: an editable install whose checkout moved on (new version, new dependencies) without a reinstall."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        site, src = t / "site", t / "src"
        (site / "pkgx").mkdir(parents=True)
        src.mkdir()
        # the code (the checkout) says 2026.10.28.1 and needs a new package; the installed metadata is older
        (site / "pkgx" / "__init__.py").write_text('__version__ = "2026.10.28.1"\n')
        di = site / "fake_translator-2026.10.21.1.dist-info"
        di.mkdir()
        (di / "METADATA").write_text("Metadata-Version: 2.1\nName: fake-translator\nVersion: 2026.10.21.1\n"
                                     "Requires-Dist: pip\nRequires-Dist: old-gone-pkg-xyz; extra == \"unused\"\n"
                                     "Requires-Dist: mcp-dep-xyz>=1; extra == \"mcp\"\n")
        (src / "pyproject.toml").write_text(textwrap.dedent('''\
            [project]
            name = "fake-translator"
            version = "2026.10.28.1"
            dependencies = ["pip", "matplotlib-new-xyz>=3.7", "winonly-xyz; sys_platform == 'win32'"]
            [project.optional-dependencies]
            mcp = ["mcp-dep-xyz>=1"]
            test = ["pytest-xyz"]
            '''))
        self.site, self.src = site, src

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, extras):
        r = subprocess.run([sys.executable, "-c", L._VENV_REPORT,
                            json.dumps({"x": ["pkgx", "fake-translator", extras, str(self.src)]})],
                           capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(self.site)})
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["x"]

    def test_report(self):
        rep = self.report(["mcp"])
        self.assertEqual((rep["version"], rep["dist_version"], rep["stale"]), ("2026.10.28.1", "2026.10.21.1", True))
        self.assertEqual(sorted(rep["missing"]), ["matplotlib-new-xyz>=3.7", "mcp-dep-xyz>=1"])
        self.assertNotIn("pytest-xyz", rep["missing"])                 # the test extra was not chosen
        rep = self.report([])
        self.assertEqual(rep["missing"], ["matplotlib-new-xyz>=3.7"])

    def test_hint_and_status_line(self):
        rep = {"version": "2026.10.28.1", "dist_version": "2026.10.21.1", "stale": True, "missing": ["matplotlib>=3.7"]}
        bad = L.reinstall_needed("b3dt", rep, "pinned")
        self.assertEqual(bad["problem"], "installed as 2026.10.21.1, code 2026.10.28.1; missing matplotlib>=3.7")
        self.assertEqual(bad["fix"], "llmt update b3dt --no-pull")
        self.assertEqual(L.reinstall_needed("b3dt", rep, "moved")["fix"], "llmt install b3dt")   # keeps the checkout
        self.assertEqual(L.reinstall_needed("b3dt", {"version": "2026.11.2.9", "dist_version": "2026.11.02.9",
                                                     "stale": False, "missing": []}, "pinned"), {})
        data = {"llmt": "x", "root": "/r", "commit": "abc", "venv": "/r/.venv", "python": "3.12", "settings": [],
                "translators": {"b3dt": {"version": "2026.10.28.1", "commit": "abc1234", "submodule": "pinned",
                                         "backends": {}, **bad}}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            L.print_status(data, False)
        self.assertIn("b3dt: installed as 2026.10.21.1, code 2026.10.28.1; missing matplotlib>=3.7: "
                      "run llmt update b3dt --no-pull", out.getvalue())

    def test_mcp_test_entry(self):
        saved = (L.venv_report, L.submodule_states)
        try:
            L.venv_report = lambda *a: {"b3dt": {"version": "2026.10.28.1", "dist_version": "2026.10.21.1",
                                                 "stale": True, "missing": []}}
            L.submodule_states = lambda: {"build123d-translator": ("pinned", "abc1234")}
            bad = L.entry_health("b3dt", {"command": str(L.ROOT / "bin" / "mcp"), "args": ["b3dt"]})
            self.assertEqual(bad["fix"], "llmt update b3dt --no-pull")
            self.assertIsNone(L.entry_health("b3dt", {"command": str(L.ROOT / "bin" / "router"), "args": ["b3dt"]}))
        finally:
            L.venv_report, L.submodule_states = saved


FAKE_SSH = textwrap.dedent('''\
    #!/usr/bin/env python3
    # ssh [options] HOST COMMAND...: runs COMMAND here with HOME=$FAKE_REMOTES/HOST. Paths under $FAKE_HIDE (the
    # computer the router runs on) are made invisible to the command, as on a real other machine. Logs each command.
    import json, os, sys, time
    a = sys.argv[1:]
    i = 0
    while i < len(a) and a[i].startswith("-"):
        i += 2 if a[i] in ("-o", "-p", "-i", "-l") else 1
    host, cmd = a[i].split("@")[-1], " ".join(a[i + 1:])
    if os.environ.get("FAKE_HIDE"):
        cmd = cmd.replace(os.environ["FAKE_HIDE"], "/hidden-local")
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps({"host": host, "cmd": cmd}) + "\\n")
    if cmd.startswith("cat >"):
        time.sleep(float(os.environ.get("FAKE_SLOW") or 0))
    home = os.path.join(os.environ["FAKE_REMOTES"], host)
    os.execvpe("sh", ["sh", "-c", cmd], dict(os.environ, HOME=home, LLMT_SYSTEM_ENV=os.path.join(home, "sysenv")))
''')

FAKE_STAGE_SERVER = textwrap.dedent('''\
    #!/usr/bin/env python3
    # A translator server that reports which of the paths in a study exist where it runs, with their sha256.
    import hashlib, json, os, sys

    def paths(o):
        if isinstance(o, dict):
            o = list(o.values())
        if isinstance(o, list):
            return [p for v in o for p in paths(v)]
        return [o] if isinstance(o, str) and o.startswith("/") else []

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.isfile(p) else None

    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        meth, p = m["method"], m.get("params") or {}
        if meth == "initialize":
            r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                 "serverInfo": {"name": "fake", "version": "x"}, "instructions": "fake"}
        elif meth == "tools/list":
            obj = {"type": "object", "properties": {"studies": {"type": "object"}}}
            r = {"tools": [{"name": "jobs", "description": "", "inputSchema": {"type": "object", "properties": {}}},
                           {"name": "validate_studies", "description": "", "inputSchema": obj},
                           {"name": "run_studies", "description": "", "inputSchema": {
                               "type": "object", "properties": {"studies": {"type": "object"},
                                                                "wait_s": {"type": "number", "default": 45}}}}]}
        elif meth == "tools/call":
            name, args = p["name"], p.get("arguments") or {}
            st = args.get("studies") or {}
            if name == "validate_studies":
                out = {"studies": {k: {"ok": all(os.path.isfile(x) for x in paths(s)),
                                       "estimate": {"minutes": 1, "procs": 1}} for k, s in st.items()}}
            elif name == "run_studies":
                out = {"translator": "1", "request": os.path.expanduser("~/out/run1"),
                       "seen": {x: sha(x) for x in paths(st)}}
            elif name == "add_geometry":
                out = {"translator": "1", "seen": {x: sha(x) for x in paths(args)}}
            else:
                out = {"translator": "1", "requests": []}
            r = {"content": [{"type": "text", "text": json.dumps(out)}]}
        else:
            r = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}) + "\\n")
        sys.stdout.flush()
''')

FAKE_STAGE_LLMT = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json
    print(json.dumps({"mode": "checkout", "translators": {"cfdt": {"version": "1", "backends": {},
                      "status": {"executor": {"kind": "local", "procs": 1}}}}}))
''')


class Staging(unittest.TestCase):
    """Router: files on the router's computer named in a call are copied to the machine that runs it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.t = t
        self.mac = t / "mac"                                   # the router's computer: its home folder
        (self.mac / "parts").mkdir(parents=True)
        self.data = os.urandom(300_000)
        (self.mac / "parts" / "a b.step").write_bytes(self.data)
        (self.mac / "parts" / "copy.stp").write_bytes(self.data)
        self.sha = hashlib.sha256(self.data).hexdigest()
        root = t / "root"                                      # the install on the machines
        (root / "bin").mkdir(parents=True)
        for path, text in ((root / "bin" / "mcp", FAKE_STAGE_SERVER), (root / "llmt", FAKE_STAGE_LLMT),
                           (t / "ssh", FAKE_SSH)):
            path.write_text(text)
            os.chmod(path, 0o755)
        (root / "bin" / "llmt-env.sh").write_text((REPO / "bin" / "llmt-env.sh").read_text())
        for m in ("m1", "m2"):
            (t / "remotes" / m).mkdir(parents=True)
        (t / "remotes" / "m1" / "sysenv").write_text(f"CFDT_OUT={t}/remotes/m1/out\n")   # m2: no CFDT_OUT
        self.log = t / "ssh.log"
        self.saved_env = {k: os.environ.get(k) for k in ("HOME", "FAKE_REMOTES", "FAKE_HIDE", "FAKE_LOG", "FAKE_SLOW",
                                                         "CFDT_MAX_UPLOAD_MB")}
        os.environ.update(HOME=str(self.mac), FAKE_REMOTES=str(t / "remotes"), FAKE_HIDE=str(self.mac),
                          FAKE_LOG=str(self.log), FAKE_SLOW="0")
        os.environ.pop("CFDT_MAX_UPLOAD_MB", None)
        self.saved = (R.L.ssh_exe, R.PATHS_FILE)
        R.L.ssh_exe = lambda: str(t / "ssh")
        R.PATHS_FILE = t / "router_paths.json"
        r = R.Router("cfdt")
        r.hosts = [{"name": m, "ssh": f"u@{m}", "root": str(root)} for m in ("m1", "m2")]
        r.prefer, r.exclude, r.speed, r.paths = [], set(), {}, {}
        r.initialize({"protocolVersion": "2025-06-18"})
        self.r = r

    def tearDown(self):
        self.r.close()
        R.L.ssh_exe, R.PATHS_FILE = self.saved
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def copies(self) -> list:
        return [json.loads(x)["cmd"] for x in self.log.read_text().splitlines() if json.loads(x)["cmd"].startswith("cat >")]

    def run_call(self, studies: dict, tool: str = "run_studies") -> tuple:
        res = self.r.tool_call(tool, {"studies": studies, "wait_s": 0})
        return res, R.Router.parse(res)

    def test_nested_placement_and_no_second_copy(self):
        local = str(self.mac / "parts" / "a b.step")
        studies = {"s": {"geometry": {"step": local}, "more": [{"objects": ["~/parts/copy.stp"]}], "other": "/not/here"}}
        res, out = self.run_call(studies)
        self.assertFalse(res.get("isError"), out)
        m = out["machine"]
        self.assertEqual(len(out["staged"]), 2, out)
        base = {"m1": self.t / "remotes" / "m1" / "out" / "uploads",                 # CFDT_OUT/uploads
                "m2": self.t / "remotes" / "m2" / "cfdt_runs" / "uploads"}             # no CFDT_OUT: cfdt's default
        want = base[m] / self.sha[:12] / "a_b.step"
        self.assertEqual(out["staged"][local], str(want))
        self.assertEqual(set(out["seen"].values()) - {None}, {self.sha})                # the server saw the copies
        self.assertIsNone(out["seen"]["/not/here"])
        self.assertNotIn(local, out["seen"])
        self.assertFalse(any("not valid" in w for w in out["router"].get("warnings", [])), out["router"])
        for b in base.values():                                       # small files: validated on both machines
            self.assertEqual((b / self.sha[:12] / "a_b.step").read_bytes(), self.data)
        n = len(self.copies())
        res, out = self.run_call(studies)                              # again: nothing is sent
        self.assertEqual(len(self.copies()), n)
        self.assertEqual(set(out["seen"].values()) - {None}, {self.sha})
        # the same content under a new name: linked there, not sent
        (self.mac / "parts" / "new.step").write_bytes(self.data)
        res, out = self.run_call({"s": {"geometry": {"step": str(self.mac / "parts" / "new.step")},
                                        "run": {"host": "m1"}}})
        self.assertEqual(out["machine"], "m1")
        self.assertEqual(len(self.copies()), n)
        self.assertTrue((base["m1"] / self.sha[:12] / "new.step").is_file())
        # a staged path given back later counts as the local file (it can go to the other machine)
        remote_m1 = out["staged"][str(self.mac / "parts" / "new.step")]
        res, out = self.run_call({"s": {"geometry": {"step": remote_m1}, "run": {"host": "m2"}}})
        self.assertEqual(out["seen"][str(base["m2"] / self.sha[:12] / "new.step")], self.sha)

    def test_add_geometry_top_level_paths(self):
        # add_geometry(stl=<local path>, groups=<local path>) goes to every machine, each with its own copy
        (self.mac / "parts" / "groups.json").write_text('{"wheels": [1, 2]}')
        args = {"name": "rogue", "stl": str(self.mac / "parts" / "a b.step"),
                "groups": str(self.mac / "parts" / "groups.json"), "scale": 0.001}
        out = R.Router.parse(self.r.tool_call("add_geometry", args))
        for m in ("m1", "m2"):
            res = out["machines"][m]
            self.assertEqual(len(res["staged"]), 2, res)
            remote = res["staged"][args["stl"]]
            self.assertIn(f"/remotes/{m}/", remote)
            self.assertEqual(res["seen"][remote], self.sha)

    def test_limit(self):
        os.environ["CFDT_MAX_UPLOAD_MB"] = "0.1"
        res, out = self.run_call({"s": {"geometry": {"step": str(self.mac / "parts" / "a b.step")}}})
        self.assertTrue(res.get("isError"))
        self.assertIn("CFDT_MAX_UPLOAD_MB", out["error"])
        self.assertEqual(self.copies(), [])

    def test_no_default_output_folder(self):
        self.assertEqual(self.r.default_out(), "cfdt_runs")
        self.r.t = {**self.r.t, "env": {}}                          # a translator with no default listed
        self.assertIsNone(self.r.default_out())
        local = str(self.mac / "parts" / "a b.step")
        res, out = self.run_call({"s": {"geometry": {"step": local}, "run": {"host": "m2"}}})
        self.assertEqual(out["staged"][local],
                         str(self.t / "remotes" / "m2" / ".llmt" / "uploads" / self.sha[:12] / "a_b.step"))

    def test_shared_path_left_alone(self):
        os.environ["FAKE_HIDE"] = ""                                  # the machine sees the same path (shared folder)
        local = str(self.mac / "parts" / "a b.step")
        res, out = self.run_call({"s": {"geometry": {"step": local}}})
        self.assertNotIn("staged", out)
        self.assertEqual(out["seen"][local], self.sha)
        self.assertEqual(self.copies(), [])

    def test_slow_copy_then_resume(self):
        os.environ["FAKE_SLOW"] = "2"
        local = str(self.mac / "parts" / "a b.step")
        args = {"studies": {"s": {"geometry": {"step": local}}}, "wait_s": 0}
        out = R.Router.parse(self.r.call("m1", "run_studies", args, deadline=time.time() + 5))
        self.assertEqual(out["status"], "uploading")
        self.assertIn(local, out["uploading"])
        self.assertIn("again", out["next"])
        u = next(iter(self.r.uploads.values()))
        u.thread.join(20)
        self.assertTrue(u.done, u.error)
        os.environ["FAKE_SLOW"] = "0"
        out = R.Router.parse(self.r.call("m1", "run_studies", args, deadline=time.time() + 30))
        self.assertEqual(out["seen"][out["staged"][local]], self.sha)
        self.assertEqual(len(self.copies()), 1)
        # resuming: half of another file is already there as .part; only the rest is appended
        other = os.urandom(200_000)
        (self.mac / "parts" / "c.step").write_bytes(other)
        sha = hashlib.sha256(other).hexdigest()
        d = self.t / "remotes" / "m2" / "cfdt_runs" / "uploads" / sha[:12]
        d.mkdir(parents=True)
        (d / ".c.step.part").write_bytes(other[:120_000])
        out = R.Router.parse(self.r.call("m2", "run_studies", {"studies": {"s": {"step": str(self.mac / "parts" / "c.step")}}},
                                         deadline=time.time() + 30))
        self.assertEqual(out["seen"][str(d / "c.step")], sha)
        self.assertTrue(self.copies()[-1].startswith("cat >>"), self.copies())


if __name__ == "__main__":
    unittest.main()
