# Running studies through SLURM: the contract for the translators

Status: specification, 2026-10-18 (run record added the same day). llm-translators configures it (`llmt setup`, `llmt tune`, `config/env`) and shows it (`llmt status`, `llmt jobs`); each translator implements it. It applies to cfd-translator (cfdt), lammps-translator (lmpt) and fea-translator (feat). build123d-translator builds in milliseconds and does not need it.

## Why

Today each MCP server process runs studies in its own worker pool (`P_WORKERS`, default 2):

- **No coordination.** Every chat that uses a translator starts its own server over ssh, with its own pool. Two projects, each running studies, put 2 × 2 studies × `P_PROCS` ranks on one machine, and nothing stops them.
- **Runs die with the conversation.** The studies are children of the server. When the chat closes or ssh drops, the server exits and the runs are lost. cfd-translator already says so: "not finished and not running here (the server restarted?)".

The machines (mochi, carbonio) have SLURM on a single node. Submitting each long study as a SLURM job fixes both problems. SLURM queues studies from every chat against the machine's cores, and a job outlives the server that submitted it. The model then submits, reports when to come back, and stops. Later, or from another chat, it reads the queue and the results.

## Settings

`P` is the translator's prefix: `CFDT`, `LMPT`, `FEAT`. They are read from the environment. `bin/mcp` loads them from `config/env`, and `llmt setup` / `llmt tune` write them.

| Variable | Values (default) | Meaning |
|---|---|---|
| `P_EXECUTOR` | `local` \| `slurm` (`local`) | `slurm`: long studies become SLURM jobs. `local`: the worker pool, as today. |
| `P_SLURM_MIN_MINUTES` | number (cfdt 0, lmpt 1, feat 5) | Studies whose own time estimate is below this run locally even with `slurm`, because queueing would cost more than it saves. A study with no estimate goes to SLURM. |
| `P_SLURM_ARGS` | text (empty) | Extra `sbatch` arguments, split like a shell does, e.g. `--mem=64G --nice=100`. |
| `P_PROCS` | integer (1) | MPI ranks per study (cfdt, lmpt) or threads (feat, CalculiX), as today. A study's own `procs` overrides it. |
| `P_TIMEOUT` | seconds | Per study, as today. With SLURM it also sets the job's time limit (below). |
| `P_WORKERS` | integer (2) | Local pool only. With SLURM, the queue decides how many run at once. |

A study can also say `"queue": true` (always SLURM) or `"queue": false` (always local). That overrides `P_SLURM_MIN_MINUTES` but not `P_EXECUTOR=local`.

## One job per study

`run_studies` prepares the request folder as today (`request.json`, `studies.json`, validation). For each study that goes to SLURM, it writes two files into `<request>/<name>/` and submits with `sbatch --parsable [P_SLURM_ARGS] job.sh`.

`job.sh`:

```bash
#!/bin/bash
#SBATCH --job-name=cfdt:<request folder name>:<study name>
#SBATCH --chdir=<request>/<name>
#SBATCH --output=<request>/<name>/job.log
#SBATCH --ntasks=<procs>                  # MPI (cfdt, lmpt); feat: --ntasks=1 --cpus-per-task=<procs>
#SBATCH --time=<ceil(P_TIMEOUT/60) + 10>  # minutes; the translator's own timeout fires first
#SBATCH --export=ALL                      # the server's environment: config/env, backend paths
exec <sys.executable> -m cfd_translator.cli run-study <request> <name>
```

- **Job name.** The `<cli>:` prefix is how `llmt jobs` and the `jobs` tool find the translators' jobs among the user's others.
- **Time limit.** It is 10 minutes longer than `P_TIMEOUT`, so the translator stops itself first and writes a clean "timed out" result. SLURM's limit is only the backstop.
- **Environment.** The job runs the same Python as the server, so the same venv. `--export=ALL` carries everything `bin/mcp` loaded.
- **Reproducibility.** `job.sh` is a complete script, so `sbatch job.sh` reruns the study by hand.

`job.json`, written after a successful `sbatch`:

```json
{"executor": "slurm", "job_id": "12345", "submitted": 1760760000.0, "procs": 9, "time_min": 190,
 "command": ["sbatch", "--parsable", "job.sh"]}
```

If `sbatch` fails (not installed, controller down), the study's result is `{"ok": false, "why": "could not submit: <sbatch's message>"}`. The translator never falls back to local silently.

### The job's command: `<cli> run-study REQUEST NAME`

- **What it does.** It reads `request.json` and the study's `studies[name]`, then runs `run_study` exactly as a pool worker does today: meshing, case, solve, results, pictures.
- **Ranks and threads.** It takes them from `SLURM_NTASKS` (MPI) or `SLURM_CPUS_PER_TASK` (threads; set `OMP_NUM_THREADS` from it), and falls back to `P_PROCS` when run by hand.
- **MPI.** It runs inside the allocation (`mpirun -np $SLURM_NTASKS`, or `P_MPIRUN`). On a single node, OpenMPI's mpirun uses the allocation as it is.
- **Whole cores.** MPI jobs ask for `--ntasks-per-core=1`. OpenMPI 5 places ranks by core and refuses more ranks than physical cores in the job's CPU set ("allocation-overload"). On a node with two hardware threads per core, `--ntasks=N` alone can give fewer than N cores. Found on mochi with cfdt.
- **The result file.** It writes `<name>.json` atomically (a temporary file, then rename), so `study_status` never reads half a file.
- **Exit code.** It exits 0 when it wrote a result, even if the study failed, because the result says why. It exits non-zero only on a crash; then `job.log` has the traceback.

## The run record (in every study result)

Every study result, local or SLURM, carries `execution` with the same keys in every translator. It says where the study ran and on what, so a model or `llmt` can read it without knowing the translator:

```json
"execution": {"executor": "slurm", "slurm_job": "5516", "procs_requested": 9, "procs_used": 4,
              "mpi": {"launch": "mpirun -np 4", "binding": "default", "cores": 9, "hwthreads": 18}}
```

| key | meaning |
|---|---|
| `executor` | `slurm` (a job), `local` (the server's worker pool), or `by hand` (`<cli> run-study` outside SLURM) |
| `slurm_job` | the job id, or null |
| `procs_requested` | ranks (or threads) asked for: `SLURM_NTASKS` / `SLURM_CPUS_PER_TASK`, the study's `procs`, or `P_PROCS` |
| `procs_used` | what actually ran. It can be fewer: cfdt caps ranks at cells / `CFDT_CELLS_PER_RANK`. A difference also goes in `warnings`. |
| `mpi.launch` | the launcher command as run (e.g. `mpirun --map-by hwthread -np 9`); `mpi` is null for a serial run |
| `mpi.binding` | `default` (mpirun's own placement), or what the translator asked for (`core`, `hwthread`, `none`) |
| `mpi.cores`, `mpi.hwthreads` | physical cores and hardware threads in the CPU set the study ran in (a job's allocation, or the machine) |

Results that end without running, such as `could not submit` and the finished states below, carry what is known: `{"executor": "slurm", "slurm_job": ...}`.

Threaded translators (feat) put threads in `procs_requested`/`procs_used` and leave `mpi` null.

## States

`study_status(request)` keeps its shape: `done`, `pending`, `studies`, `progress`, `next`. A pending study's `progress` gains `state`, and in SLURM mode it is computed from the files and the queue, never from in-process handles. This is why it works from any server process, after restarts and from other chats.

| State | How it is known | What `progress` adds |
|---|---|---|
| `done` | `<name>.json` exists | (the result, as today) |
| `queued` | `squeue -h -j ID -o '%T\|%r\|%S'` says PENDING | `reason` (Resources, Priority), `expected_start` when SLURM gives one, `ahead` (jobs before it) |
| `running` | squeue says RUNNING or COMPLETING | the translator's progress as today (iteration, `eta_min_max`), plus `job_elapsed_min` |
| `timed_out` | sacct says TIMEOUT | why, and what to change (`P_TIMEOUT`, the study's iterations) |
| `out_of_memory` | sacct says OUT_OF_MEMORY | "add `--mem=...` to `P_SLURM_ARGS`, or coarsen the mesh" |
| `cancelled` | sacct says CANCELLED | who cancelled, if known |
| `failed` | sacct says FAILED / NODE_FAIL, or the job is gone without a result | the last 30 lines of `job.log` |

- **A finished state is a result.** `timed_out`, `out_of_memory`, `cancelled` and `failed` end the study: it moves from `pending` into `studies` with `{"ok": false, "state": ..., "why": ...}`.
- **Without accounting.** On single-node installs `sacct` often has no accounting storage. If sacct fails or knows nothing, use this rule: a job that is not in `squeue` and left no `<name>.json` is `failed`, with the tail of `job.log` (SLURM writes "DUE TO TIME LIMIT" or "CANCELLED" there, so match those to `timed_out` and `cancelled`).
- **Speed.** Make one `squeue` call per `study_status` for all the request's job ids (`-j id1,id2,...`), not one per study.

`next` in SLURM mode:

> The studies are SLURM jobs on this machine (ids ...). They keep running without this conversation. Tell the user when they should finish (eta) and stop; collect them later with study_status(request) or find them with jobs().

## Two new tools (same names and shapes in every translator)

`jobs(limit: int = 20, active: bool = False)`

- **What it lists.** The newest request folders under `P_OUT` that have any `job.json`, each with its studies' states (as above), job ids and ETAs.
- **The queue as a whole.** `queue: {running, queued, cpus_in_use}` for all of this translator's jobs (job names starting with `<cli>:`), and `machine: {cpus, cpus_allocated}` from `sinfo -h -o '%C'`.
- **`active=True`.** Only requests with queued or running studies.
- **Why.** A new chat, or the other project, finds runs without knowing the request path.

`cancel(request: str, names: list[str] | None = None)`

- **What it does.** `scancel` on the request's jobs (or the named studies), and returns their states.
- **In local mode.** It terminates the study if this server process runs it, and otherwise says it cannot.

## The server's instructions (add to each)

> Long studies run as SLURM jobs on the machine (when P_EXECUTOR=slurm): run_studies returns them pending with job ids. They do not depend on this conversation. Report the expected finish time and stop; do not poll. Later, study_status(request) collects them; jobs() lists recent and active runs (also those started in other conversations); cancel(request) stops them.

## Status for llm-translators

`tool_status()` (`<cli> status`) adds:

```json
"executor": {"kind": "slurm", "sbatch": "/usr/bin/sbatch", "min_minutes": 0, "procs": 9, "time_limit_min": 190}
```

`llmt status` reads this key to show the executor and to tell that a translator supports this contract. If `P_EXECUTOR` is set but the key is missing, it warns that the installed version ignores the setting.

## Local mode, later

Optional, phase 2. Even without SLURM, studies from several server processes on one machine could share one limit. Before starting a study, a worker would take one of `P_WORKERS` lock files under `P_OUT/.slots/` (`fcntl.flock`), and while waiting the study would be `queued` with `reason: "waiting for a free slot"`. The machines with SLURM don't need it.

## Tests

- **Without SLURM.** Put fake `sbatch`, `squeue`, `scancel` and `sacct` scripts on `PATH`. `sbatch` starts `job.sh` in the background, prints an id and records the pid; `squeue` reports RUNNING while the pid lives; `scancel` kills it.
  - Cover: submit, then queued/running/done, then the result is collected by a *new* server process.
  - Cover each failure state from a fake `sacct`, and from `job.log` when `sacct` fails.
- **On mochi.** Run a fast benchmark with `P_EXECUTOR=slurm` (`couette_2d`, `madelung_nacl`, `axial_bar`).
  - Run two benchmarks from two server processes at once, and confirm they queue rather than oversubscribe.
  - Close the MCP connection while a job runs, reconnect, and collect the result.

## Order

1. cfd-translator: the longest runs, and the one that jams the machine today.
2. lammps-translator: MD runs, same pattern (MPI).
3. fea-translator: only for large or `converge` studies (threads, `--cpus-per-task`).
