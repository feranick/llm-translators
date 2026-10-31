"""Tests for llm-translators' own code: python3 -m unittest discover -s tests (standard library only).

    - the router notices a server process that runs another version than the installed one (serving,
      server_stale) and replaces it once idle;
    - llmt servers: the process list, with a symlinked prefix, a shell mentioning a server path, a SLURM job;
    - llmt deploy's pin check: a pin behind its branch's newest commit;
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
            r = {"tools": [{"name": n, "description": n, "inputSchema": {"type": "object", "properties": {}}}
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
