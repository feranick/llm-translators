#!/usr/bin/env bash
# llmt-check.sh — what is running on each machine of llm-translators, from the Mac.
#
#   llmt-check.sh                         the router's machines (config/setup.json of your llm-translators)
#   llmt-check.sh mochi carbonio          only these (names from the router's list)
#   llmt-check.sh nicola@host:/opt/llm-translators/current     any machine, user@host[:llm-translators folder]
#
# For each machine (in parallel, over ssh with your key):
#   - the llm-translators install: shared release (and which one is current) or personal checkout;
#   - the version of each translator installed there;
#   - the translator MCP servers running (any user): pid, user, age, the release they run from and the
#     translator version that release has, flagged OLD when it is not what is installed now;
#   - the translators' SLURM jobs (any user): id, user, state, time, node, and for your own jobs the version
#     they run (from the job's script), flagged OLD likewise;
#   - SLURM nodes: state and CPUs in use.
# Then, on this Mac: the router processes (one per translator, started by the Claude app) and the router
# version they run, against the one in your checkout (restart the Claude app when they differ).
#
# Needs: bash, ssh, python3 here and on the machines. Changes nothing anywhere.
# Options: -r PATH  your llm-translators checkout (default ~/Software/llm-translators); -h help.

set -u
REPO="${LLMT_HOME:-$HOME/Software/llm-translators}"
while getopts "r:h" opt; do
    case "$opt" in
        r) REPO="$OPTARG" ;;
        h) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) exit 2 ;;
    esac
done
shift $((OPTIND - 1))

# ---------------------------------------------------------------- the machines
SETUP="$REPO/config/setup.json"
[ -f "$SETUP" ] || SETUP="${XDG_CONFIG_HOME:-$HOME/.config}/llm-translators/setup.json"
HOSTS=$(python3 - "$SETUP" "$@" <<'PY'
import json, sys
setup, args = sys.argv[1], sys.argv[2:]
try:
    hosts = (json.load(open(setup)).get("router") or {}).get("hosts", [])
except (OSError, ValueError):
    hosts = []
known = {h["name"]: h for h in hosts}
out = []
if not args:
    out = [(h["name"], h.get("ssh", ""), h.get("root", "")) for h in hosts]
for a in args:
    if a in known:
        h = known[a]; out.append((a, h.get("ssh", ""), h.get("root", "")))
    else:
        ssh, _, root = a.partition(":")
        out.append((ssh.split("@")[-1].split(".")[0], ssh, root or "/opt/llm-translators/current"))
for n, s, r in out:
    print(f"{n}\t{s or '-'}\t{r or '/opt/llm-translators/current'}")   # '-': this machine (no empty fields)
PY
)
if [ -z "$HOSTS" ]; then
    echo "No machines: give user@host[:folder], or set up the router (llmt router setup) in $REPO" >&2
    exit 2
fi

# ---------------------------------------------------------------- what runs on a machine (python3 there)
read -r -d '' REMOTE <<'PY'
import glob, json, os, re, socket, subprocess, sys, time

root_arg = os.path.expanduser(sys.argv[1])
real = os.path.realpath(root_arg)
out = []
say = out.append

def version_in(root, repo, package):
    try:
        m = re.search(r"__version__\s*=\s*[\"']([^\"']+)", open(os.path.join(root, repo, package, "__init__.py")).read())
        return m.group(1) if m else None
    except OSError:
        return None

try:
    reg = json.load(open(os.path.join(real, "translators.json")))["translators"]
except (OSError, ValueError, KeyError):
    print(f"  ! no llm-translators at {root_arg}")
    sys.exit(0)
cli_to_key = {t["cli"]: k for k, t in reg.items()}
release = None
if os.path.exists(os.path.join(real, "RELEASE.json")):
    rel = json.load(open(os.path.join(real, "RELEASE.json")))
    release = rel.get("release")
    prefix = os.path.dirname(os.path.dirname(real))
    cur = os.path.realpath(os.path.join(prefix, "current"))
    olds = sorted(os.path.basename(p) for p in glob.glob(os.path.join(prefix, "releases", "*"))
                  if os.path.exists(os.path.join(p, "RELEASE.json")) and os.path.realpath(p) != cur)
    say(f"  llm-translators: shared release {release} (current; built {rel.get('created', '?')} by "
        f"{rel.get('built_by', '?')}); older releases: {', '.join(olds) or 'none'}")
else:
    say(f"  llm-translators: personal checkout {real}")

installed = {}
for k, t in reg.items():
    v = version_in(real, t["repo"], t["package"])
    if v and os.path.isdir(os.path.join(real, ".venv")):
        installed[k] = v
say("  installed: " + (", ".join(f"{k} {v}" for k, v in installed.items()) or "none"))

def root_version(root, key):
    t = reg.get(key)
    return version_in(root, t["repo"], t["package"]) if t else None

def label(root, key):
    """'release X' or 'checkout PATH', its version, and OLD when it differs from what is installed now."""
    v = root_version(root, key)
    rj = os.path.join(root, "RELEASE.json")
    where = ("release " + json.load(open(rj)).get("release", "?")) if os.path.exists(rj) else f"checkout {root}"
    old = (root != real and os.path.exists(rj)) or (v and installed.get(key) and v != installed.get(key))
    return f"{where}, {key} {v or '?'}" + ("   OLD" if old else "")

# ---- MCP servers (the first two words of each command line: program or script <root>/.venv/bin/<key>-mcp)
hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
try:
    uptime = float(open("/proc/uptime").read().split()[0])
except OSError:
    uptime = None

def age(pid):
    try:
        start = int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[19]) / hz
        s = int(uptime - start)
        return f"{s // 86400}d{s % 86400 // 3600:02d}h" if s >= 86400 else f"{s // 3600}h{s % 3600 // 60:02d}m"
    except (OSError, ValueError, IndexError, TypeError):
        return "?"

def owner(pid):
    try:
        import pwd
        uid = int(next(l.split()[1] for l in open(f"/proc/{pid}/status") if l.startswith("Uid:")))
        return pwd.getpwuid(uid).pw_name
    except Exception:
        return "?"

me = os.getpid()
servers = []
for d in glob.glob("/proc/[0-9]*"):
    pid = int(os.path.basename(d))
    if pid == me:
        continue
    try:
        argv = [x.decode(errors="replace") for x in open(f"{d}/cmdline", "rb").read().split(b"\0")[:2] if x]
    except OSError:
        continue
    for w in argv:
        if not w.startswith("/"):
            continue
        m = re.match(r"^(.+)/\.venv/bin/([a-z0-9_]+)-mcp$", os.path.realpath(w))
        if m and m.group(2) in reg:
            servers.append((m.group(2), owner(pid), pid, age(pid), label(m.group(1), m.group(2))))
            break
if not os.path.isdir("/proc"):
    say("  MCP servers: (no /proc here)")
elif servers:
    say(f"  MCP servers ({len(servers)}):")
    for key, user, pid, a, lab in sorted(servers):
        say(f"    {key:5} {user:10} pid {pid:<8} up {a:>8}  {lab}")
else:
    say("  MCP servers: none running")

# ---- the translators' SLURM jobs
def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
if subprocess.run(["sh", "-c", "command -v squeue >/dev/null"]).returncode == 0:
    jobs = []
    for line in run(["squeue", "-h", "-o", "%i|%u|%j|%T|%M|%N|%o"]).splitlines():
        f = (line.split("|") + [""] * 7)[:7]
        cli = f[2].split(":", 1)[0]
        if cli not in cli_to_key:
            continue
        key = cli_to_key[cli]
        runs = "?"
        try:
            m = re.search(r"(/\S+?)/\.venv/bin/python\S*", open(f[6]).read())
            if m:
                runs = label(os.path.realpath(m.group(1)), key)
        except OSError:
            pass
        study = f[2].split(":")[-1]
        jobs.append(f"    {f[0]:>7} {key:5} {f[1]:10} {f[3]:9} {f[4]:>9} {f[5] or '-':12} {study:14} {runs}")
    say(f"  SLURM jobs of the translators ({len(jobs)}):" if jobs else "  SLURM jobs of the translators: none")
    out.extend(jobs)
    nodes = []
    for line in run(["scontrol", "-o", "show", "node"]).splitlines():
        f = dict(kv.split("=", 1) for kv in line.split() if "=" in kv)
        if "NodeName" in f:
            nodes.append(f"{f['NodeName']} {f.get('State', '?').lower()} {f.get('CPUAlloc', '?')}/{f.get('CPUTot', '?')} CPUs")
    say("  SLURM nodes: " + ("; ".join(nodes) if nodes else "scontrol gave no answer (is slurmctld running?)"))
else:
    say("  SLURM: not installed")
print("\n".join(out))
PY

# ---------------------------------------------------------------- run on every machine at once
TMP=$(mktemp -d "${TMPDIR:-/tmp}/llmt-check.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
i=0
while IFS=$'\t' read -r name ssh root; do
    [ "$ssh" = "-" ] && ssh=""
    i=$((i + 1))
    f="$TMP/$(printf %03d $i)"
    printf '%s\t%s\t%s\n' "$name" "${ssh:--}" "$root" > "$f.head"
    if [ -n "$ssh" ]; then
        ssh -o BatchMode=yes -o ConnectTimeout=10 "$ssh" "python3 - $(printf %q "$root")" <<<"$REMOTE" > "$f.out" 2>&1 &
    else
        python3 - "$root" <<<"$REMOTE" > "$f.out" 2>&1 &
    fi
done <<<"$HOSTS"
wait

for h in "$TMP"/*.head; do
    IFS=$'\t' read -r name ssh root < "$h"
    [ "$ssh" = "-" ] && ssh="this machine"
    echo "== $name ($ssh:$root)"
    o="${h%.head}.out"
    if [ -s "$o" ]; then
        sed 's/^ssh:/  ! ssh:/;s/^Warning:/  ! Warning:/' "$o"
    else
        echo "  ! no answer"
    fi
    echo
done

# ---------------------------------------------------------------- the routers on this Mac
echo "== this Mac: routers (started by the Claude app)"
want=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/llmt_router.py" 2>/dev/null)
found=0
while read -r pid etime rest; do
    [ -n "$pid" ] || continue
    found=1
    script=$(printf '%s\n' "$rest" | grep -o '[^ ]*llmt_router\.py' | head -1)
    key=$(printf '%s\n' "$rest" | sed 's/.*llmt_router\.py *//' | cut -d' ' -f1)
    started=$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/  */ /g')
    mod=$(stat -c %Y "$script" 2>/dev/null || stat -f %m "$script" 2>/dev/null)
    case "$mod" in *[!0-9]*|"") mod="" ;; esac
    st=$(date -j -f "%a %b %d %T %Y" "$started" +%s 2>/dev/null || date -d "$started" +%s 2>/dev/null)
    case "$st" in *[!0-9]*|"") st="" ;; esac
    note=""
    if [ -n "$mod" ] && [ -n "$st" ] && [ "$mod" -gt "$st" ]; then
        note="   OLD: llmt_router.py changed after it started (now $want): restart the Claude app"
    fi
    printf '  %-5s pid %-7s up %10s%s\n' "${key:-?}" "$pid" "$etime" "$note"
done < <(ps -axo pid=,etime=,command= 2>/dev/null | grep -E '^ *[0-9]+ +[0-9:-]+ +[^ ]*python[^ ]* +[^ ]*llmt_router\.py +[a-z0-9_]+ *$')
[ "$found" = 1 ] || echo "  none running (the Claude app starts them; entries on [a]uto use the router)"
echo "  router in your checkout: ${want:-?} ($REPO)"
