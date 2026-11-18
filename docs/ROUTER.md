# The router: one translator, several machines

Status: design and implementation, llmt 2026.10.22.3; time limits 2026.10.23.1; servers on an old release 2026.10.25.2; files on this computer 2026.10.31.1 (default output folder 2026.10.31.2); tool definitions that follow deploys, per-node queues, request names 2026.11.06.1; init_from across machines 2026.11.13.1 (copies judged by the fields' time 2026.11.13.2); run.host checked study by study, memory per study with its reason 2026.11.14.1; inspect 2026.11.16.1; run.after placement, 'together' chains as one block, cancel at='job_end', start times with their offset 2026.11.18.1.

Until now, each MCP entry in the Claude app pointed to one machine (`ssh mochi .../bin/mcp cfdt`). With several machines able to run a translator (mochi, carbonio, a DGX Spark), choosing one by hand wastes the others, and one entry per machine (`cfdt-mochi`, `cfdt-carbonio`, ...) multiplies the tools in every chat.

The **router** is one MCP server per translator, running on the client (the Mac). The Claude app sees a single `cfdt`, with cfdt's own tools plus `machines()`. Behind it, the router talks to cfdt's real server on each machine over ssh. It does three things:

- It places each run on the machine where it should finish first.
- It sends follow-up calls (status, cancel, files, pictures) to the machine that holds the run.
- It combines the per-machine views (`jobs`).

The translators need no change to work behind it. One small addition to their contract, estimates in `validate_studies` (below), makes placement smarter.

## How it works

```
Claude app ── stdio ──> bin/router cfdt  (on the Mac; Python standard library only)
                          ├── ssh mochi    <root>/bin/mcp cfdt   (cfdt's own server, kept open)
                          ├── ssh carbonio <root>/bin/mcp cfdt
                          └── local        <root>/bin/mcp cfdt   (a machine can also be this one)
```

- **A protocol relay.** The router speaks MCP's stdio protocol (JSON-RPC, one message per line) both ways. It is written with the Python standard library, so it doesn't depend on the MCP SDK version, and it can run where the venv has no `mcp` package.
- **Connections.** The connection to each machine opens on first use and stays open. If a machine drops, its next call reconnects. The router's other ssh calls (status, queue estimates) share one connection per host through ssh's ControlMaster, so they cost milliseconds.
- **Tools and instructions.** The router adds `machines()` to the tools, and one paragraph about the machines to the instructions.
  - **The tool definitions come from the machine with the newest version.** At start, the router asks every machine for its status and reads `tools/list` from one with the newest translator version.
  - **They follow deploys.** A client drops every argument its definitions lack, so stale definitions break new features (on 2026-11-06, `add_geometry(stl=...)` arrived with no geometry). Whenever a status or a server shows a newer version, the router reads `tools/list` again from a new server on that machine. If the definitions changed, it sends `notifications/tools/list_changed`; the router declares `listChanged: true`. Clients that support the notification fetch the list again; others need a reconnect.
  - **The instructions** stay as they were when the client connected.
  - **Arguments pass through** as the client sends them, whatever the stored definitions say.

## Machines and what the router learns about them

**You list the machines once,** with `llmt router setup` or in `setup.json`:

```json
"router": {
  "hosts": [{"name": "mochi",    "ssh": "nicola@mochi.mit.edu",    "root": "/opt/llm-translators/current"},
            {"name": "carbonio", "ssh": "nicola@carbonio.mit.edu", "root": "/opt/llm-translators/current"},
            {"name": "mac",      "ssh": "",                        "root": "/Users/feranick/Software/llm-translators"}],
  "prefer": ["mochi"], "exclude": [], "speed": {"spark": 0.8}, "small_minutes": 5
}
```

`ssh: ""` means this machine. `root` is a personal checkout or a shared release (docs/SHARED.md).

**Everything else the router finds out by itself.** It runs `<root>/llmt status --json KEY` and reads `/proc/loadavg` on each machine, and keeps the answer for 60 seconds. From that it learns:

- whether the translator is installed there, its version, and whether its backends are found;
- how it runs (local, or SLURM jobs), its default ranks per study, its timeout and its caps;
- with SLURM: the machine's CPUs, how many are in use, and the queued and running jobs;
- otherwise: the load average and the number of cores.

**Which machines can take a run:** those that answer and have the translator with its backends, minus `exclude`.

## Placement

Placement applies to calls that start work: those with a `studies` or `goals` argument (`run_studies`, `build_parts`), and `run_benchmarks`. The steps:

1. **A manual choice wins, and is never overridden silently.** A study may say `"run": {"host": "mochi"}`, or a list in order of preference. The router removes `host` before passing the study on. One request runs on one machine (2026.11.14.1):
   - **Each study keeps its own `run.host`.** Before 2026.11.14.1, the router merged every study's `run.host` into one list for all of them, so a mixed request went to the first machine of that list.
   - **Every `run.host` must allow the same machine.** Preference lists may overlap: the request goes to their common machines, in the first study's order. If they share none, the call is refused before anything is validated, placed or copied. The answer says which studies want which machines (e.g. `a -> carbonio; b -> spark1`) and suggests one request per machine. `validate_studies` is refused the same way.
   - **Studies without `run.host`** go wherever the request goes. If one of them excludes every machine the others' `run.host` allows (memory, below), the call is refused. The answer names the requirement (e.g. "r2_g0p45_plain_sl needs ~40 GB (its estimate), which excludes carbonio (its largest job: 15G)") and suggests a separate request or another `run.host`.

   **Memory comes next** (2026.11.07.1; per study 2026.11.14.1). Each study's needs are compared with each machine's largest job memory. That is the cap, not free memory: `executor.caps.mem_mb` in its status (else `caps.mem` parsed), either the node's SLURM memory or `*_MAX_MEM`, shown as `max_job_mem` in `machines()`. A machine whose memory is in use now is not left out; its queue shows in the start estimate.
   - **Its `run.mem`,** read as SLURM reads `--mem` (K, M, G, T; M without a suffix; `"auto"` and unset are ignored). This is checked before anything is validated or copied.
   - **The memory its validation estimates** on that machine (`estimate.mem_mb`), after validation.
   - **The answer says why.** A machine below a study's need is left out, and `router.warnings` says which study excludes it: "carbonio left out: r2_g0p45_plain_sl needs ~40 GB (its estimate), which excludes carbonio (its largest job: 15G)".
   - **No limit reported:** the machine stays a candidate (an older version, or memory unknown).
   - **Nothing fits:** if no allowed machine is left, the call fails with each study's reason, before any upload or submission. After validation (an estimate), the answer adds that only small input files (under 25 MB) may already have been copied to the machines it was validated on (2026.11.14.2).
   - `machines()` shows the limit as `max_job_mem`. cfdt 2026.11.07.1 reports it from the SLURM nodes' memory (or `CFDT_MAX_MEM` if lower); the other translators report it when their `*_MAX_MEM` is set.
   - With llmt-runtime the node's memory is given exactly: `caps.mem` is `64000M` rather than `62G` (a whole number of GB only when it is one), so a `run.mem` of 64000M is no longer refused; `caps.mem_mb` is the same number. `machines()` also shows the translator's `runtime` (llmt-runtime version) and the release's `runtime_commit`.
2. **Data stays where it is.** A study that mentions an earlier request folder, such as a geometry `{"ref": "<request>/<study>"}` or a chained path, goes to the machine holding that folder. The router knows where each request folder lives (below).
3. **Validation on every candidate machine,** in parallel. `validate_studies` / `validate_goals` is static and fast. A machine whose version rejects the request (a newer feature, say) is dropped, if another one accepts it. The validation also returns each study's **estimate**, where the translator provides one: `{"minutes", "procs", "mem_mb"}` (see "Contract", below).
4. **A score for each candidate,** in minutes; the lowest wins:

   ```
   score = expected start + estimated minutes × speed(host) − 0.5 × (preference bonus)
   ```

   - **Expected start, with SLURM:** SLURM's own answer for a job of that size: `sbatch --test-only -n <procs> -t <minutes> --mem <mem>`. Threaded translators ask for `-c <procs>` instead. This counts everything ahead in the queue, other users' jobs included, and backfill. A start 300 days or more ahead is SLURM's placeholder when it cannot plan the job (behind a job with no time limit, queued or running on the node: carbonio on 2026-11-10 gave 2027-11-09). The router reads it as no start time: "SLURM gives no start time: a job ahead of it (or running on the node) has no time limit; it starts when that one ends". The machine is then a last resort, and `machines()` shows `typical_job_start_min: null` (router 2026.11.10.1).
   - **Expected start, without SLURM:** the load. `(load / cores − 0.7) × 30` minutes when the load is above 70 % of the cores, else 0.
   - **Estimated minutes:** from the study's estimate. Without one, only the start counts.
   - **Small jobs** (estimate below `small_minutes`, default 5) count only the start: they go where they can start now.
   - **Speed** is a factor per machine (default 1). Set it by hand for now; later it can be learned from the `execution` records of past runs.
   - **Preference bonus:** machines in `prefer` win ties.
5. **Different versions:** when the candidates run different versions of the translator, the router warns and uses the chosen machine anyway.

**The answer says what happened:**

```json
"machine": "mochi",
"router": {"chose": "mochi", "why": "starts now (12 of 40 CPUs free), estimate 18 min",
           "candidates": [{"machine": "mochi", "start_min": 0, "run_min": 18, "score": 18},
                          {"machine": "carbonio", "start_min": 45, "run_min": 18, "score": 63}],
           "warnings": ["cfdt versions differ: mochi 2026.10.22.6, carbonio 2026.10.22.5"]}
```

### Machines that are nodes of one SLURM cluster

The DGX Sparks are one cluster of two nodes. Each one is still a machine of its own for the router:

- **Separate nodes, separate machines.** Without a shared home folder, a job must run on the node holding its run folder, so each machine's jobs stay on its own node (`P_SLURM_NODE=local`, docs/EXECUTION.md).
- **Estimates per node.** The router's start estimate asks SLURM about that node (`sbatch --test-only --nodelist=<node>`), and `machines()` shows that node's CPUs.
  - SLURM's estimate counts every running job's whole time limit, those on the other node included: spark1 once said "a start in 545 min" for a job that started at once. So when all of the node's CPUs are idle (`sinfo -n <node> -o %C`) and no pending job asks for the node, the router says "starts now" without asking `sbatch --test-only`.
- **Queue per node.** `queue` counts the machine's own node only: running jobs on it, and pending jobs whose `--nodelist` names it. `cluster_queue` counts the whole cluster, and `pending_for_node` the pending jobs for the node. It needs llmt 2026.11.06.1 on the machine for pending jobs (`squeue %n`). With an older llmt, only running jobs are counted per node.
- **The result.** The router picks the spark whose own node frees up first.
- **With shared homes (`any`)** both machines report the cluster's estimate, and SLURM chooses the node: in effect, one machine.

### Runs that wait for other runs, and runs that keep their node (2026.11.18.1)

- **`run.after`.** A study that waits for other runs (docs/EXECUTION.md, `run.after`) goes to the machine that holds them. The router finds that machine from its request map, else by asking each machine's output folder.
  - A `run.host` that names another machine is refused, and so are refs to runs on two machines. Both are refused before anything is placed or copied.
  - The study is passed on with its `run.after`: the machine's llmt-runtime does the waiting, and the answer shows `state: "waiting"`.
  - `<study>` refs within the same request don't constrain placement.
- **`together` runs as one block.** A multi-job run whose next job is already submitted and waiting for the running one keeps its node for all of its jobs (`job.json` `next_job`; cfdt's `solve.transient.chain: "together"`). SLURM's `sbatch --test-only` doesn't plan jobs waiting on a dependency, so it frees those CPUs when the current job ends.
  - `llmt status` reports such a running job's `chain` (`segment`, `max_jobs`, `job_min`, `time_left_min`, `end_min`), and the router keeps the node's `chains`.
  - When another study would start at about that job's end, the router moves its start to the block's end: the time left, plus each remaining job's limit, an upper bound. The candidate's `why` says so.
- **Start times with their offset.** Placement candidates carry `expected_start` and `machines()` carries `typical_job_expected_start`, as this computer's local time with its UTC offset. The servers' clocks run on UTC.
- **`cancel(at="job_end")`.** The router offers `at` on every translator's `cancel`. When the translator's own `cancel` lacks it, `at: "job_end"` runs llmt-runtime's `stop-after-job` on the request's machine, and `at: "now"` is the usual cancel.

## Following up: where each request lives

The results stay as the translator wrote them; their paths are real paths on their machine. The router keeps a map from each request folder to its machine. It learns it from `request` / `bench` fields in answers and from `jobs()` lists, and keeps it across restarts in `router_paths.json`, next to `setup.json`. Calls are then routed by their argument:

| Calls | Routed by | Effect |
|---|---|---|
| `study_status(request)`, `bench_status(bench)`, `cancel(request)`, `get_file(path)`, `get_image(path)`, `render_images(study_folder)` | the path | sent to the machine whose request folder contains it (longest prefix). `machine:/path` also works, to choose explicitly. A path the router doesn't know is tried on each machine until one has it. |
| `jobs(...)`, `list_geometry()` | all machines | asked in parallel and combined: `{"machines": {name: result}}` |
| `add_geometry(...)` | all machines | sent to every machine, so a registered geometry works wherever a run goes |
| `study_format`, `goal_format`, `get_case` / `get_inputs` / `get_model` and other calls | the preferred machine | the first one that answers |
| `machines()` | the router itself | each machine: reachable, mode (release or checkout), `llmt_release` (the llm-translators bundle installed there, named like `2026.10.26.2`; not the translator's version), `version` (the translator's version installed), `serving` (what the server process the router uses runs; `server_stale` and a `note` when it differs), backends, executor, CPUs and their use, the translator's queued and running jobs, load, expected start of a typical job, speed, preferred or excluded |

Answers from a single machine get `"machine": "<name>"` at the top.

## Looking at hand-run work: inspect

Checked against spark2 with router 2026.11.16.1: `jobs`, `roots` and `grep` over `log.ncc.*` work. Hand-run work (a port's `sbatch` scripts, a mesh ladder, a sampler) is outside the translators: its jobs are not in `jobs()`, and its logs can't be reached with `get_file`. Every router instance therefore offers its own read-only tool, whatever the translator: `inspect(machine, action, ...)` (`llmt_inspect.py`, written in the cfdt chat, router 2026.11.16.1). Any machine of the router may be inspected, excluded ones too, since inspect places nothing.

| action | does |
|---|---|
| `jobs` | all of the user's SLURM jobs on the machine: `squeue` now, and `sacct` for those that ended in the last `days` (2, at most 14). Job, name, state, elapsed, limit, nodes, CPUs, where or why, submitted, start, work folder, exit code. Where SLURM accounting is off (the sparks: `sacct_error` says so), finished jobs leave no record, so `recent_logs` lists the newest `slurm-*.out`, `log.*` and `*.log` files under the allowed folders instead (router 2026.11.16.2): path, size, mtime, the job id from a `slurm-<id>.out` or `log.<name>.<id>` name (`sbatch -o log.mesh.%j`; 2026.11.16.3) and whether that job is still queued, and the last 5 lines. At most 30, from the last `days`, up to 4 levels deep, skipping `processor*`, `postProcessing` and `constant`. `logs: true` adds them where accounting works too |
| `roots` | the allowed folders, as the configured patterns expand on that machine |
| `list` | a folder: name, type, size, mtime; `depth` 1-3; at most 1000 entries |
| `read` | `lines` lines (200, at most 2000) of a text file, from line `start` |
| `tail` | the last `lines` lines (50, at most 2000) |
| `grep` | a Python regular expression in a file, or in a folder's files matching `files` (a glob, e.g. `log.*`): file, line, text. At most 300 matches, 200 files, 50 MB scanned |

**The allowed folders** come from `setup.json`, never from the call:

```json
"router": {
  "inspect": {"roots": ["~/s50_*", "~/ncc_ladder", "~/v2506_ring_sample"]},
  "hosts": [{"name": "spark2", "...": "...", "inspect_roots": ["~/s50_*", "~/ncc_ladder"]}]
}
```

- A machine's own `inspect_roots` replaces `router.inspect.roots`.
- Without either, the defaults are `~/s50_*`, `~/ncc_ladder` and `~/v2506_ring_sample`.
- An empty list allows nothing.
- `~` and globs expand on the machine.
- **The sparks don't share home folders.** spark1 and spark2 are one SLURM cluster but each has its own home, so `~/s50_*` expands differently on each. A hand-run case lives only on the spark where it was unpacked, and its logs only there. Inspect that spark: `roots` shows which cases each one has. Its SLURM jobs show on both, since the queue is shared, but the files don't.

**Why it cannot change anything:**

- **Fixed code.** What runs on the machine is the constant script in `llmt_inspect.py` (`python3 -c '<script>' '<json>'`). The call's values reach it only as one quoted JSON argument, never as shell text. Tests put `$(...)`, backticks and quotes in the path, pattern and file glob, and nothing runs. `jobs` runs `squeue`/`sacct` with fixed arguments, and a user name must match `[A-Za-z0-9_.-]{1,64}`.
- **Inside the folders only.** Every path is resolved on the machine (`realpath`: symlinks and `..`) and must lie inside a resolved allowed folder. So must every file `grep` opens in a folder: a symlinked file pointing outside is skipped (router 2026.11.16.1). Tests cover `..`, a symlinked folder and file pointing outside, and `/etc/passwd`.
- **Plain text only.** Only regular files are opened, for reading: a FIFO is refused, so a read never blocks. Binary files are refused (a NUL in the first 8 KB). Names that look like secrets (`.ssh`, `.gnupg`, `.netrc`, `.aws`, `id_*`, `*.pem`, `*.key`, `*.p12`) are refused and left out of listings.
- **Capped.** At most 200,000 characters of answer, 20 s of work on the machine and 30 s for the ssh call, well within the router's 50 s. Numbers that are not whole numbers are refused before anything is sent.

The machine needs Python 3.6 or newer (`python3`).

## Starting from a run of another machine (init_from)

A cfdt study (2026.11.13.6) can start from a finished run: `solve.init_from` or `solve.transient.init_from` is `'<request>/<study>'` or an absolute study path. Only the first study of a chain points at an earlier request; the rest name studies of the same request (`'<study>'`). When that request is on machine N (the request map), the study may still run on another machine M.

- **Checked first.** Before placing, the router runs `cfdt init-check <ref>` on N.
  - If the run is not usable (not finished, no reconstructed time…), the call is refused with N's problems. Nothing is submitted or copied.
  - The check gives the size to copy (`init_bytes`, a few GB for 8 M cells).
- **Placement.** The ref does not pin the call to N: N needs no copy, and every other machine's score adds the copy time (`init_bytes` at 50 MB/s, `COPY_MB_S`). Validation on the other machines drops their "no case … on this machine" for that study.
- **The copy,** in the background, to the chosen machine M only:
  1. On N: `cfdt pack <request folder> --studies <study> --init --out <CFDT_OUT>/.llmt-transfer/<request>.<tag>.tgz`.
  2. N → this computer → M over the two ssh connections, appended to `<archive>.part` on M from the size already there (resumable), then checked with sha256 against N's archive.
  3. On M: `cfdt import <archive> --name <request>@<N>`.
  4. The temporary archives are deleted on both machines. After a failure they stay, so the next attempt resumes.

  While it runs, the answer is `status: "uploading"` with `uploading.init_from` (stage, MB, `sent_pct`, `eta_s`), and nothing is submitted. Repeat the call: it stays on M while the copy runs, and uses the copy once it's done.
- **Reused.** A copy is not made again when `<CFDT_OUT>/<request>@<N>[_importK]/<study>` exists on M with `request.json` `imported.packed` after the source last changed.
  - "Last changed" is init-check's `fields_mtime` (cfdt 2026.11.13.8: the latest time directory), else the result file's time. A reprocess rewrites the result but not the fields, so it causes no new copy.
  - A copy the router made itself is reused only while the source is unchanged (router 2026.11.13.2).
- **The call** goes on with the study's init_from replaced by the import's ref (`<request>@<N>/<study>`). The answer adds `staged.init_from: {<study>: {from: "<N>:<ref>", to: "<new ref>", bytes}}`.
- **validate_studies** checks with `cfdt init-check` on N, and copies nothing. M's "no case … on this machine" goes when N's check is ok; N's problems are added when it isn't.

## Files on this computer

A study often names a file that exists only where the Claude app runs: a STEP file for cfdt's `geometry.step` or feat's part, p3dt's objects, a b3dt step input. The router copies such files to the machine that gets the call, and passes the call on with their path there.

- **Which strings.** Any string in the arguments, at any depth (inside `studies`, lists, nested dicts), that names a regular file under the home folder of the router's computer (`~/...` works too). That includes top-level arguments such as cfdt's `add_geometry(stl=..., groups=..., goal=...)`; `add_geometry` goes to every machine, and each gets its own copy. The top-level `request`, `bench`, `study_folder` and `path` arguments name results on a machine and are left alone, as are `get_file` and `get_image`.
- **Already there.** If the same path exists on the machine with the same size (a shared folder), nothing is copied and the path is passed unchanged.
- **Where.** `$<KEY>_OUT/uploads/<sha256[:12]>/<name>` on the machine, with `<KEY>_OUT` from the machine's settings files (docs/SHARED.md).
  - Most machines don't set `<KEY>_OUT`. Then the translator's own default output folder is used, as `translators.json` describes it (`~/cfdt_runs/uploads/...`).
  - Only a translator with no default listed there uses `~/.llmt/uploads/...`. The name keeps letters, digits and `._+-`; other characters become `_`.
- **Not twice.** A file whose hash is already there is not sent again. The same content under another name is hard-linked (or copied) there.
- **How.** Over the ssh connection the router already shares with the machine, so neither rsync nor scp is needed at either end.
  - The file is appended to `.<name>.part`, so an interrupted copy resumes where it stopped.
  - The copy is checked with sha256 on the machine and only then renamed.
- **The answer** lists what was copied: `"staged": {"<local path>": "<path on the machine>"}`. A path from `staged` given back in a later call still counts as the local file, so that call can go to another machine.
- **Placement.**
  - Up to 25 MB of files in total: they are copied to every candidate, so validation and estimates work there as usual.
  - Larger files are copied to the chosen machine only, and placement uses the queues without validating on each machine.
- **Time.** A copy that can't finish within the call's time budget goes on in the background. The answer is `"status": "uploading"`, with each file's progress and `eta_s`, and says to repeat the same call. Nothing ran yet. The repeated call stays on the machine receiving the files.
- **Limit.** `<KEY>_MAX_UPLOAD_MB` (default 500 MB per file), from the router's environment or the settings of the llm-translators install it runs from (`config/env` on the Mac). A larger file is refused, with what to do: copy it yourself and use `run.host`, or raise the limit.

## Contract addition for the translators: estimates

`validate_studies` (and `validate_goals`) returns, per valid study:

```json
"estimate": {"minutes": 18.0, "procs": 9, "mem_mb": 6000}
```

- **`minutes`:** the expected run time with `procs` ranks or threads; an upper bound is fine.
- **`procs`:** the ranks or threads the study can use (after the size cap, docs/EXECUTION.md "Sizing jobs").
- **`mem_mb`:** the memory it will need.

The translators already compute these before they submit to SLURM. This exposes them before running. Without them the router still works, and places by start time only.

## Commands

```
llmt router setup              add, check or remove machines; choose which translators go through the router
llmt router status [KEY...]    the machines as the router sees them (as machines() does)
llmt mcp setup                 per translator: ... or [a]uto, which means the router
```

An entry through the router:

```json
"cfdt": {"command": "/Users/feranick/Software/llm-translators/bin/router", "args": ["cfdt"]}
```

`llmt mcp test cfdt` tests it like any entry: the handshake goes through the router to the first machine.

## Servers that run an old release

**What happened** (incident 2026-10-25, cfdt on spark2). `machines()` listed cfdt 2026.10.25.6, the installed version. The server process the router used, though, ran 2026.10.22.10.

- **How it got there.** It had started under a release built before the cfdt pin was moved.
- **Why it stayed.** It was older than the stale-server rule (docs/SHARED.md), so it never exited after the deploy.
- **The damage.** Every job it submitted ran its old code too, since a job runs the Python of the server that submitted it.

**What the router does now:**

- **Learns what each server runs.** Each server process reports the version it runs as `"translator"` in its answers (`jobs()`, `study_format`'s status). The router records it for each connection. It asks when it opens a connection, and reads it from every answer afterwards.
- **Shows it.** `machines()` shows it as `serving`, next to `version` (installed, from `llmt status`) and `release`. When they differ: `server_stale: true`, with a note.
- **Replaces such a server.** A server whose version differs from the installed one is retired:
  - closed at once if idle, otherwise as soon as its calls finish;
  - the next call starts a server on the installed release;
  - the router's log says so.

  The installed version must come from a status asked *after* that server started; otherwise the router asks again first, so it never closes a server that is newer than its information.
- **Translators that exit by themselves** (`STALE_SERVER`) keep working as before. This is the backstop for servers that don't.

On a machine, `llmt servers` lists the running servers and their releases, and `llmt servers --stop` ends those not on `current` (docs/SHARED.md).

## Time limits

The Claude app gives up on a tool call after about a minute, and then closes the server, which every chat using it notices. The router therefore answers every call within 50 seconds.

- **Placement takes at most half of that.** Machines whose status hasn't arrived within 12 seconds are left out, and the answer's warnings say so. A translator's status can take seconds: omct loads the Modelica library, cfdt sources OpenFOAM.
- **The tool's own wait is shortened to fit.** Calls with a `wait_s` argument (`run_studies`, `study_status`, `run_benchmarks`, `bench_status`) get what's left of the 50 seconds. Studies still running come back as pending, as usual.
- **Status is fetched in the background.** It's fetched when the router starts, and again in the background once it's older than a minute; meanwhile the older answer is used. After the router sends a machine work, that machine's queue estimate is asked again at the next placement.
- **Connections are opened ahead.** The router connects to every machine when it starts, so a first call doesn't pay for ssh and the server start.
- **A machine that doesn't answer in time** gets an error saying the call may still be working there, and to look with `jobs()` or `study_status` before running it again. The router itself keeps running.
- **Requests named by the router.** When a tool that starts work declares `request_name` (cfdt's `run_studies`, 2026.11.06.1), the router names each call `run_<YYYYmmdd_HHMMSS>_<6 hex>`, unless the call names it. It records `<the machine's <KEY>_OUT>/<name>` as that machine's request before calling.
  - If the machine doesn't answer in time, the answer is not an error. It is `status: "submitted_unconfirmed"`, with `request`, `machine` and `next`: "study_status(request) in a minute; do not submit again".
  - Repeating the call with the same `request_name` is safe: the translator returns that request's status and doesn't submit twice.
  - The output folder comes from the machine's settings (`<KEY>_OUT`), else the translator's default (`~/cfdt_runs`).
- **Status calls on a busy machine.** When the router's connection to a machine already has a call in flight, `study_status` and `bench_status` wait at most 20 s there, so they end well within the minute.

## Limits, and what comes later

- **Each call goes to one machine.** Splitting a large request of independent studies across machines would be a later step.
- **Speed factors are set by hand.** Learning them from the `execution` records is a later step.
- **The router reads the queue, it doesn't reserve.** Two calls placed at the same moment can both pick the machine that looked free; SLURM queues the second.
- **Notifications from the machines' servers** (progress, logs) are not passed on. No translator sends them today.
