"""Tests for llm-translators' own code: python3 -m unittest discover -s tests (standard library only).

    - the router notices a server process that runs another version than the installed one (serving,
      server_stale) and replaces it once idle;
    - llmt servers: the process list, with a symlinked prefix, a shell mentioning a server path, a SLURM job;
    - llmt deploy's pin check: a pin behind its branch's newest commit.
"""
from __future__ import annotations

import contextlib
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
        self.assertFalse(c1.alive())


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


if __name__ == "__main__":
    unittest.main()
