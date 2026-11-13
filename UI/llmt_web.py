#!/usr/bin/env python3
"""UI/llmt_web.py — the llmt gui window as a web page: a local server for UI/llmt_web.html.
Version: 2026.11.12.3

    ./llmt web [--port 8790] [--no-browser]

Same three tabs as llmt gui (Setup, Settings, Machines) and the same model (llmt_gui.py), so both behave alike.
Standard library only (no tkinter).

Safety:
- The server listens on 127.0.0.1 only.
- Every /api call must carry the token printed at start (the page keeps it in the URL's #fragment, which is
  never sent to the server or to other sites), and a Host of 127.0.0.1 or localhost. Another web site can neither
  read the answers nor start a command.
- Commands are only the Setup actions (llmt install / update / remove / test / tune --print / mcp config /
  mcp test), one at a time. The Machines tab only reads.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

__version__ = "2026.11.12.3"

HERE = Path(__file__).resolve().parent
PAGE = HERE / "llmt_web.html"
PORT = 8790


def _load(name: str, file: str):
    loader = SourceFileLoader(name, str(HERE / file))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(m)
    return m


G = _load("llmt_gui", "llmt_gui.py")      # the model shared with the window (it needs no tkinter)
L = G.L


class Job:
    """The one llmt command running for the page: its output lines, its exit code."""

    def __init__(self, args: list[str], title: str):
        self.id = secrets.token_hex(4)
        self.args, self.title = args, title
        self.lines: list[str] = [f"$ ./llmt {' '.join(args)}\n"]
        self.rc: int | None = None
        self.started = time.time()
        self.proc = subprocess.Popen(G.llmt_cmd(*args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, text=True, bufsize=1, cwd=str(L.ROOT))
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.append(line)
        self.rc = self.proc.wait()
        self.lines.append(f"(exit {self.rc})\n")

    def view(self, start: int) -> dict:
        return {"id": self.id, "title": self.title, "lines": self.lines[start:], "next": len(self.lines),
                "done": self.rc is not None, "rc": self.rc}


class State:
    def __init__(self, token: str):
        self.token = token
        self.job: Job | None = None
        self.lock = threading.Lock()
        self.results: dict = {}           # machine name -> its last fetch_machine answer


def env_path(which: str) -> Path:
    return Path(L.SYSTEM_ENV) if which == "system" else Path(L.ENV_FILE)


def writable(p: Path) -> bool:
    return os.access(p if p.exists() else p.parent, os.W_OK)


def info() -> dict:
    setup = L.load_setup()
    reg = {k: {"repo": t["repo"], "summary": t.get("summary", ""), "cli": t.get("cli"),
               "optional_extras": t.get("optional_extras") or {}} for k, t in L.REGISTRY.items()}
    return {"llmt": L.__version__, "web": __version__, "root": str(L.ROOT), "shared": bool(L.SHARED),
            "keys": list(L.KEYS), "registry": reg, "local": G.LOCAL,
            "setup": {"test": setup.get("test", True), "extras": setup.get("extras") or {},
                      "mcp": setup.get("mcp") or {}, "hosts": (setup.get("router") or {}).get("hosts", [])},
            "env": {"mine": str(L.ENV_FILE), "system": str(L.SYSTEM_ENV) if Path(L.SYSTEM_ENV) != Path(L.ENV_FILE) else None}}


def machine_view(res: dict) -> dict:
    return {"result": {k: v for k, v in res.items() if k != "data"}, "summary": G.machine_summary(res),
            "notes": G.machine_notes(res),
            "translators": G.translator_rows(res), "jobs": G.slurm_job_rows(res),
            "installed": [k for k, t in ((res.get("data") or {}).get("translators") or {}).items() if t.get("version")]}


def make_handler(st: State):
    class H(BaseHTTPRequestHandler):
        server_version = f"llmt-web/{__version__}"

        def log_message(self, fmt, *args):  # quiet: the terminal shows only errors
            pass

        # ---- answers
        def send(self, code: int, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                 "connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def fail(self, code: int, msg: str):
            self.send(code, {"error": msg})

        def host_ok(self) -> bool:
            h = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return h in ("127.0.0.1", "localhost", "::1")

        def allowed(self) -> bool:
            if not self.host_ok():
                self.fail(403, "Host must be 127.0.0.1 or localhost")
                return False
            if not secrets.compare_digest(self.headers.get("X-LLMT-Token", ""), st.token):
                self.fail(401, "missing or wrong token: open the address llmt web printed")
                return False
            return True

        def body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                raise ValueError("request too large")
            raw = self.rfile.read(n) if n else b"{}"
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ValueError("send JSON")
            v = json.loads(raw or b"{}")
            if not isinstance(v, dict):
                raise ValueError("send a JSON object")
            return v

        def host_named(self, name: str) -> dict:
            h = next((h for h in G.machines() if h["name"] == name), None)
            if not h:
                raise KeyError(f"no machine {name!r}")
            return h

        # ---- routes
        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                if not self.host_ok():
                    return self.fail(403, "Host must be 127.0.0.1 or localhost")
                return self.send(200, PAGE.read_bytes(), "text/html")
            if not u.path.startswith("/api/") or not self.allowed():
                return None if u.path.startswith("/api/") else self.fail(404, "not found")
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            parts = [unquote(p) for p in u.path.split("/")[2:]]
            try:
                if parts == ["info"]:
                    return self.send(200, info())
                if parts == ["status"]:
                    r = subprocess.run(G.llmt_cmd("status", "--json"), capture_output=True, text=True,
                                       timeout=G.STATUS_TIMEOUT, stdin=subprocess.DEVNULL, cwd=str(L.ROOT))
                    return self.send(200, json.loads(r.stdout) if r.stdout.strip() else {"error": r.stderr[-400:]})
                if parts == ["job"]:
                    j = st.job
                    return self.send(200, j.view(int(q.get("from", 0))) if j else {"id": None, "done": True, "lines": [], "next": 0})
                if parts == ["settings"]:
                    p = env_path(q.get("file", "mine"))
                    secs = G.settings_sections(p)
                    return self.send(200, {"path": str(p), "writable": writable(p),
                                           "problems": L.env_problems(p) if p.exists() else [],
                                           "sections": [{"name": n, "rows": rows} for n, rows in secs]})
                if parts == ["settings", "raw"]:
                    p = env_path(q.get("file", "mine"))
                    return self.send(200, {"path": str(p), "writable": writable(p),
                                           "text": p.read_text() if p.exists() else L.env_template()})
                if parts == ["machines"]:
                    return self.send(200, {"machines": [{k: h.get(k) for k in ("name", "ssh", "root", "label")}
                                                        for h in G.machines()]})
                if len(parts) == 2 and parts[0] == "machine":
                    res = G.fetch_machine(self.host_named(parts[1]))
                    st.results[parts[1]] = res
                    return self.send(200, machine_view(res))
                if len(parts) == 3 and parts[0] == "machine" and parts[2] == "requests":
                    h = self.host_named(parts[1])
                    keys = machine_view(st.results[parts[1]])["installed"] if parts[1] in st.results else []
                    if not keys:
                        return self.fail(409, f"no translator answered on {parts[1]} yet: refresh it first")
                    return self.send(200, {"rows": G.request_rows(G.fetch_requests(h, keys))})
                if len(parts) == 3 and parts[0] == "machine" and parts[2] == "servers":
                    return self.send(200, {"text": G.fetch_servers(self.host_named(parts[1]))})
                return self.fail(404, "not found")
            except KeyError as exc:
                return self.fail(404, str(exc).strip("'\""))
            except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                return self.fail(500, str(exc)[:400])

        def do_POST(self):
            u = urlparse(self.path)
            if not self.allowed():
                return None
            parts = u.path.split("/")[2:]
            try:
                b = self.body()
                if parts == ["action"]:
                    args = G.action_args(str(b.get("action")), list(b.get("keys") or []), bool(b.get("test", True)))
                    with st.lock:
                        if st.job and st.job.rc is None:
                            return self.fail(409, f"'{st.job.title}' is still running (Stop ends it)")
                        st.job = Job(args, str(b.get("action")))
                        return self.send(200, st.job.view(0))
                if parts == ["job", "stop"]:
                    if st.job and st.job.rc is None:
                        st.job.proc.terminate()
                    return self.send(200, {"ok": True})
                if parts == ["options"]:
                    s = G.save_options(bool(b.get("test", True)), b.get("extras") or {})
                    return self.send(200, {"test": s["test"], "extras": s["extras"]})
                if parts == ["mcp"]:
                    changed, kept = G.set_mcp_where(list(b.get("keys") or []), str(b.get("where")), bool(b.get("replace")))
                    return self.send(200, {"changed": changed, "kept": kept})
                if parts == ["hosts"]:
                    return self.send(200, {"hosts": G.save_hosts(list(b.get("hosts") or []))})
                if parts == ["settings"]:
                    p = env_path(str(b.get("file", "mine")))
                    if not writable(p):
                        return self.fail(403, f"{p} is not writable by you")
                    probs = G.save_settings({str(k): str(v) for k, v in (b.get("changes") or {}).items()}, p)
                    return self.send(200, {"problems": probs})
                if parts == ["settings", "raw"]:
                    p = env_path(str(b.get("file", "mine")))
                    if not writable(p):
                        return self.fail(403, f"{p} is not writable by you")
                    text = str(b.get("text", "")).rstrip("\n") + "\n"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    tmp = p.with_name(p.name + ".tmp-llmt-web")
                    tmp.write_text(text)
                    os.replace(tmp, p)
                    return self.send(200, {"problems": L.env_problems(p)})
                return self.fail(404, "not found")
            except ValueError as exc:
                return self.fail(400, str(exc))
            except OSError as exc:
                return self.fail(500, str(exc)[:400])
    return H


def serve(port: int = PORT, open_browser: bool = True, token: str | None = None):
    st = State(token or secrets.token_urlsafe(24))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(st))
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/#t={st.token}"
    return httpd, url, st


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="llmt web", description="llm-translators in a browser (this machine only)")
    ap.add_argument("--port", type=int, default=PORT, help=f"default {PORT}; 0: any free port")
    ap.add_argument("--no-browser", action="store_true", help="print the address only")
    a = ap.parse_args(argv)
    try:
        httpd, url, _ = serve(a.port)
    except OSError as exc:
        sys.exit(f"llmt web: port {a.port}: {exc} (another llmt web? try --port 0)")
    print(f"llmt web: {url}\n  (this address carries the access token: keep it to yourself; Ctrl-C stops the server)",
          flush=True)
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nllmt web: stopped")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
