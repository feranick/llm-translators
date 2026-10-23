# The router: one translator, several machines

Status: design and implementation, llmt 2026.10.22.3; time limits 2026.10.23.1.

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
- **Tools and instructions** come from the first machine that answers, and the router adds `machines()`. The instructions get one more paragraph about the machines.

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

1. **A manual choice wins.** A study may say `"run": {"host": "mochi"}`, or a list in order of preference. The router removes `host` before passing the study on. All studies of one call go to one machine; if their choices conflict, the call is refused and the router says why.
2. **Data stays where it is.** A study that mentions an earlier request folder, such as a geometry `{"ref": "<request>/<study>"}` or a chained path, goes to the machine holding that folder. The router knows where each request folder lives (below).
3. **Validation on every candidate machine,** in parallel. `validate_studies` / `validate_goals` is static and fast. A machine whose version rejects the request (a newer feature, say) is dropped, if another one accepts it. The validation also returns each study's **estimate**, where the translator provides one: `{"minutes", "procs", "mem_mb"}` (see "Contract", below).
4. **A score for each candidate,** in minutes; the lowest wins:

   ```
   score = expected start + estimated minutes × speed(host) − 0.5 × (preference bonus)
   ```

   - **Expected start, with SLURM:** SLURM's own answer for a job of that size: `sbatch --test-only -n <procs> -t <minutes> --mem <mem>`. Threaded translators ask for `-c <procs>` instead. This counts everything ahead in the queue, other users' jobs included, and backfill.
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
- **The result.** The router picks the spark whose own node frees up first.
- **With shared homes (`any`)** both machines report the cluster's estimate, and SLURM chooses the node: in effect, one machine.

## Following up: where each request lives

The results stay as the translator wrote them; their paths are real paths on their machine. The router keeps a map from each request folder to its machine. It learns it from `request` / `bench` fields in answers and from `jobs()` lists, and keeps it across restarts in `router_paths.json`, next to `setup.json`. Calls are then routed by their argument:

| Calls | Routed by | Effect |
|---|---|---|
| `study_status(request)`, `bench_status(bench)`, `cancel(request)`, `get_file(path)`, `get_image(path)`, `render_images(study_folder)` | the path | sent to the machine whose request folder contains it (longest prefix). `machine:/path` also works, to choose explicitly. A path the router doesn't know is tried on each machine until one has it. |
| `jobs(...)`, `list_geometry()` | all machines | asked in parallel and combined: `{"machines": {name: result}}` |
| `add_geometry(...)` | all machines | sent to every machine, so a registered geometry works wherever a run goes |
| `study_format`, `goal_format`, `get_case` / `get_inputs` / `get_model` and other calls | the preferred machine | the first one that answers |
| `machines()` | the router itself | each machine: reachable, mode (release or checkout), version, backends, executor, CPUs and their use, the translator's queued and running jobs, load, expected start of a typical job, speed, preferred or excluded |

Answers from a single machine get `"machine": "<name>"` at the top.

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

## Time limits

The Claude app gives up on a tool call after about a minute, and then closes the server, which every chat using it notices. The router therefore answers every call within 50 seconds.

- **Placement takes at most half of that.** Machines whose status hasn't arrived within 12 seconds are left out, and the answer's warnings say so. A translator's status can take seconds: omct loads the Modelica library, cfdt sources OpenFOAM.
- **The tool's own wait is shortened to fit.** Calls with a `wait_s` argument (`run_studies`, `study_status`, `run_benchmarks`, `bench_status`) get what's left of the 50 seconds. Studies still running come back as pending, as usual.
- **Status is fetched in the background.** It's fetched when the router starts, and again in the background once it's older than a minute; meanwhile the older answer is used. After the router sends a machine work, that machine's queue estimate is asked again at the next placement.
- **Connections are opened ahead.** The router connects to every machine when it starts, so a first call doesn't pay for ssh and the server start.
- **A machine that doesn't answer in time** gets an error saying the call may still be working there, and to look with `jobs()` or `study_status` before running it again. The router itself keeps running.

## Limits, and what comes later

- **Each call goes to one machine.** Splitting a large request of independent studies across machines would be a later step.
- **Speed factors are set by hand.** Learning them from the `execution` records is a later step.
- **The router reads the queue, it doesn't reserve.** Two calls placed at the same moment can both pick the machine that looked free; SLURM queues the second.
- **Notifications from the machines' servers** (progress, logs) are not passed on. No translator sends them today.
