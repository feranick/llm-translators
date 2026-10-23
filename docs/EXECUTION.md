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
| `P_SLURM_MEM` | `auto` \| a size \| `none` (`auto`) | The job's `--mem`. `auto`: from the study (lmpt: 1 GB + 256 MB per rank + 4 kB per atom, at least 2 GB). `none`: SLURM's default. A `--mem…` in `P_SLURM_ARGS` wins. Implemented by lmpt; proposed for all (below, "Memory"). |
| `P_SLURM_TIME` | `timeout` \| `estimate` (`timeout`) | The job's `--time`. `estimate`: max(`P_SLURM_TIME_MIN` (20), ceil(`P_SLURM_TIME_FACTOR` (3) × the study's estimate) + 10) minutes, at most the `timeout` value; the study's own timeout is 10 minutes less. Implemented by lmpt; proposed for all (below, "Time limits and backfill"). |

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
#SBATCH --time=<ceil(P_TIMEOUT/60) + 10>  # minutes; the translator's own timeout fires first (P_SLURM_TIME)
#SBATCH --mem=<P_SLURM_MEM>                # unless none, or P_SLURM_ARGS has --mem
#SBATCH --export=ALL                      # the server's environment: config/env, backend paths
exec <sys.executable> -m cfd_translator.cli run-study <request> <name>
```

- **Job name.** The `<cli>:` prefix is how `llmt jobs` and the `jobs` tool find the translators' jobs among the user's others.
- **Time limit.** It is 10 minutes longer than `P_TIMEOUT`, so the translator stops itself first and writes a clean "timed out" result. SLURM's limit is only the backstop.
- **Memory.** Ask for it. With `SelectTypeParameters=CR_Core_Memory` and no `DefMemPerCPU`/`DefMemPerNode`, SLURM gives a job that does not ask the node's whole memory, so jobs run one at a time whatever the free cores: found on mochi (`DefMemPerNode=UNLIMITED`, every job `mem=60000M`). An estimate from the study (or a fixed `P_SLURM_MEM`) lets jobs share the node; a job that runs out ends `out_of_memory` with what to raise. Setting `DefMemPerCPU` in `slurm.conf` also works, but it changes every user's jobs.
- **Time limits and backfill.** A job that asks for `P_TIMEOUT` + 10 min (hours) cannot be backfilled: SLURM starts a lower-priority job early only if it ends before the next big job's reserved start. `P_SLURM_TIME=estimate` gives short studies short limits so they fill free cores; the price is that a study that outruns its estimate times out (its result says to rerun with `P_SLURM_TIME=timeout`).
- **Environment.** The job runs the same Python as the server, so the same venv. `--export=ALL` carries everything `bin/mcp` loaded.
- **Reproducibility.** `job.sh` is a complete script, so `sbatch job.sh` reruns the study by hand.

`job.json`, written after a successful `sbatch`:

```json
{"executor": "slurm", "job_id": "12345", "submitted": 1760760000.0, "procs": 9, "time_min": 190,
 "time_from": "timeout", "timeout_s": 10800, "mem": "4096M", "command": ["sbatch", "--parsable", "job.sh"]}
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

## Estimates in validation (for the router)

`validate_studies` returns, for each valid study, the estimate the translator would use to size its job:

```json
{"ar": {"ok": true, "errors": [], "warnings": [], "estimate": {"minutes": 18.0, "procs": 9, "mem_mb": 6000}}}
```

- **`minutes`:** the expected run time with `procs` ranks or threads; an upper bound is fine.
- **`procs`:** the ranks or threads the study can use, after the size cap (see "Sizing jobs").
- **`mem_mb`:** the memory it will need.

llm-translators' router (docs/ROUTER.md) reads these to place a run on the machine where it should finish first. Without them, it places by start time only. They come from the same functions that size SLURM jobs, so they cost nothing extra. Implemented in omct 2026.10.22.1 (`executor.estimate`: `cpus_for_job`, `mem_for_job`, `estimate_minutes`) and lmpt 2026.10.22.1 (`executor.sizing`: `ranks_for`, `mem_mb_for`, `estimate_minutes`).

## Status for llm-translators

`tool_status()` (`<cli> status`) adds:

```json
"executor": {"kind": "slurm", "sbatch": "/usr/bin/sbatch", "min_minutes": 0, "procs": 9, "time_limit_min": 190,
             "time": "timeout", "mem": "auto"}
```

`llmt status` reads this key to show the executor and to tell that a translator supports this contract. If `P_EXECUTOR` is set but the key is missing, it warns that the installed version ignores the setting.

## Per-study run settings (`run`)

`config/env` holds a machine's defaults, and only a person edits it. A study can override a few run settings for itself, so a model can give a long run more time without anyone editing files:

```json
"run": {"timeout_s": 30000, "mem": "12G", "procs": 10, "queue": true}
```

- **Keys.** `timeout_s` sets `P_TIMEOUT` for this study, and so the job's `--time` (+10 min). `mem` is the job's `--mem`; it replaces any `--mem`/`--mem-per-cpu` in `P_SLURM_ARGS`, and other sbatch arguments are kept. `procs` is the ranks (or threads); the size cap of "Sizing jobs" still applies. `queue` is as before. No free-form sbatch arguments come from a study.
- **Caps.** `P_MAX_TIMEOUT` (s), `P_MAX_MEM` and `P_MAX_PROCS` in `config/env` bound what a study may ask. Validation refuses more, naming the variable. Unset means no cap. `tool_status().executor` reports `caps` and the defaults, so a model can see the limits before it writes a study.
- **Record.** `execution.limits = {timeout_s, mem, from_study: [keys the study set]}` in every result. `job.json` keeps the study's `run`.
- **Status.** Implemented in omct 2026.10.21.7 (threads: `procs` is sweep points at once, `--cpus-per-task`), cfdt 2026.10.20.1 and lmpt 2026.10.20.1 (top-level `procs` / `queue` for lmpt's `solve.procs`; ranks capped at atoms / `LMPT_ATOMS_PER_RANK`, 2000). Same keys and variables for feat (feat: `procs` is threads, `--cpus-per-task`).

## Sizing jobs and timing (found with cfdt on mochi, 2026-10-19)

Worth carrying over to lmpt and feat:

- **Ask only for the ranks a study can use.** A job used to ask for `P_PROCS` (9) ranks even when the study could use 1. cfdt caps ranks at cells / `CFDT_CELLS_PER_RANK` (5000), but only after meshing, inside the job, so six small benchmarks held 18 CPUs each for 1-3 ranks and ran two at a time. Now `submit` estimates an upper bound before submitting (cfdt `executor.cell_estimate` / `procs_for_job`) and asks for min(procs, ceil(estimate / per_rank)). The estimate must be an upper bound (too few ranks makes a run slower; too many only wastes cores) and cheap (cfdt builds a b3dt goal for its volume, about 1 s). If it can't be told, use procs as before.
  - lmpt: atoms are known before submitting (lattice × size, or the data file), so ranks = min(procs, atoms / atoms_per_rank). LAMMPS usually scales down to a few thousand atoms per rank (a rule of thumb: measure it).
  - feat: threads, not ranks. Measured on mochi (fea-translator `tools/measure_threads.py`, 2026-10-20): a 175k-node box, direct solver (SPOOLES) 409 / 230 / 167 / 125 s at 1 / 6 / 10 / 19 threads with 6.6 / 12.2 / 12.5 / 14 GB; the iterative solver 167 s at 1 and at 10 threads (it is single-threaded) with 0.64 GB; a 45k-node box 30 / 21 / 18 s at 1 / 6 / 10. So feat estimates nodes before submitting (box parts exactly, goals and STEP files from their volume), uses the iterative solver from 50k nodes and asks for 1 CPU then, and caps direct solves at a thread per 10k nodes (`FEAT_PROCS` default 1). Two 10-thread direct jobs side by side: one ran in 185 s, the other's threaded solve came out wrong (equilibrium error 6 %) and feat reran it single-threaded; CalculiX 2.21's threaded code went wrong four times under load in all, so feat keeps threads optional and checks every result.
- **Memory caps how many jobs run together.** With `P_SLURM_ARGS="--mem=16G"`, only 3-4 jobs fit in mochi's 61.5 GB, whatever their size (two cfdt jobs waited with "Resources" while 12 CPUs were free). Reserve what the study needs (cfdt suggests 8G; a 900k-cell OpenFOAM case uses a few GB), or size `--mem` from the same estimate.
- **The machine is memory-bandwidth bound.** cfdt's 900k-cell pump, 200 iterations, idle mochi: 283 s on 6 ranks, 210 s on 10, 178 s on 19 (19 ranks only 1.18× faster than 10). Two 10-rank jobs at once took 316 s each: a pair in 316 s against 356 s one after the other on 19 (11 % more throughput; 24 % in wall time, because serial meshing and post-processing overlap). For sweeps of several studies, run them side by side on about half the cores each; give a single study all the cores. Worth measuring the same way for LAMMPS and CalculiX (one study at 6, 10, 19 ranks, then two at 10 together).
- **MCP benchmark tools must not wait for SLURM.** cfdt's `run_benchmarks` waited for every job and the call timed out (the jobs still ran). Now it submits and returns within 30 s, and `bench_status(bench)` evaluates what has finished. The same applies to any tool that submits jobs. feat does the same since 2026.10.20.1 (`run_benchmarks` returns within `wait_s`, `bench_status(bench)`).

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
