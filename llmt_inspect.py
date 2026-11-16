"""llmt_inspect.py: the router's read-only 'inspect' tool (loaded by llmt_router.py; written in the cfdt chat).
Version: 2026.11.16.2 (grep: each file resolved and kept inside the roots; numbers checked on the router's side)

inspect(machine, action, ...) looks at hand-run work on a machine without a translator's server:
  jobs   the user's SLURM jobs (all of them, not only a translator's): squeue now, sacct for the last `days`
  roots  the folders this machine allows (the configured patterns, expanded there)
  list   a folder under an allowed root (name, type, size, mtime; `depth` 1-3)
  read   a text file: `lines` lines from line `start` (1-based)
  tail   a text file's last `lines` lines
  grep   a regular expression in a file, or in the files of a folder matching `files` (a glob, e.g. "log.*")

Read-only by construction: the code sent to the machine is the constant REMOTE below; the call's values reach it
only as one JSON argument (shlex-quoted), never as shell text. The allowed roots come from the router's config
(setup.json router.inspect.roots, a host's own inspect_roots replacing them), never from the call. Every path is
resolved on the machine (symlinks included) and must lie inside a resolved root; only regular files and folders are
opened (a FIFO would block), only for reading; names that look like secrets are refused; output and time are capped.
Python 3.6 or newer on the machine; standard library only on both sides.
"""
from __future__ import annotations

import json
import shlex

__version__ = "2026.11.16.2"

DEFAULT_ROOTS = ["~/s50_*", "~/ncc_ladder", "~/v2506_ring_sample"]
ACTIONS = ("jobs", "roots", "list", "read", "tail", "grep")
REMOTE_TIMEOUT = 30.0

TOOL = {
    "name": "inspect",
    "description": (
        "Read-only look at a machine outside the translator: action 'jobs' lists all of the user's SLURM jobs there "
        "(queued and running, and those that ended in the last `days`, 2 by default), with name, state, times, "
        "nodes, work folder and exit code; 'roots' lists the folders that may be read; 'list' a folder, 'read' or "
        "'tail' a text file, 'grep' a regular expression in a file or a folder's files (`files`: a glob such as "
        "'log.*'). Only under the allowed folders (hand-run cases such as ~/s50_*), nothing is changed. Paths: "
        "absolute, '~/...', or relative to the home folder."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "machine": {"type": "string", "description": "the machine (machines() lists them)"},
            "action": {"type": "string", "enum": list(ACTIONS)},
            "path": {"type": "string", "description": "file or folder (list, read, tail, grep)"},
            "lines": {"type": "integer", "description": "read/tail: lines to return (read 200, tail 50; at most 2000)"},
            "start": {"type": "integer", "description": "read: first line, 1-based (1)"},
            "pattern": {"type": "string", "description": "grep: a Python regular expression"},
            "files": {"type": "string", "description": "grep in a folder: which files (a glob, '*' by default)"},
            "depth": {"type": "integer", "description": "list: levels below the folder (1; at most 3)"},
            "days": {"type": "integer", "description": "jobs: ended jobs of the last days (2; at most 14)"},
            "user": {"type": "string", "description": "jobs: whose jobs (the ssh user by default)"},
        },
        "required": ["machine", "action"],
    },
}

# ---------------------------------------------------------------------------------------------------- on the machine
REMOTE = r'''
import fnmatch, glob, json, os, re, stat, subprocess, sys, time
a = json.loads(sys.argv[1])
T0 = time.time(); BUDGET = 20.0; OUT_MAX = 200000; FILE_MAX = 50 * 1024 * 1024
DENY = re.compile(r"(^|/)(\.ssh|\.gnupg|\.netrc|\.aws|\.config/gcloud|id_[^/]*|[^/]*\.(pem|key|p12))(/|$)")
def out(d):
    s = json.dumps(d)
    if len(s) > OUT_MAX:
        d = {"error": "answer larger than %d characters: ask for fewer lines, a narrower pattern or a smaller depth" % OUT_MAX}
        s = json.dumps(d)
    sys.stdout.write(s); sys.exit(0)
def fail(msg): out({"error": msg})
roots = []
for pat in a.get("roots") or []:
    for m in sorted(glob.glob(os.path.expanduser(pat))):
        r = os.path.realpath(m)
        if os.path.isdir(r) and r not in roots and not DENY.search(r):
            roots.append(r)
act = a.get("action")
if act == "roots":
    out({"patterns": a.get("roots"), "roots": roots})
if act == "jobs":
    user = a.get("user") or os.environ.get("USER") or ""
    if not re.match(r"^[A-Za-z0-9_.-]{1,64}$", user):
        fail("bad user name")
    days = max(1, min(int(a.get("days") or 2), 14))
    def run(argv):
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            o, e = p.communicate(timeout=15)
            return p.returncode, o, e
        except OSError as exc:
            return 127, "", str(exc)
        except subprocess.TimeoutExpired:
            p.kill(); return 124, "", "timed out"
    f1 = ["job", "name", "state", "elapsed", "time_limit", "nodes", "cpus", "where_or_why", "submitted", "start", "workdir"]
    rc, o, e = run(["squeue", "-h", "-u", user, "-o", "%i|%j|%T|%M|%l|%D|%C|%R|%V|%S|%Z"])
    if rc == 127:
        fail("no SLURM here (squeue not found)")
    queue = [dict(zip(f1, l.split("|", len(f1) - 1))) for l in o.splitlines() if l.strip()]
    f2 = ["job", "name", "state", "elapsed", "start", "end", "exit", "nodes", "cpus", "workdir"]
    rc2, o2, e2 = run(["sacct", "-u", user, "-X", "-n", "-P", "-S", "now-%ddays" % days,
                       "--format=JobID,JobName%80,State,Elapsed,Start,End,ExitCode,NodeList,AllocCPUS,WorkDir"])
    if rc2 != 0:   # older sacct without WorkDir
        f2 = f2[:-1]
        rc2, o2, e2 = run(["sacct", "-u", user, "-X", "-n", "-P", "-S", "now-%ddays" % days,
                           "--format=JobID,JobName%80,State,Elapsed,Start,End,ExitCode,NodeList,AllocCPUS"])
    live = set(q["job"] for q in queue)
    ended = [dict(zip(f2, l.split("|", len(f2) - 1))) for l in o2.splitlines() if l.strip()]
    ended = [j for j in ended if j["job"] not in live][-200:]
    out({"user": user, "queue": queue, "ended": ended, "days": days,
         "sacct_error": (e2.strip()[-300:] if rc2 != 0 else None)})
path = a.get("path") or ""
if not path:
    fail("give 'path'")
p = os.path.expanduser(path)
if not os.path.isabs(p):
    p = os.path.join(os.path.expanduser("~"), p)
p = os.path.realpath(p)
def inside(q):
    return any(q == r or q.startswith(r.rstrip("/") + "/") for r in roots)
if not inside(p):
    fail("%s is outside the folders this machine allows: %s" % (p, ", ".join(roots) or "none (router.inspect.roots)"))
if DENY.search(p):
    fail("refused: the name looks like a secret")
try:
    st = os.stat(p)
except OSError as exc:
    fail("%s: %s" % (p, exc.strerror))
def entry(q, s):
    kind = "dir" if stat.S_ISDIR(s.st_mode) else "file" if stat.S_ISREG(s.st_mode) else "other"
    return {"path": q, "type": kind, "size": s.st_size,
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.st_mtime))}
def text_open(q):
    s = os.stat(q)
    if not stat.S_ISREG(s.st_mode):
        fail("%s is not a regular file" % q)
    f = open(q, "rb")
    if b"\0" in f.read(8192):
        f.close(); fail("%s is a binary file (%d bytes)" % (q, s.st_size))
    f.seek(0); return f, s
def dec(b):
    return b.decode("utf-8", "replace").rstrip("\n")
if act == "list":
    if not stat.S_ISDIR(st.st_mode):
        out({"entries": [entry(p, st)]})
    depth = max(1, min(int(a.get("depth") or 1), 3)); ents = []
    base = p.rstrip("/").count("/")
    for dp, dns, fns in os.walk(p):
        dns[:] = sorted(d for d in dns if not DENY.search(os.path.join(dp, d)))
        if dp.count("/") - base >= depth:
            dns[:] = []
        for n in sorted(dns) + sorted(fns):
            q = os.path.join(dp, n)
            if DENY.search(q) or (dp.count("/") - base >= depth):
                continue
            try:
                ents.append(entry(os.path.relpath(q, p), os.lstat(q)))
            except OSError:
                pass
            if len(ents) >= 1000:
                out({"folder": p, "entries": ents, "truncated": True})
    out({"folder": p, "entries": ents})
if act in ("read", "tail"):
    n = max(1, min(int(a.get("lines") or (200 if act == "read" else 50)), 2000))
    f, s = text_open(p)
    if act == "tail":
        size = s.st_size; block = 65536; data = b""; pos = size
        while pos > 0 and data.count(b"\n") <= n:
            k = min(block, pos); pos -= k; f.seek(pos); data = f.read(k) + data
        lines = data.split(b"\n")
        if lines and lines[-1] == b"":
            lines = lines[:-1]
        out({"path": p, "size": size, "mtime": entry(p, s)["mtime"], "lines": [dec(l) for l in lines[-n:]]})
    start = max(1, int(a.get("start") or 1)); got = []; i = 0
    for i, l in enumerate(f, 1):
        if i >= start:
            got.append(dec(l))
            if len(got) >= n:
                break
        if time.time() - T0 > BUDGET:
            break
    out({"path": p, "size": s.st_size, "mtime": entry(p, s)["mtime"], "start": start, "lines": got,
         "more": len(got) >= n})
if act == "grep":
    try:
        rx = re.compile(a.get("pattern") or "")
    except re.error as exc:
        fail("pattern: %s" % exc)
    if not a.get("pattern"):
        fail("give 'pattern'")
    if stat.S_ISDIR(st.st_mode):
        g = a.get("files") or "*"; files = []
        for dp, dns, fns in os.walk(p):
            dns[:] = sorted(d for d in dns if not DENY.search(os.path.join(dp, d)))
            files += [os.path.join(dp, n) for n in sorted(fns) if fnmatch.fnmatch(n, g)]
            if len(files) > 200:
                break
    else:
        files = [p]
    hits = []; scanned = 0; skipped = []; stop = None
    for q in files[:200]:
        if DENY.search(q):
            continue
        rq = os.path.realpath(q)            # a symlinked file in the folder may point anywhere: resolved, kept inside
        if not inside(rq) or DENY.search(rq):
            continue
        try:
            s = os.stat(rq)
        except OSError:
            continue
        if not stat.S_ISREG(s.st_mode):
            continue
        if scanned + s.st_size > FILE_MAX:
            skipped.append(q); continue
        with open(rq, "rb") as f:
            if b"\0" in f.read(8192):
                continue
            f.seek(0); scanned += s.st_size
            for i, l in enumerate(f, 1):
                t = dec(l)
                if rx.search(t):
                    hits.append({"path": os.path.relpath(q, p) if q != p else os.path.basename(q), "line": i,
                                 "text": t[:500]})
                    if len(hits) >= 300:
                        stop = "300 matches"; break
                if i % 5000 == 0 and time.time() - T0 > BUDGET:
                    stop = "time"; break
        if stop:
            break
    out({"path": p, "files": len(files), "matches": hits, "stopped_at": stop,
         "skipped_over_50MB": skipped[:20]})
fail("action must be one of jobs, roots, list, read, tail, grep")
'''


def roots_for(host: dict, config: dict) -> list:
    """The allowed root patterns for a host: its own 'inspect_roots', else router.inspect.roots, else the defaults."""
    if isinstance(host.get("inspect_roots"), list):
        return [str(r) for r in host["inspect_roots"]]
    r = ((config or {}).get("inspect") or {}).get("roots")
    return [str(x) for x in r] if isinstance(r, list) else list(DEFAULT_ROOTS)


def command(args: dict, roots: list) -> str:
    """The shell command for the machine: python3 running the constant REMOTE with the call as one JSON argument."""
    act = args.get("action")
    if act not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    keep = {k: args[k] for k in ("action", "path", "lines", "start", "pattern", "files", "depth", "days", "user")
            if args.get(k) is not None}
    for k in ("lines", "start", "depth", "days"):
        if k in keep:
            try:
                keep[k] = int(keep[k])
            except (TypeError, ValueError):
                raise ValueError(f"{k}: a whole number, not {keep[k]!r}") from None
    for k in ("path", "pattern", "files", "user"):
        if k in keep and not isinstance(keep[k], str):
            raise ValueError(f"{k}: a string")
    keep["roots"] = roots
    return f"python3 -c {shlex.quote(REMOTE)} {shlex.quote(json.dumps(keep))}"


def run(remote_cmd, host: dict, config: dict, args: dict) -> dict:
    """One inspect call: remote_cmd(host, script, timeout) is the router's (ssh, or sh here)."""
    p = remote_cmd(host, command(args, roots_for(host, config)), timeout=REMOTE_TIMEOUT)
    text = (p.stdout or "").strip()
    try:
        res = json.loads(text)
    except json.JSONDecodeError:
        return {"machine": host.get("name"), "error": f"no answer from the machine (exit {p.returncode}): "
                f"{(p.stderr or text).strip()[-300:]}"}
    res["machine"] = host.get("name")
    return res
