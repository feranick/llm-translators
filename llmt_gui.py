#!/usr/bin/env python3
"""llmt_gui.py — a window for llm-translators: set up this machine, edit its settings, watch the machines.
Version: 2026.11.12.1

    ./llmt gui                 (or: python3 llmt_gui.py)

Tabs:
    Setup      the translators on this machine: install, update, reinstall, remove, test; extras; MCP entries
               of the Claude app (local / router / none), written with llmt mcp config and checked with llmt mcp test
    Settings   config/env (or ~/.config/llm-translators/env in a release; the machine's /etc file when writable):
               every variable of translators.json with its description, plus any other in the file
    Machines   the router's machines (config/setup.json) and this one, read-only: llm-translators release and
               commit, each translator's version and backends, SLURM CPUs and the translators' jobs, load; per
               machine, the translators' active requests (<cli> jobs --active) and the MCP servers (llmt servers)

Standard library and tkinter only. Every action runs ./llmt (the same commands as in a terminal) or ssh in the
background; its output goes to the log at the bottom. Nothing here changes a remote machine.
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import json
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

try:  # the model below works without tkinter (tests); the window needs it
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover
    tk = ttk = messagebox = None

__version__ = "2026.11.12.1"

HERE = Path(__file__).resolve().parent


def load_llmt():
    loader = SourceFileLoader("llmt", str(HERE / "llmt"))
    spec = importlib.util.spec_from_loader("llmt", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


L = load_llmt()

STATUS_TIMEOUT = 300      # llmt status --json on a machine (each translator's status: omct loads its library)
JOBS_TIMEOUT = 120
LOCAL = "local"            # this machine's name in the Machines tab (a router machine may not use it)


# ================================================================ model (no tkinter)

def llmt_cmd(*args: str) -> list[str]:
    """./llmt ARGS with the Python running this window (llmt needs only the standard library)."""
    return [sys.executable, str(L.ROOT / "llmt"), *args]


def machines(setup: dict | None = None) -> list[dict]:
    """This machine first, then the router's machines (config/setup.json: router.hosts)."""
    setup = L.load_setup() if setup is None else setup
    out = [{"name": LOCAL, "ssh": "", "root": str(L.ROOT), "label": "this machine"}]
    for h in (setup.get("router") or {}).get("hosts", []):
        if h.get("name") and h.get("root") and h["name"] != LOCAL:
            out.append({"name": h["name"], "ssh": h.get("ssh", ""), "root": h["root"]})
    return out


def root_expr(root: str) -> str:
    r = root.rstrip("/")
    return '"$HOME' + r[1:] + '"' if r.startswith("~") else shlex.quote(r)


def shell(host: dict, script: str, timeout: float) -> subprocess.CompletedProcess:
    """A shell script on the machine: over ssh (batch mode, one shared connection per host), or here."""
    if host.get("ssh"):
        ctl = Path.home() / ".ssh" / "llmt-%C"
        cmd = [L.ssh_exe(), *L.SSH_OPTS, "-o", "ControlMaster=auto", "-o", f"ControlPath={ctl}",
               "-o", "ControlPersist=600", "-o", "ConnectTimeout=10", "-n", host["ssh"], script]
    else:
        cmd = ["sh", "-c", script]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def fetch_machine(host: dict, timeout: float = STATUS_TIMEOUT) -> dict:
    """llmt status --json on the machine, its load and cores. -> {ok, data, load, cores, error, seconds}."""
    t0 = time.time()
    script = (f"r={root_expr(host['root'])}; python3 \"$r/llmt\" status --json 2>/dev/null; echo; echo @@LOAD; "
              f"cat /proc/loadavg 2>/dev/null || sysctl -n vm.loadavg 2>/dev/null; "
              f"nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null")
    out = {"name": host["name"], "ok": False, "asked": t0}
    try:
        r = shell(host, script, timeout)
        text, _, load = r.stdout.partition("@@LOAD")
        try:
            out["data"] = json.loads(text.strip()) if text.strip() else None
        except json.JSONDecodeError:
            out["data"] = None
        if out["data"] is None:
            out["error"] = (r.stderr or "no answer from llmt status").strip()[-300:]
        else:
            out["ok"] = True
        nums = [x for x in re.split(r"[\s{}]+", load) if x]
        try:
            out["load"], out["cores"] = float(nums[0]), int(nums[-1])
        except (ValueError, IndexError):
            pass
    except (OSError, subprocess.TimeoutExpired) as exc:
        out["error"] = str(exc)[:300]
    out["seconds"] = round(time.time() - t0, 1)
    return out


def parse_json_out(text: str):
    """The JSON a translator's CLI prints (the first '{' or '[' on, if something precedes it)."""
    text = text.strip()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return json.loads(text[i:])
            except json.JSONDecodeError:
                return None
    return None


def fetch_requests(host: dict, keys: list[str], timeout: float = JOBS_TIMEOUT) -> dict:
    """Each translator's active requests on the machine (<root>/bin/run <cli> jobs --active), as its jobs() tool
    gives them. -> {key: data or {"error": ...}}."""
    out: dict = {}

    def one(k):
        cli = L.REGISTRY[k]["cli"]
        try:
            r = shell(host, f"{root_expr(host['root'])}/bin/run {shlex.quote(cli)} jobs --active --limit 20", timeout)
            data = parse_json_out(r.stdout)
            out[k] = data if data is not None else {"error": (r.stderr or r.stdout or "no answer").strip()[-300:]}
        except (OSError, subprocess.TimeoutExpired) as exc:
            out[k] = {"error": str(exc)[:300]}
    ths = [threading.Thread(target=one, args=(k,), daemon=True) for k in keys]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout + 5)
    return out


def fetch_servers(host: dict, timeout: float = 60) -> str:
    """llmt servers on the machine (Linux): the MCP servers and leftover workers. Read-only (no --stop)."""
    try:
        r = shell(host, f"python3 {root_expr(host['root'])}/llmt servers", timeout)
        return (r.stdout + (("\n" + r.stderr) if r.stderr.strip() else "")).strip() or "(no answer)"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"error: {exc}"


def machine_summary(res: dict) -> dict:
    """One row of the machine table."""
    d = res.get("data") or {}
    row = {"name": res["name"], "reachable": bool(res.get("ok")), "error": res.get("error")}
    if not d:
        return row
    rel = d.get("release") or {}
    row["bundle"] = (f"release {rel.get('release')}" if d.get("mode") == "release" else "checkout") + \
        (f" @ {d.get('commit')}" if d.get("commit") else "")
    row["llmt"] = d.get("llmt")
    rt = d.get("runtime") or {}
    row["runtime"] = rt.get("installed") or rt.get("version")
    sl = d.get("slurm") or {}
    if sl:
        jobs = sl.get("jobs") or []
        row["cpus"] = f"{sl.get('cpus_allocated', '?')}/{sl.get('cpus', '?')} in use" + \
            (f" ({sl.get('state')})" if sl.get("state") else "")
        row["jobs"] = f"{sum(1 for j in jobs if j.get('state') == 'RUNNING')} running, " \
                      f"{sum(1 for j in jobs if j.get('state') != 'RUNNING')} queued"
    else:
        row["cpus"], row["jobs"] = "no SLURM", ""
    if res.get("load") is not None:
        row["load"] = f"{res['load']:.1f} / {res.get('cores', '?')}"
    notes = []
    for k, t in (d.get("translators") or {}).items():
        if t.get("reinstall"):
            notes.append(f"{k}: reinstall")
        miss = [n for n, b in (t.get("backends") or {}).items() if not b.get("found")]
        if miss:
            notes.append(f"{k}: {', '.join(miss)} missing")
    if rt.get("problems"):
        notes.append("runtime: " + "; ".join(map(str, rt["problems"]))[:80])
    row["note"] = "; ".join(notes)
    return row


def translator_rows(res: dict) -> list[dict]:
    """The translators installed on a machine (children of its row)."""
    d = res.get("data") or {}
    jobs = ((d.get("slurm") or {}).get("jobs")) or []
    out = []
    for k, t in (d.get("translators") or {}).items():
        if not t.get("version"):
            continue
        st = t.get("status") if isinstance(t.get("status"), dict) else {}
        ex = (st or {}).get("executor") or {}
        kind = ex.get("kind") if isinstance(ex, dict) else None
        mine = [j for j in jobs if j.get("key") == k]
        bes = t.get("backends") or {}
        out.append({"key": k, "version": t["version"], "commit": t.get("commit") or "",
                    "executor": kind or (t.get("executor") or {}).get("setting") or "local",
                    "jobs": (f"{sum(1 for j in mine if j.get('state') == 'RUNNING')} running, "
                             f"{sum(1 for j in mine if j.get('state') != 'RUNNING')} queued") if jobs or kind == "slurm" else "",
                    "backends": ", ".join(f"{n} {'ok' if b.get('found') else 'MISSING'}" for n, b in bes.items()) or "none needed",
                    "note": t.get("problem") or ""})
    return out


def slurm_job_rows(res: dict) -> list[dict]:
    jobs = (((res.get("data") or {}).get("slurm") or {}).get("jobs")) or []
    rows = []
    for j in jobs:
        running = j.get("state") == "RUNNING"
        rows.append({"id": j.get("id", ""), "key": j.get("key", ""), "state": j.get("state", ""),
                     "time": f"{j.get('elapsed', '')} / {j.get('limit', '')}", "cpus": j.get("cpus", ""),
                     "node": j.get("nodes") or (j.get("reason") if running else j.get("req_nodes")) or "",
                     "study": j.get("study", ""), "request": j.get("request", ""),
                     "why": "" if running else (j.get("reason", "") + (f", start {j['start']}" if j.get("start") not in (None, "", "N/A") else ""))})
    return rows


DETAIL_KEYS = ("stage", "progress", "job_elapsed_min", "eta_min", "eta_min_max", "expected_finish", "expected_start",
               "reason", "ahead", "why", "ok", "reliable")


def request_rows(data: dict) -> list[dict]:
    """The studies of the translators' active requests, whatever each translator's jobs() adds."""
    rows = []
    for k, d in data.items():
        if not isinstance(d, dict) or d.get("error"):
            rows.append({"key": k, "request": "", "study": "", "state": "error", "job": "",
                         "detail": str((d or {}).get("error", "no answer"))[:200]})
            continue
        reqs = d.get("requests") or []
        if not reqs:
            rows.append({"key": k, "request": "(none active)", "study": "", "state": "", "job": "", "detail": ""})
        for rq in reqs:
            studies = rq.get("studies") or {}
            for name, s in (studies.items() if isinstance(studies, dict) else []):
                s = s if isinstance(s, dict) else {"state": str(s)}
                det = []
                for key in DETAIL_KEYS:
                    v = s.get(key)
                    if v not in (None, "", [], {}):
                        if isinstance(v, dict):
                            v = ", ".join(f"{a}={b}" for a, b in list(v.items())[:4])
                        det.append(f"{key}={v}")
                rows.append({"key": k, "request": rq.get("request", ""), "study": name, "state": s.get("state", ""),
                             "job": s.get("job_id") or "", "detail": "; ".join(det)})
    return rows


def settings_sections(env_file: Path | None = None) -> list[tuple[str, list[dict]]]:
    """[(section, [{var, desc, value, machine}])]: each translator's variables (translators.json), then any other
    in the file. value: in the file being edited; machine: in the machine's file when another one is edited."""
    env_file = env_file or L.ENV_FILE
    mine = L.env_read(env_file)
    machine = L.env_read(L.SYSTEM_ENV) if Path(L.SYSTEM_ENV) != Path(env_file) else {}
    seen, out = set(), []
    for k, t in L.REGISTRY.items():
        rows = []
        for var, desc in (t.get("env") or {}).items():
            seen.add(var)
            rows.append({"var": var, "desc": desc, "value": mine.get(var, ""), "machine": machine.get(var, "")})
        out.append((f"{k}  ({t['repo']})", rows))
    other = [v for v in list(mine) + list(machine) if v not in seen]
    if other:
        out.append(("other", [{"var": v, "desc": "", "value": mine.get(v, ""), "machine": machine.get(v, "")}
                              for v in dict.fromkeys(other)]))
    return out


def save_settings(changes: dict, env_file: Path) -> list[str]:
    """Writes the changed variables ('' comments a variable out) into env_file; -> the file's problems."""
    saved = L.ENV_FILE
    try:
        L.ENV_FILE = Path(env_file)
        L.env_set(changes)
    finally:
        L.ENV_FILE = saved
    return L.env_problems(Path(env_file))


# ================================================================ the window

class App:
    def __init__(self, root):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.proc = None
        self.results: dict = {}
        self.status_local: dict = {}
        self.pending: dict = {}
        self.env_file = Path(L.ENV_FILE)
        root.title(f"llm-translators {L.__version__}" + (" (shared release)" if L.SHARED else ""))
        root.geometry("1180x780")
        root.minsize(900, 600)
        style = ttk.Style()
        if "clam" in style.theme_names() and sys.platform != "darwin":
            style.theme_use("clam")
        style.configure("Treeview", rowheight=22)

        pw = ttk.PanedWindow(root, orient="vertical")
        pw.pack(fill="both", expand=True)
        self.nb = ttk.Notebook(pw)
        pw.add(self.nb, weight=4)
        self.build_setup()
        self.build_settings()
        self.build_machines()
        pw.add(self.build_log(pw), weight=1)
        root.after(100, self.poll)
        self.refresh_setup()
        self.load_settings()
        self.refresh_machines()

    # ---- plumbing: background work posts (callback, value) to the queue; poll() runs them in the UI thread
    def bg(self, fn, done, *args):
        def run():
            try:
                v = fn(*args)
            except Exception as exc:  # noqa: BLE001
                v = exc
            self.q.put((done, v))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        try:
            while True:
                done, v = self.q.get_nowait()
                done(v)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def build_log(self, parent):
        f = ttk.Frame(parent)
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        self.run_label = ttk.Label(bar, text="Log")
        self.run_label.pack(side="left", padx=6)
        ttk.Button(bar, text="Clear", command=lambda: self.log.delete("1.0", "end")).pack(side="right", padx=4)
        self.stop_btn = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.log = tk.Text(f, height=10, wrap="word", font=("Menlo" if sys.platform == "darwin" else "Monospace", 10))
        sb = ttk.Scrollbar(f, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        return f

    def say(self, text: str):
        self.log.insert("end", text if text.endswith("\n") else text + "\n")
        self.log.see("end")

    def run(self, args: list[str], title: str, after=None):
        """./llmt ARGS in the background, its output streamed to the log; one at a time."""
        if self.proc is not None:
            messagebox.showinfo("Busy", "A command is still running (Stop ends it).")
            return
        cmd = llmt_cmd(*args)
        self.say(f"\n$ ./llmt {' '.join(shlex.quote(a) for a in args)}")
        self.run_label.configure(text=f"Running: {title}")
        self.stop_btn.configure(state="normal")
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         text=True, bufsize=1, cwd=str(L.ROOT))
        except OSError as exc:
            self.say(f"cannot start: {exc}")
            self.finish(None, after)
            return
        p = self.proc

        def pump():
            for line in p.stdout:
                self.q.put((self.say, line))
            rc = p.wait()
            self.q.put((lambda _: self.finish(rc, after), None))
        threading.Thread(target=pump, daemon=True).start()

    def finish(self, rc, after):
        self.proc = None
        self.stop_btn.configure(state="disabled")
        self.run_label.configure(text="Log")
        if rc is not None:
            self.say(f"(exit {rc})")
        if after:
            after(rc)

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            self.say("(stopping)")

    @staticmethod
    def tree(parent, cols: list[tuple[str, str, int]], height=10, show="headings"):
        f = ttk.Frame(parent)
        t = ttk.Treeview(f, columns=[c for c, _, _ in cols], show=show, height=height)
        for c, label, w in cols:
            t.heading(c, text=label)
            t.column(c, width=w, stretch=c in ("note", "detail", "summary", "desc", "request", "value"))
        sy = ttk.Scrollbar(f, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sy.set)
        sy.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        return f, t

    # ================================================================ Setup tab
    def build_setup(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Setup (this machine)")
        self.setup_info = ttk.Label(tab, text="…", justify="left")
        self.setup_info.pack(fill="x", pady=(0, 4))
        f, self.st_tree = self.tree(tab, [("key", "Translator", 70), ("version", "Installed", 110),
                                          ("commit", "Commit", 70), ("submodule", "Checkout", 80),
                                          ("mcp", "Claude app", 80), ("backends", "Backends", 240),
                                          ("note", "Note", 300)], height=7)
        f.pack(fill="both", expand=True)
        if L.SHARED:
            ttk.Label(tab, text="This is a shared release: installing and updating is the admin's (llmt deploy).",
                      foreground="#a05000").pack(fill="x", pady=2)

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(6, 2))
        state = "disabled" if L.SHARED else "normal"
        for text, fn in (("Install", self.do_install), ("Update (pinned)", lambda: self.do_update(False)),
                         ("Update to latest", lambda: self.do_update(True)), ("Reinstall", self.do_reinstall),
                         ("Remove", self.do_remove)):
            ttk.Button(row, text=text, command=fn, state=state).pack(side="left", padx=2)
        ttk.Separator(row, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(row, text="Run tests", command=self.do_test).pack(side="left", padx=2)
        ttk.Button(row, text="Suggest tuning", command=lambda: self.run(["tune", "--print"], "tune")).pack(side="left", padx=2)
        ttk.Button(row, text="Refresh", command=self.refresh_setup).pack(side="right", padx=2)

        opts = ttk.LabelFrame(tab, text="Options for Install / Update (config/setup.json)", padding=4)
        opts.pack(fill="x", pady=4)
        setup = L.load_setup()
        self.test_var = tk.BooleanVar(value=bool(setup.get("test", True)))
        ttk.Checkbutton(opts, text="test tools (pytest)", variable=self.test_var, command=self.save_options,
                        state=state).pack(side="left")
        self.extra_vars: dict = {}
        for k, t in L.REGISTRY.items():
            for e, what in (t.get("optional_extras") or {}).items():
                v = tk.BooleanVar(value=e in (setup.get("extras") or {}).get(k, []))
                self.extra_vars[(k, e)] = v
                cb = ttk.Checkbutton(opts, text=f"{k}: {e}", variable=v, command=self.save_options, state=state)
                cb.pack(side="left", padx=8)
                Tip(cb, what)

        mcp = ttk.LabelFrame(tab, text="Claude app on this machine (MCP entries)", padding=4)
        mcp.pack(fill="x", pady=4)
        ttk.Label(mcp, text="Selected translators:").pack(side="left")
        self.where_var = tk.StringVar(value="router")
        for w, label in (("local", "local"), ("router", "router (several machines)"), ("none", "none")):
            ttk.Radiobutton(mcp, text=label, value=w, variable=self.where_var).pack(side="left", padx=4)
        ttk.Button(mcp, text="Set", command=self.set_where).pack(side="left", padx=4)
        ttk.Button(mcp, text="Write Claude config", command=lambda: self.run(["mcp", "config"], "mcp config")).pack(side="left", padx=4)
        ttk.Button(mcp, text="Test entries", command=self.do_mcp_test).pack(side="left", padx=4)
        ttk.Button(mcp, text="Machines…", command=self.edit_machines).pack(side="right", padx=4)

    def selected_keys(self, need=True) -> list[str]:
        keys = [k for k in self.st_tree.selection() if k in L.REGISTRY]
        if not keys and need:
            messagebox.showinfo("Select", "Select one or more translators in the table first.")
        return keys

    def refresh_setup(self):
        self.setup_info.configure(text="Reading llmt status …")

        def get():
            r = subprocess.run(llmt_cmd("status", "--json"), capture_output=True, text=True, timeout=STATUS_TIMEOUT,
                               stdin=subprocess.DEVNULL, cwd=str(L.ROOT))
            return json.loads(r.stdout) if r.stdout.strip() else {"_error": r.stderr.strip()[-400:]}
        self.bg(get, self.show_setup)

    def show_setup(self, d):
        if isinstance(d, Exception) or d.get("_error"):
            self.setup_info.configure(text=f"llmt status failed: {d if isinstance(d, Exception) else d['_error']}")
            return
        self.status_local = d
        setup = L.load_setup()
        mcp = setup.get("mcp") or {}
        rt = d.get("runtime") or {}
        self.setup_info.configure(text=(
            f"llm-translators {d.get('llmt')}  ·  {('release ' + str((d.get('release') or {}).get('release'))) if d.get('mode') == 'release' else 'checkout'}"
            f" @ {d.get('commit') or '?'}  ·  {d.get('root')}\n"
            f"venv: {d.get('venv') or 'none yet (Install creates it)'}" + (f"  (Python {d['python']})" if d.get("python") else "") +
            (f"  ·  llmt-runtime {rt.get('installed') or '-'} (pinned {rt.get('version') or '?'})" if rt else "") +
            ("  ·  settings: " + ", ".join(d.get("settings") or []) if d.get("settings") else "")))
        sel = self.st_tree.selection()
        self.st_tree.delete(*self.st_tree.get_children())
        for k, t in (d.get("translators") or {}).items():
            bes = t.get("backends") or {}
            note = t.get("problem") and f"{t['problem']}: run {t.get('fix')}" or \
                ("not installed" if not t.get("version") else "")
            if t.get("submodule") == "moved":
                note = (note + "; " if note else "") + "not at the pinned commit"
            self.st_tree.insert("", "end", iid=k, values=(
                k, t.get("version") or "-", t.get("commit") or "-", t.get("submodule") or "-",
                (mcp.get(k) or {}).get("where", "-"),
                ", ".join(f"{n} {'ok' if b.get('found') else 'MISSING'}" for n, b in bes.items()) or ("-" if not t.get("version") else "none needed"),
                note))
        self.st_tree.selection_set([k for k in sel if self.st_tree.exists(k)])

    def save_options(self):
        setup = L.load_setup()
        setup["test"] = bool(self.test_var.get())
        ex: dict = {}
        for (k, e), v in self.extra_vars.items():
            if v.get():
                ex.setdefault(k, []).append(e)
        setup["extras"] = ex
        L.save_setup(setup)
        self.say("(config/setup.json: options saved; Install or Reinstall applies them)")

    def do_install(self):
        keys = self.selected_keys()
        if keys:
            self.run(["install", *keys, "--test" if self.test_var.get() else "--no-test"], "install",
                     after=lambda rc: self.refresh_setup())

    def do_update(self, latest: bool):
        keys = self.selected_keys(need=False)
        args = ["update", *keys] + (["--latest"] if latest else [])
        if latest and not messagebox.askyesno("Update to latest", "Check out each translator's newest commit "
                                              "(past the pins)? 'llmt pin' records them afterwards."):
            return
        self.run(args, "update", after=lambda rc: self.refresh_setup())

    def do_reinstall(self):
        keys = self.selected_keys()
        if keys:
            self.run(["update", *keys, "--no-pull"], "reinstall", after=lambda rc: self.refresh_setup())

    def do_remove(self):
        keys = self.selected_keys()
        if keys and messagebox.askyesno("Remove", f"Uninstall {', '.join(keys)} from the venv?"):
            self.run(["remove", *keys], "remove", after=lambda rc: self.refresh_setup())

    def do_test(self):
        self.run(["test", *self.selected_keys(need=False)], "tests")

    def do_mcp_test(self):
        self.run(["mcp", "test", *self.selected_keys(need=False)], "mcp test")

    def set_where(self):
        keys = self.selected_keys()
        if not keys:
            return
        w = self.where_var.get()
        setup = L.load_setup()
        mcp = setup.setdefault("mcp", {})
        if w == "router" and not (setup.get("router") or {}).get("hosts"):
            messagebox.showwarning("Router", "No machines for the router yet: add them with Machines…")
            return
        for k in keys:
            prev = mcp.get(k) or {}
            if prev.get("where") in ("remote", "existing") and not messagebox.askyesno(
                    "Replace", f"{k} is '{prev['where']}' now ({prev.get('host') or prev.get('entry', '')}). Replace it?"):
                continue
            mcp[k] = {"where": w, **({"env": prev["env"]} if prev.get("env") and w == "local" else {})}
        L.save_setup(setup)
        self.say(f"(config/setup.json: {', '.join(keys)} -> {w}. 'Write Claude config' updates the Claude app; restart it.)")
        self.refresh_setup()

    def edit_machines(self):
        MachinesDialog(self.root, on_save=self.refresh_machines)

    # ================================================================ Settings tab
    def build_settings(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Settings")
        top = ttk.Frame(tab)
        top.pack(fill="x")
        self.file_var = tk.StringVar(value="mine")
        ttk.Radiobutton(top, text=f"Mine: {L.ENV_FILE}", value="mine", variable=self.file_var,
                        command=self.load_settings).pack(side="left")
        if Path(L.SYSTEM_ENV) != Path(L.ENV_FILE):
            ttk.Radiobutton(top, text=f"The machine's: {L.SYSTEM_ENV}", value="system", variable=self.file_var,
                            command=self.load_settings).pack(side="left", padx=12)
        ttk.Button(top, text="Edit file…", command=self.edit_raw).pack(side="right", padx=2)
        ttk.Button(top, text="Reload", command=self.load_settings).pack(side="right", padx=2)

        body = ttk.PanedWindow(tab, orient="horizontal")
        body.pack(fill="both", expand=True, pady=4)
        f, self.env_tree = self.tree(body, [("value", "Value (this file)", 260), ("machine", "Machine's file", 200)],
                                     height=14, show="tree headings")
        self.env_tree.heading("#0", text="Variable")
        self.env_tree.column("#0", width=260, stretch=False)
        self.env_tree.bind("<<TreeviewSelect>>", self.pick_var)
        body.add(f, weight=3)

        side = ttk.Frame(body, padding=6)
        body.add(side, weight=2)
        self.var_name = ttk.Label(side, text="Select a variable", font=("TkDefaultFont", 12, "bold"))
        self.var_name.pack(anchor="w")
        self.var_desc = ttk.Label(side, text="", wraplength=380, justify="left")
        self.var_desc.pack(anchor="w", pady=4, fill="x")
        self.var_value = tk.StringVar()
        ent = ttk.Entry(side, textvariable=self.var_value, width=48)
        ent.pack(fill="x", pady=4)
        ent.bind("<Return>", lambda e: self.set_var())
        b = ttk.Frame(side)
        b.pack(fill="x")
        ttk.Button(b, text="Set", command=self.set_var).pack(side="left")
        ttk.Button(b, text="Clear (comment out)", command=lambda: self.set_var(clear=True)).pack(side="left", padx=4)
        ttk.Label(side, text="Values are written quoted, as the shell needs them. New settings reach an MCP server "
                             "when it starts: restart the Claude app (servers over ssh too).",
                  wraplength=380, justify="left", foreground="#555").pack(anchor="w", pady=8)
        self.env_pending = ttk.Label(side, text="", foreground="#a05000", wraplength=380, justify="left")
        self.env_pending.pack(anchor="w")
        sb = ttk.Frame(side)
        sb.pack(fill="x", pady=6)
        ttk.Button(sb, text="Save changes", command=self.save_env).pack(side="left")
        ttk.Button(sb, text="Discard", command=self.load_settings).pack(side="left", padx=4)
        self.env_problems = ttk.Label(side, text="", foreground="#b00020", wraplength=380, justify="left")
        self.env_problems.pack(anchor="w", pady=4)

    def current_env_file(self) -> Path:
        return Path(L.SYSTEM_ENV) if self.file_var.get() == "system" else Path(L.ENV_FILE)

    def load_settings(self):
        self.env_file = self.current_env_file()
        self.pending = {}
        self.env_tree.delete(*self.env_tree.get_children())
        self.env_meta = {}
        for sec, rows in settings_sections(self.env_file):
            set_here = sum(1 for r in rows if r["value"])
            pid = self.env_tree.insert("", "end", text=f"{sec}  [{set_here} set]", open=False)
            for r in rows:
                iid = f"{pid}:{r['var']}"
                self.env_meta[iid] = r
                self.env_tree.insert(pid, "end", iid=iid, text=r["var"], values=(r["value"], r["machine"]))
        probs = L.env_problems(self.env_file) if self.env_file.exists() else []
        writable = os.access(self.env_file if self.env_file.exists() else self.env_file.parent, os.W_OK)
        self.env_problems.configure(text="\n".join(probs) + ("" if writable else f"\n{self.env_file} is not writable "
                                                             f"here (the admin edits it: sudo llmt env --system --edit)"))
        self.env_pending.configure(text="")

    def pick_var(self, _e=None):
        sel = self.env_tree.selection()
        r = self.env_meta.get(sel[0]) if sel else None
        if not r:
            return
        self.var_name.configure(text=r["var"])
        self.var_desc.configure(text=r["desc"] or "(not described in translators.json)")
        self.var_value.set(self.pending.get(r["var"], r["value"]))

    def set_var(self, clear=False):
        sel = self.env_tree.selection()
        r = self.env_meta.get(sel[0]) if sel else None
        if not r:
            return
        v = "" if clear else self.var_value.get().strip()
        self.pending[r["var"]] = v
        for iid, m in self.env_meta.items():
            if m["var"] == r["var"]:
                self.env_tree.set(iid, "value", (v or "(cleared)") + "  *")
        self.env_pending.configure(text="Not saved yet: " + ", ".join(self.pending))

    def save_env(self):
        if not self.pending:
            return
        f = self.current_env_file()
        if not os.access(f if f.exists() else f.parent, os.W_OK):
            messagebox.showerror("Not writable", f"{f} is not writable by you.")
            return
        probs = save_settings(self.pending, f)
        self.say(f"(saved {', '.join(self.pending)} in {f})")
        self.load_settings()
        if probs:
            messagebox.showwarning("Check the file", "\n".join(probs))

    def edit_raw(self):
        f = self.current_env_file()
        RawEditor(self.root, f, on_save=self.load_settings)

    # ================================================================ Machines tab
    def build_machines(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Machines")
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Button(top, text="Refresh", command=self.refresh_machines).pack(side="left")
        self.auto_var = tk.BooleanVar(value=False)
        self.every_var = tk.StringVar(value="5")
        ttk.Checkbutton(top, text="every", variable=self.auto_var, command=self.schedule).pack(side="left", padx=(12, 2))
        ttk.Spinbox(top, from_=1, to=120, width=4, textvariable=self.every_var).pack(side="left")
        ttk.Label(top, text="min").pack(side="left", padx=2)
        self.mach_label = ttk.Label(top, text="")
        self.mach_label.pack(side="left", padx=12)
        ttk.Button(top, text="Machines…", command=self.edit_machines).pack(side="right")

        pw = ttk.PanedWindow(tab, orient="vertical")
        pw.pack(fill="both", expand=True, pady=4)
        f, self.m_tree = self.tree(pw, [("version", "Version / bundle", 210), ("cpus", "SLURM CPUs", 150),
                                        ("jobs", "Translator jobs", 140), ("load", "Load / cores", 90),
                                        ("note", "Note", 380)], height=9, show="tree headings")
        self.m_tree.heading("#0", text="Machine / translator")
        self.m_tree.column("#0", width=170, stretch=False)
        self.m_tree.bind("<<TreeviewSelect>>", self.pick_machine)
        pw.add(f, weight=3)

        lower = ttk.Frame(pw)
        pw.add(lower, weight=3)
        bar = ttk.Frame(lower)
        bar.pack(fill="x")
        self.sel_label = ttk.Label(bar, text="Select a machine", font=("TkDefaultFont", 11, "bold"))
        self.sel_label.pack(side="left")
        ttk.Button(bar, text="List MCP servers", command=self.show_servers).pack(side="right", padx=2)
        ttk.Button(bar, text="Fetch active requests", command=self.fetch_reqs).pack(side="right", padx=2)
        self.mnb = ttk.Notebook(lower)
        self.mnb.pack(fill="both", expand=True)
        f1, self.j_tree = self.tree(self.mnb, [("id", "Job", 70), ("key", "Tr.", 50), ("state", "State", 80),
                                               ("time", "Elapsed / limit", 140), ("cpus", "CPUs", 50),
                                               ("node", "Node", 100), ("study", "Study", 180),
                                               ("request", "Request", 260), ("why", "Reason / start", 200)], height=7)
        self.mnb.add(f1, text="SLURM jobs")
        f2, self.r_tree = self.tree(self.mnb, [("key", "Tr.", 50), ("request", "Request", 300), ("study", "Study", 170),
                                               ("state", "State", 90), ("job", "Job", 60), ("detail", "Detail", 400)],
                                    height=7)
        self.r_tree.bind("<Double-1>", self.copy_request)
        self.mnb.add(f2, text="Active requests")
        f3 = ttk.Frame(self.mnb)
        self.srv_text = tk.Text(f3, height=8, wrap="none", font=("Menlo" if sys.platform == "darwin" else "Monospace", 10))
        self.srv_text.pack(fill="both", expand=True)
        self.mnb.add(f3, text="MCP servers")
        self.after_id = None

    def host(self, name: str) -> dict | None:
        return next((h for h in machines() if h["name"] == name), None)

    def refresh_machines(self):
        hosts = machines()
        self.mach_label.configure(text=f"asking {len(hosts)} machine(s) …")
        self.m_tree.delete(*self.m_tree.get_children())
        for h in hosts:
            self.m_tree.insert("", "end", iid=h["name"], text=h.get("label", h["name"]),
                               values=("…", "", "", "", h.get("ssh") or "here"),
                               open=False)
            self.bg(fetch_machine, self.show_machine, h)

    def show_machine(self, res):
        if isinstance(res, Exception):
            self.say(f"machine status failed: {res}")
            return
        self.results[res["name"]] = res
        if not self.m_tree.exists(res["name"]):
            return
        s = machine_summary(res)
        iid = res["name"]
        for c in self.m_tree.get_children(iid):
            self.m_tree.delete(c)
        if not s["reachable"]:
            self.m_tree.item(iid, values=("unreachable", "", "", "", s.get("error") or ""))
        else:
            self.m_tree.item(iid, values=(f"{s.get('bundle', '')}  (llmt {s.get('llmt')})", s.get("cpus", ""),
                                          s.get("jobs", ""), s.get("load", ""), s.get("note", "")))
            for t in translator_rows(res):
                self.m_tree.insert(iid, "end", iid=f"{iid}/{t['key']}", text=f"  {t['key']}",
                                   values=(f"{t['version']} ({t['commit']})", t["executor"], t["jobs"], "",
                                           (t["note"] + "; " if t["note"] else "") + t["backends"]))
        done = sum(1 for n in self.m_tree.get_children() if n in self.results and
                   self.results[n].get("asked", 0) >= time.time() - STATUS_TIMEOUT - 30)
        self.mach_label.configure(text=f"{done}/{len(self.m_tree.get_children())} answered · "
                                       f"last {_dt.datetime.now():%H:%M:%S}")
        if self.current_machine() == iid:
            self.pick_machine()

    def current_machine(self) -> str | None:
        sel = self.m_tree.selection()
        return sel[0].split("/")[0] if sel else None

    def pick_machine(self, _e=None):
        name = self.current_machine()
        if not name:
            return
        res = self.results.get(name) or {}
        label = (self.host(name) or {}).get("label", name)
        self.sel_label.configure(text=f"{label}" +(f"  ({res.get('seconds')} s)" if res.get("seconds") else ""))
        self.j_tree.delete(*self.j_tree.get_children())
        rows = slurm_job_rows(res)
        for r in rows:
            self.j_tree.insert("", "end", values=(r["id"], r["key"], r["state"], r["time"], r["cpus"], r["node"],
                                                  r["study"], r["request"], r["why"]))
        if not rows:
            sl = (res.get("data") or {}).get("slurm")
            self.j_tree.insert("", "end", values=("", "", "", "", "", "", "no translator jobs" if sl else "no SLURM here",
                                                  "", ""))

    def fetch_reqs(self):
        name = self.current_machine()
        h = self.host(name) if name else None
        if not h:
            messagebox.showinfo("Select", "Select a machine first.")
            return
        keys = [k for k, t in (((self.results.get(name) or {}).get("data") or {}).get("translators") or {}).items()
                if t.get("version")]
        if not keys:
            messagebox.showinfo("Nothing", f"No translator answered on {name} yet (Refresh first).")
            return
        self.r_tree.delete(*self.r_tree.get_children())
        self.r_tree.insert("", "end", values=("", f"asking {', '.join(keys)} on {name} …", "", "", "", ""))
        self.mnb.select(1)
        self.bg(fetch_requests, lambda d: self.show_reqs(name, d), h, keys)

    def show_reqs(self, name, data):
        self.r_tree.delete(*self.r_tree.get_children())
        if isinstance(data, Exception):
            self.r_tree.insert("", "end", values=("", f"failed: {data}", "", "", "", ""))
            return
        for r in request_rows(data):
            self.r_tree.insert("", "end", values=(r["key"], r["request"], r["study"], r["state"], r["job"], r["detail"]))

    def copy_request(self, _e=None):
        sel = self.r_tree.selection()
        if sel:
            req = self.r_tree.item(sel[0], "values")[1]
            self.root.clipboard_clear()
            self.root.clipboard_append(req)
            self.say(f"(copied {req})")

    def show_servers(self):
        name = self.current_machine()
        h = self.host(name) if name else None
        if not h:
            messagebox.showinfo("Select", "Select a machine first.")
            return
        self.srv_text.delete("1.0", "end")
        self.srv_text.insert("end", f"llmt servers on {name} …\n")
        self.mnb.select(2)

        def show(text):
            self.srv_text.delete("1.0", "end")
            self.srv_text.insert("end", str(text) + "\n\n(read-only: on the machine, 'llmt servers --stop' ends "
                                                    "old-release servers and leftover workers)")
        self.bg(fetch_servers, show, h)

    def schedule(self):
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        if self.auto_var.get():
            try:
                every = max(1.0, float(self.every_var.get()))
            except ValueError:
                every = 5.0
            self.after_id = self.root.after(int(every * 60000), self.auto_tick)

    def auto_tick(self):
        self.after_id = None
        self.refresh_machines()
        self.schedule()


# ================================================================ small windows

class Tip:
    """A tooltip."""

    def __init__(self, widget, text: str):
        self.w, self.text, self.top = widget, text, None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _e=None):
        if self.top or not self.text:
            return
        x, y = self.w.winfo_rootx() + 10, self.w.winfo_rooty() + self.w.winfo_height() + 4
        self.top = tk.Toplevel(self.w)
        self.top.wm_overrideredirect(True)
        self.top.wm_geometry(f"+{x}+{y}")
        tk.Label(self.top, text=self.text, background="#ffffe0", relief="solid", borderwidth=1,
                 wraplength=420, justify="left").pack()

    def hide(self, _e=None):
        if self.top:
            self.top.destroy()
            self.top = None


class RawEditor:
    """The settings file as text."""

    def __init__(self, parent, path: Path, on_save):
        self.path, self.on_save = path, on_save
        self.top = tk.Toplevel(parent)
        self.top.title(str(path))
        self.top.geometry("820x560")
        self.text = tk.Text(self.top, wrap="none", font=("Menlo" if sys.platform == "darwin" else "Monospace", 11))
        self.text.pack(fill="both", expand=True)
        try:
            self.text.insert("1.0", path.read_text() if path.exists() else L.env_template())
        except OSError as exc:
            self.text.insert("1.0", f"# cannot read {path}: {exc}\n")
        b = ttk.Frame(self.top, padding=4)
        b.pack(fill="x")
        writable = os.access(path if path.exists() else path.parent, os.W_OK)
        ttk.Button(b, text="Save", command=self.save, state="normal" if writable else "disabled").pack(side="right")
        ttk.Button(b, text="Close", command=self.top.destroy).pack(side="right", padx=4)
        if not writable:
            ttk.Label(b, text="read-only for you").pack(side="left")

    def save(self):
        text = self.text.get("1.0", "end").rstrip("\n") + "\n"
        tmp = self.path.with_suffix(".tmp-llmt-gui")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text)
            os.replace(tmp, self.path)
        except OSError as exc:
            messagebox.showerror("Not saved", str(exc), parent=self.top)
            return
        probs = L.env_problems(self.path)
        if probs:
            messagebox.showwarning("Check the file", "\n".join(probs), parent=self.top)
        self.on_save()
        self.top.destroy()


class MachinesDialog:
    """The router's machines (config/setup.json: router.hosts): name, ssh user@host, llm-translators folder there."""

    def __init__(self, parent, on_save):
        self.on_save = on_save
        self.top = tk.Toplevel(parent)
        self.top.title("Machines (the router's, config/setup.json)")
        self.top.geometry("760x360")
        cols = [("name", "Name", 110), ("ssh", "ssh (user@host)", 260), ("root", "llm-translators folder there", 330)]
        f = ttk.Frame(self.top)
        f.pack(fill="both", expand=True, padx=6, pady=6)
        self.t = ttk.Treeview(f, columns=[c for c, _, _ in cols], show="headings", height=8)
        for c, label, w in cols:
            self.t.heading(c, text=label)
            self.t.column(c, width=w)
        self.t.pack(fill="both", expand=True)
        self.t.bind("<<TreeviewSelect>>", self.pick)
        for h in (L.load_setup().get("router") or {}).get("hosts", []):
            self.t.insert("", "end", values=(h.get("name", ""), h.get("ssh", ""), h.get("root", "")))
        e = ttk.Frame(self.top, padding=6)
        e.pack(fill="x")
        self.vars = [tk.StringVar() for _ in cols]
        for (c, label, w), v in zip(cols, self.vars):
            ttk.Label(e, text=label.split(" (")[0]).pack(side="left")
            ttk.Entry(e, textvariable=v, width=max(10, w // 10)).pack(side="left", padx=(2, 8))
        self.vars[2].set("/opt/llm-translators/current")
        b = ttk.Frame(self.top, padding=6)
        b.pack(fill="x")
        ttk.Button(b, text="Add / update", command=self.put).pack(side="left")
        ttk.Button(b, text="Remove", command=self.remove).pack(side="left", padx=4)
        ttk.Button(b, text="Save", command=self.save).pack(side="right")
        ttk.Button(b, text="Cancel", command=self.top.destroy).pack(side="right", padx=4)
        ttk.Label(self.top, text="The router reads these when it starts: restart the Claude app after saving. "
                                 "'llmt router setup' checks each machine too.", foreground="#555").pack(pady=(0, 6))

    def pick(self, _e=None):
        sel = self.t.selection()
        if sel:
            for v, x in zip(self.vars, self.t.item(sel[0], "values")):
                v.set(x)

    def put(self):
        name, ssh, root = (v.get().strip() for v in self.vars)
        if not name or not root:
            messagebox.showinfo("Missing", "A machine needs a name and its folder.", parent=self.top)
            return
        for iid in self.t.get_children():
            if self.t.item(iid, "values")[0] == name:
                self.t.item(iid, values=(name, ssh, root))
                return
        self.t.insert("", "end", values=(name, ssh, root))

    def remove(self):
        for iid in self.t.selection():
            self.t.delete(iid)

    def save(self):
        setup = L.load_setup()
        r = setup.setdefault("router", {})
        r["hosts"] = [{"name": a, "ssh": b, "root": c} for a, b, c in (self.t.item(i, "values") for i in self.t.get_children())]
        L.save_setup(setup)
        self.on_save()
        self.top.destroy()


def main() -> None:
    if tk is None:
        sys.exit("llmt gui needs tkinter. macOS: use the python.org Python, or 'brew install python-tk@3.x' for "
                 "Homebrew's; Linux: 'sudo apt install python3-tk'.")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        sys.exit(f"llmt gui: no display ({exc})")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
