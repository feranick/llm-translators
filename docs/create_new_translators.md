# Creating a new translator

To build a new translator for another backend program, start a new chat with this file. The translator lives in its own repository and fits into llm-translators like the existing five:

| Key | Repository | Backend |
|---|---|---|
| `b3dt` | build123d-translator | build123d (OpenCascade) |
| `feat` | fea-translator | Gmsh + CalculiX |
| `cfdt` | cfd-translator | Gmsh + OpenFOAM |
| `lmpt` | lammps-translator | LAMMPS |
| `omct` | openmodelica-translator | OpenModelica + MSL 4.0.0 |

print3d_translator (`p3dt`, OrcaSlicer) is a sixth translator, kept outside llm-translators; it runs only on the Mac.

This guide follows cfd-translator 2026.11.23.1 on llmt-runtime 2026.11.19.2, the most complete of them. It has:

- The shared code from **llmt-runtime** (SLURM executor, worker pools, requests, the MCP server's wrapper), used through its functions, not copied.
- SLURM jobs sized before submission, per-study `run` settings within machine caps, runs that wait for other runs (`run.after`), multi-job runs, and `cancel(at="job_end")`.
- Benchmarks that don't wait for SLURM, and cancellation that reaches the whole solver.
- Pictures returned as images, and the `execution` run record.
- An `estimate` in validation, so llm-translators' router can place each run on the best machine.

**Starting the chat.** Connect `~/Software/llm-translators` and `~/Software/cfd-translator` as the working example. Also connect `~/Software/lammps-translator` if the backend needs an independent reference implementation; lammps' `refcalc.py` is the cleanest one. Then write:

> Read `~/Software/llm-translators/docs/create_new_translators.md`, and use cfd-translator as the example to follow. I want a new translator for **<backend>** (<what it computes; the machines it runs on; typical requests>). Start with the research step and the scope, and write them into `planning.md`; then build it in `~/Software/<name>-translator`.

Part 1 describes the translator (its own repository). Part 2 adds it to the umbrella. The checklist at the end covers both.

---

## Part 1: the translator

### The idea

An LLM is good at turning a request into a structured description, and bad at the bookkeeping a solver needs. A translator splits the work:

1. **The LLM writes a study.** This is JSON, with a short, documented format: the physical problem in the user's terms (geometry or structure, materials, conditions, what to compute, expected values).
2. **The translator computes everything else, deterministically.** That means meshes and numbering, which faces or atoms get which condition, every coefficient and unit conversion, and the backend's input files. Nothing is guessed, and nothing relies on the backend's own numbering. cfdt, for example, writes OpenFOAM's polyMesh itself instead of calling `gmshToFoam` or `blockMesh`.
3. **A checker confirms each run.** It uses invariants on every run (balances, conservation, symmetry), an independent reference implementation where possible, convergence, and the study's expected values. The result says `reliable: false` with `unreliable_because` when a hard check fails, and the model is told not to report such numbers.
4. **Errors go back as fixes.** Validation runs before anything starts. Every error message says what to change.

### Research first

Before writing code, write this down in `planning.md`. It is the session context (below); the user-facing parts later go into the README.

- **The backend.** Its exact version on each machine that will run it, how to install it (distribution, PPA, Homebrew, PyPI, built from source), how to find it (a `<KEY>_<BACKEND>` setting, then PATH, then common folders) and how to tell its version.
- **Its inputs and outputs.** The file formats, which output holds which number, and how errors and convergence show up in its logs. Compute things the way the backend does: read its files the way it reads them, and use its geometry formulas, sign conventions and units.
- **Scope.** The first version's analyses, what is out of scope, and five or more typical requests a user would make. Write a first idea of the study format, and challenge it before building.
- **Assumptions about the backend.** List each one together with the check or benchmark that would catch it if it were wrong. They become a README section ("Assumptions about <backend>").
- **Checks and benchmarks.**
  - The invariants every run will check.
  - The reference implementation, if any: a small independent solver or calculator, so the translation and the result reading can be tested without the backend.
  - The benchmarks: closed-form or published answers, each with its tolerance and why. Include at least one per analysis kind, and one chain with another translator if they connect.
- **Size and cost.** What makes a study big, such as cells, nodes, atoms or time steps. How to estimate that size before running, as a cheap upper bound. Roughly how time and memory grow with it, and how the backend scales with ranks or threads. Measure the scaling on the server later, but plan for it now; see "Long runs" below.
- **Interconnections.** Which other translators this one exchanges data with, and through which keys:
  - Geometry: build123d-translator goals inline or STEP files, with the shared face selectors `{nx,ny,nz}` (+ `at`), `{pts}`, `{cyl_r, side}`, `{all}` (lists are unions) and the `selection` report.
  - Materials: lmpt's `feat_material`.
  - Loads: cfdt's wall forces and `feat_thermal` for feat.

  Shared conventions: mm, N, MPa, °C; Z up, the base on z = 0 centred in X/Y; dotted result paths. Import other translators optionally: a warning in validation, and the tests skip when they are missing. The servers may run on different machines, so tools pass specs (JSON) to each other, not paths; `get_file` returns files as base64.

### Names

| Thing | Rule | Example |
|---|---|---|
| Repository | `<name>-translator`, under github.com/feranick | `em-translator` |
| Package | `<name>_translator` | `em_translator` |
| Key = command | short, lowercase, letters and digits only (bin/mcp accepts `[a-z0-9_-]`). Existing keys end in `t`. | `emt` |
| MCP server command | `<key>-mcp`. Required, because llm-translators' `bin/mcp KEY` runs `.venv/bin/<KEY>-mcp`. | `emt-mcp` |
| Settings prefix | the key in capitals | `EMT_` |
| HTTP port | the next free one; 8766–8770 are taken | 8771 |
| Output folder | `~/<key>_runs` (`<KEY>_OUT`) | `~/emt_runs` |

### Repository layout

Modelled on cfd-translator:

```
<name>-translator/
  pyproject.toml
  README.md                 the documentation (user-facing)
  planning.md               the session context (below); a new chat starts with "Read planning.md and start."
  examples/studies.json     a few studies covering the scope
  <name>_translator/
    __init__.py             __version__ = "YYYY.MM.DD.x"
    format.py               study_format(), validate(studies), validate_study(study): the study format and static checks
    select.py               selectors -> faces / atoms / regions, and the 'selection' report (if the study selects things)
    mesher.py / build.py    our own geometry or structure, with our own numbering
    deck.py / case.py       writes the backend's inputs
    <backend>.py            find the backend, run it (serial / MPI / threads, in its own process group), version, capabilities
    results.py / post.py    reads the backend's outputs and logs; results and invariants
    average.py              (iterative solvers) per-iteration monitors, window means, a 'settled' verdict
    ref*.py                 the independent reference implementation (solver or calculator)
    render.py               (optional) pictures as PNG, with the state saved to render other views later
    runner.py               run_study(name, study, folder, ...): one study end to end -> result dict
    bench.py                the benchmark cases, with known answers and tolerances
    runtime.py              the one llmt_runtime.Runtime (RT), with this translator's hooks
    executor.py             a thin layer over RT.slurm and RT.jobs: the size estimate, procs and memory per job
    api.py                  the functions below (JSON in, JSON out)
    cli.py                  the command line
    mcp_server.py           the MCP server (llmt_runtime.server.Server)
  requirements-standalone.txt   llmt-runtime from its git tag, for an install outside llm-translators
  tests/
    test_<key>.py           format, validation, translation, checks; backend tests skip when it is missing
    test_<feature>.py       one file per larger feature (cfdt: test_average, test_scalars, test_porous)
    test_slurm.py           with llmt_runtime.testing.fake_slurm (no fake of its own)
```

`pyproject.toml` (keep the dependencies minimal; the backend itself is never a pip dependency unless it ships wheels, as LAMMPS does):

```toml
[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"

[project]
name = "<name>-translator"
version = "YYYY.MM.DD.1"
description = "<studies> -> <backend> inputs and checked results; CLI, API and MCP server"
readme = "README.md"
requires-python = ">=3.10"
authors = [{ name = "Nicola Ferralis", email = "ferralis@generalflash.com" }]
license = { text = "MIT" }
dependencies = ["llmt-runtime>=2026.11.19.2", "numpy>=1.22", "scipy>=1.8"]   # + "matplotlib>=3.5" if it renders

[project.optional-dependencies]
mcp = ["mcp>=1.2"]
test = ["pytest>=7"]
# mesh = ["gmsh>=4.11; sys_platform != 'linux' or platform_machine != 'aarch64'"]   if it meshes with Gmsh

[project.scripts]
<key> = "<name>_translator.cli:main"
<key>-mcp = "<name>_translator.mcp_server:main"

[tool.setuptools]
packages = ["<name>_translator"]
```

**llmt-runtime is a floor only, never an upper bound,** and it is not on PyPI. Inside llm-translators, `llmt install`, `update` and `deploy` install the umbrella's pinned copy first. Outside it, `requirements-standalone.txt` installs it from its git tag:

```
llmt-runtime @ git+https://github.com/feranick/llmt-runtime@2026.11.19.2
```

Keep the floor and that tag at the same version, and raise both whenever the translator starts using something new in the runtime. A runtime fix gets a new version number: never a second commit under the same number. A floor can't tell two commits of one version apart, and the tag names only one of them (llmt-runtime 2026.11.18.2, November 2026).

### The study

The study keys are the translator's own, but some are shared:

| Key | Meaning |
|---|---|
| `expect` | `[{quantity: "<dotted result path>", value, tol}]`, checked after the run |
| `run` | per-study run settings: `{"timeout_s": 30000, "mem": "12G", "procs": 10, "queue": true, "after": "<request>/<study>", "after_mode": "any"}`; `RUN_KEYS` lists all six. Validation refuses values above the machine's caps (`<KEY>_MAX_*`) and names the variable. No free-form sbatch arguments come from a study. See docs/EXECUTION.md, "Per-study run settings" and "One run after another". `run.host` belongs to the router, which takes it out before the call reaches the translator. |
| `images` | (if it renders) `true` for the default views, or a list of views |
| geometry | as above: `{"goal": <b3dt goal>}`, `{"step": path}`, or simple shapes such as boxes, for tests and benchmarks |

### api.py: the contract

The same functions and shapes as cfdt, so the model, the CLI and llm-translators all treat it the same way.

| Function | Returns |
|---|---|
| `study_format()` | The format as JSON. Keep it compact, around 10–12 kB: kinds, parameters, units, conventions, one example. Also include `status: tool_status()`, so the model sees the backend and the run caps before it writes a study. |
| `validate(studies)` | `{name: {ok, errors: [...], warnings: [...], estimate: {minutes, procs, mem_mb}}}`. Each error says the fix. Static only: no meshing, no backend run. It checks `run` against the caps and, for a study whose `run` has `after` or `after_mode`, calls `RT.jobs.after_problems(None, name, study, studies)`. The `estimate` lets the router place the run (docs/ROUTER.md, docs/EXECUTION.md "Estimates in validation"). It is advice: if computing it needs worker processes and they are refused, leave it out with a warning; don't fail validation. |
| `run(studies, solver, wait)` | Creates a request folder `<KEY>_OUT/run_<date>_<time>_<id>/`, with `request.json`, `studies.json`, and per study `<name>/` and `<name>/<name>.json`. Returns after at most `wait` seconds: `{request, translator, done, pending: [...], studies: {name: result}, progress: {name: {state, ...}}, next}`. |
| `start(studies, solver)` | The same in the background: returns the request folder. Before anything is submitted, it calls `RT.jobs.after_problems(folder, name, study, studies)`, which also finds cycles across requests. It creates the local pool before it records the request, or else writes each local study's result as failed with the pool's error: a refused pool must not leave studies pending forever. |
| `status(request, wait)` | The same shape, for an existing request. It works from any server process: read files and the queue, never in-memory handles. SLURM studies still pending are returned after at most about 15 s, because they don't need the server. |
| `bench(names, solver, wait_s)` | Runs or submits the benchmarks and returns within `wait_s`: `{bench, passed, failed, skipped, pending, cases: {name: {checks: [{what or quantity, expected, got, error, tol, passed}], ...}}}`. |
| `bench_status(bench, wait_s)` | Evaluates the benchmarks of an earlier `bench` that have finished since. |
| `jobs(limit, active)`, `cancel(request, names, at)` | As in docs/EXECUTION.md, through `RT.jobs.list_jobs` and `RT.jobs.cancel_request(..., at=at)`. A translator that runs studies in the local pool too passes `all_requests=True`, so `jobs()` also lists requests and studies without a SLURM job. Its own states and row keys go in through the hooks `jobs_states`, `jobs_row` and `jobs_answer`, not a `jobs()` of its own (omct kept one until 2026.11.19). `cancel` also writes a `cancel` flag file, which stops a local run in whatever process runs it. `at` is `"now"` (the default) or `"job_end"`: the running job finishes and no next job follows. The router supplies `at` when the tool lacks it. |
| `read_file(path, max_mb)` | Base64 of a file under `<KEY>_OUT` only. |
| `tool_status()` | Versions and what the machine has: `{translator, <backend key>: ..., procs, executor: {kind, sbatch, min_minutes, procs, time_limit_min, timeout_s, caps, runtime}, <other translator>: version or null}`. `caps` includes `mem`, `mem_mb` and `mem_from`: the router leaves out machines below a study's `run.mem`. llm-translators reads it, as described below. |
| `inputs(study, name)` | The backend's inputs for one study, without running (cfdt `case`, feat `deck`, lmpt `inputs`). |
| `render_images(folder, images)`, `image_path(path)` | (if it renders) new views of a finished study, and a checked path for `get_image`. |

**A study's result** (`<name>.json`, written atomically: a temporary file, then rename) has these keys:

| Key | Content |
|---|---|
| `ok` | false with `why` and `errors` when it could not run |
| `reliable` | true or false |
| `unreliable_because` | list of reasons, when not reliable |
| `warnings` | list |
| `checks` | the invariants and the reference comparison |
| `expect` | each expected value: `{quantity, value, got, tol, pass}` |
| the results | under documented, stable keys |
| `selection` | what each selector took (faces, atoms, regions), when the study selects things |
| `images` | (if it renders) `images.<name>.path`, `what`, `min`, `max` |
| `files` | paths of what was written |
| `seconds` | timing per phase |
| `execution` | the run record: `executor`, `slurm_job`, `procs_requested`, `procs_used`, `mpi`, and `limits: {timeout_s, mem, from_study}` (docs/EXECUTION.md) |

**Interface keys.** Keys other translators read are listed in the README under "Interface (read by other tools; kept stable)".

**States.** A study's state comes from `RT.jobs.job_states`: `queued`, `waiting`, `running`, then a final one. `waiting` is a `run.after` study, with `reason` and `expected_start`.
- If the translator keeps its own state per study, show `waiting` like `queued`, and pass through any other non-final state a newer runtime adds.
- Never write such a state as a failed result. omct did, in November 2026: a waiting study became "failed: the job left the queue without a result".
- `jobs(active=True)` counts `waiting` as active.

**Times.** Clock times and expected starts come from `RT.requests.clock`, `iso` and `with_offset`, and carry the machine's UTC offset (`19:38 (UTC)`, `2026-11-18T19:38:17+00:00`): the servers run on UTC, the Mac doesn't. Tests compare with `clock()` of the same time, not with a fixed string.

**The reliability verdict.** Choose it per analysis, and state it in the README. For iterative solvers, residual convergence alone can be too strict or too loose. cfdt adds a 'settled' verdict: monitored quantities over the last part of the run neither drift nor scatter (`average.py`).

### cli.py

Every command prints JSON, and `-` reads the studies from stdin.

```
<key> status                          versions; the backend found or not; executor (JSON: llm-translators parses it)
<key> format                          the study format
<key> validate studies.json           static checks
<key> run studies.json [--solver S]   run, check, wait for all
<key> status REQUEST                  a request's studies (finished and pending)
<key> <inputs> studies.json NAME      the backend's inputs of one study, without running
<key> bench [NAMES...]                the benchmarks
<key> jobs [--limit N] [--active]     recent requests with SLURM jobs, their states; the queue
<key> cancel REQUEST [NAMES...]       stop a request's studies (scancel, or the local cancel flag)
<key> run-study REQUEST NAME          one study of a request: what a SLURM job runs (exit 0 = result written)
```

### mcp_server.py

Copy cfd-translator's file, with `llmt_runtime.server.Server` for the stale-install handling and the tool wrapper (`@guard.tool(ToolError)`). It works with the MCP Python SDK 1.x and 2.x (both `MCPServer`/`FastMCP` and `Image`), and has `--http PORT [--host H]`. Rules:

- **Tool names:**

  | Purpose | Tools |
  |---|---|
  | Format and validation | `study_format`, `validate_studies` |
  | Running | `run_studies`, `study_status` |
  | Benchmarks | `run_benchmarks`, `bench_status` |
  | SLURM | `jobs`, `cancel` |
  | Files | `get_file` |
  | Inputs without running | `get_case`, `get_deck` or `get_inputs` |
  | Pictures (if it renders) | `get_image` (returns an MCP `Image`, so the model can see and show it), `render_images` |

- **Startup.** Answer `initialize` at once. Don't check the backend, load profiles or do other slow work before the handshake; do it at the first tool call or in the background. The Gemini Mac app gives up on a server whose handshake takes a few seconds; the Claude app waits.
- **Waiting.** `run_studies` and `study_status` take `wait_s` capped at 50 s, because MCP calls must return well inside the client's timeout; the runtime's tool wrapper gives each call 48 s. Long studies come back `pending`, with `progress` (state, stage, iteration, `eta_min_max`, job id).
- **No tool waits for SLURM.** `run_benchmarks` submits and returns within about 30 s, and `bench_status` collects. A tool that waited for jobs timed out in cfdt, although the jobs still ran.
- **The server never prints to stdout,** because stdout is the MCP channel. Logs go to stderr.
- **Instructions:** at most about 30 lines, in this order:
  1. What it does (one paragraph).
  2. Workflow: read `study_format`, `validate_studies` and fix every error, then `run_studies`.
  3. What to check before trusting numbers (`selection`, `warnings`, convergence, `checks`, `reliable`), and how to check mesh or size convergence.
  4. Domain hints, one line each (e.g. cfdt: rotating zones, `energy`).
  5. Polling: "polling costs tokens: when a run needs more than about 5 minutes, tell the user when it should finish and stop".
  6. The SLURM paragraph from docs/EXECUTION.md, plus: "A study's `run`: {timeout_s, mem, procs, queue} overrides the machine's defaults for that study, within the caps in study_format's status." The router appends its own paragraph (machines, `run.host`, files staged from the Mac, `inspect`): don't repeat it.
  7. Pictures, if any.
  8. Units.
- **Docstrings.** Each tool's docstring says what it returns, in one paragraph.

### Settings (environment variables)

These are read from the environment only, never from shell startup files. A translator may also run from a read-only shared release (docs/SHARED.md), so it writes only to `<KEY>_OUT`, to its caches (under the user's home, configurable) and to temporary folders: never inside its package folder or the venv. llm-translators' `bin/mcp` loads them from `config/env`, and only a person edits that file. A study changes its own run within the caps through `run`.

A translator outside llm-translators, like p3dt, gets its settings from its MCP entry. The Claude app's entry has an `env` block; the Gemini Mac app's Local MCP has only a Command field, so there the settings go in it: `/usr/bin/env <KEY>_<BACKEND>=... /full/path/.venv/bin/<key>-mcp`. Prefer defaults that work without any setting.

| Variable | Meaning |
|---|---|
| `<KEY>_<BACKEND>` | Where the backend is: a command, or a script to source, like `CFDT_FOAM`. |
| `<KEY>_OUT` | The output folder (`~/<key>_runs`). |
| `<KEY>_WORKERS` | Studies at once in the local pool (2). |
| `<KEY>_TIMEOUT` | Seconds per study. |
| `<KEY>_PROCS` | MPI ranks or threads per study (1). The study's `run.procs` overrides it. |
| `<KEY>_MPIRUN` | The MPI launcher (`mpirun`). |
| `<KEY>_<UNIT>_PER_RANK` | The size per rank below which more ranks don't help (cfdt: `CFDT_CELLS_PER_RANK` 5000). |
| `<KEY>_SOLVER` | `auto` / `<backend>` / `ref`, when there is a reference solver. |
| `<KEY>_EXECUTOR`, `<KEY>_SLURM_MIN_MINUTES`, `<KEY>_SLURM_ARGS`, `<KEY>_SLURM_MEM`, `<KEY>_SLURM_TIME` | SLURM, as in docs/EXECUTION.md. |
| `<KEY>_MAX_TIMEOUT`, `<KEY>_MAX_MEM`, `<KEY>_MAX_PROCS` | Ceilings for a study's `run` (unset: none). |

### Long runs and SLURM

Implement [EXECUTION.md](EXECUTION.md) from the start with **llmt-runtime**, not a copy of another translator's `executor.py`:
- **Setup:** declare `llmt-runtime>=<current version>` in `pyproject.toml`, and build one `Runtime(prefix=..., cli=..., package=..., version=__version__, kind="mpi" | "threads", hooks=Hooks(...))` in `runtime.py`.
- **Hooks:** give the runtime what is yours: the job's procs and memory from the study's size, the time per study, the advice after an out-of-memory kill, `running_progress` (which `jobs()` and the `waiting` reason use), and `log_args` (a short summary of each tool call's arguments for the server log).
- **What else it provides:**
  - `llmt_runtime.server.Server`: the tool wrapper and stale-install handling for `mcp_server.py`;
  - `llmt_runtime.workers`: the pools and their parent watch;
  - `llmt_runtime.requests`: request folders and names, clock times, and the call's time budget (`within_call(wait)`, `until`): use these rather than a deadline helper of your own;
  - `llmt_runtime.testing.fake_slurm`: the fake SLURM for the tests.
- **Records the runtime writes:** `job.json` holds the job's own time limit (`time_min`), what the runs need (`need_min`), the study's estimate (`estimate_min`) and where the time came from (`time_from`), and the node a job ran on (`ran_on`). Don't write these through `extra`. `Runtime(queue_user_only=True)` makes queue counts the user's own jobs only.
- **Docs:** its README lists the API and hooks, and its `TRANSLATOR_CHATS.md` says what changed for translators, version by version.

Shared behaviour belongs in the runtime. When this translator needs something every translator would need, ask for it there instead of writing it here.

**Submit every job through `RT.jobs.submit`,** a multi-job run's next jobs included. `run.after` depends on it: each submission moves the studies waiting for this run to its newest job.

What the runtime implements, for reference:

- **Executors.** A local pool, or one SLURM job per study:
  - `job.sh` asks for `--ntasks`, or `--cpus-per-task` for threads, plus `--ntasks-per-core=1`, because OpenMPI 5 refuses more ranks than cores.
  - It also writes `job.json` and runs `<key> run-study`.
  - If `sbatch` fails, the result is `could not submit`. There is no silent local fallback.
- **States and records.** The states, `execution` in every result, `jobs`, `cancel`, and `executor` with `caps` in `tool_status()`.
- **Size the job before submitting.** Estimate an upper bound of the study's size cheaply (cfdt builds the b3dt goal for its volume, in about 1 s). Then ask for min(procs, ceil(size / per_rank)) ranks. A job that asks for 9 ranks when the study can use 1 blocks the machine for others.
- **Ask for memory.** Without a `--mem`, SLURM can give each job the whole node's memory, so jobs run one at a time whatever the free cores. Estimate memory from the same size, or use `<KEY>_SLURM_MEM`. Ask for what the study needs, because memory requests also cap how many jobs fit at once.
- **Time.** The job's limit is the timeout + 10 min, so the translator stops itself first and writes a clean "timed out". With `<KEY>_SLURM_TIME=estimate`, short studies get short limits and SLURM can backfill them. So give every study a time estimate before it runs; it also feeds `eta_min_max` and `<KEY>_SLURM_MIN_MINUTES`.
- **Measure scaling on the server** before choosing defaults. Run one study at about 6, 10 and 19 ranks, then two at 10 side by side. mochi turned out to be memory-bandwidth bound: for sweeps, run several studies side by side on about half the cores each, and give a single study all the cores.
- **Cancellation and timeouts reach the whole solver.** Start the backend in its own process group (`start_new_session=True`) and kill the group, so mpirun and every rank stop. A `cancel` flag file in the study folder lets any process stop a local run within a second.
- **Worker pools.** Create every pool, executor and worker process with `workers.make_pool`, `make_executor` or `make_process` (spawn by default, other arguments as before).
  - **A refused pool:** where an MCP client's sandbox refuses them, they raise `WorkersNotPermitted`, whose text says what to do. The Gemini Mac app's customized sandbox did, with `PermissionError` from SemLock. Let it reach the caller as the tool's error. Don't catch it as a generic failure, and don't fall back to running in-process: a run takes far longer than a call.
  - **Pools never outlive the server.** A spawned multiprocessing worker holds both ends of its own task queue, so it never sees its parent go. Left behind, it waits forever with its `resource_tracker`, and keeps its release "in use" for `llmt gc` (feat, carbonio, November 2026).
  - **Stop them before `os._exit`,** which skips `atexit`: use `workers.stop_pool`, `stop_executor`, `stop_process` or `shutdown`.
  - **A backstop for crashes and `kill -9`:** give each pool the `initializer` `llmt_runtime.workers.watch_parent`, which ends the worker when its parent dies. Don't use `PR_SET_PDEATHSIG`: on Linux it fires when the *thread* that created the child exits, not the process.
- **One run after another (`run.after`).** The runtime does the waiting:
  - it submits with `--dependency=afterany`, or with `--hold` for a study of the same request not yet submitted;
  - it moves the wait to each new job of the runs listed;
  - it checks again when the job starts (the gate in `job.sh`);
  - it reports the study as `waiting`.

  A study with `run.after` is always a SLURM job. Where the executor is local, or with `queue: false`, `after_problems` refuses it. The translator only adds the two keys and the `after_problems` calls (above).
- **Multi-job runs.** A run longer than one job submits its next job through `RT.jobs.submit` with `after=[<current job>]`.
  - **An early next job:** if the run submits its next job early (cfdt's `solve.transient.chain: "together"`), `job.json` keeps the current job and puts the next one under `next_job: {job_id, dropped?}`.
  - **Dropping it:** first mark it `dropped`, then call `RT.jobs.notify_waiters`, then cancel it. The order matters, because a cancelled job satisfies `afterany` at once.
  - **Job records:** earlier segments go under `previous_jobs`; jobs the gate sends back to wait go under `waited_jobs`.
- **`cancel(at="job_end")`.** `RT.jobs.cancel_request(..., at="job_end")` writes `stop_after_job` in the study folder and drops a waiting next job. `submit` then refuses to chain a next job from inside the run's own job. A multi-job runner checks `RT.jobs.stop_requested(folder)` before chaining.
- **Job setup lines.** `<KEY>_JOB_SETUP` and the `script_lines` hook may use `set -e`: the gate's line is safe under it.

### Tests

- **Without the backend.** pytest covers the format, validation (including `run` caps), translation (the written inputs), the result parsing on stored logs, the reference implementation and the checks.
- **With the backend.** The backend tests skip when it is not found, with a reason.
- **SLURM.** Use the runtime's fake SLURM, `llmt_runtime.testing.fake_slurm(tmp_path, monkeypatch)`. Its settings are in `fakeslurm.py`; with `FAKE_DEPENDENCIES=1`, jobs really wait for `--dependency` and `--hold`. Cover:
  - submit, then queued/running/done, and collection by a *new* process;
  - each failure state, from `sacct` and from `job.log` without accounting;
  - cancel (also `at="job_end"`), the size estimate, and `run` overrides with their caps;
  - `run.after`:
    - validation accepts a same-request ref, a list and `after_mode: "ok"`, and refuses an unknown ref, a study waiting for itself and a bad mode;
    - with `b` after `a`, `b`'s `job.json` has `run_after` and its sbatch command has `--dependency=afterany:<a's job>`;
    - with `FAKE_DEPENDENCIES=1`, `jobs()` and `study_status` show `b` as `waiting`.
  - A refused pool: patch `llmt_runtime.workers.make_pool` to raise `WorkersNotPermitted(PermissionError(1, "Operation not permitted"))`. The tool's error carries the message, no request is left pending, and the next call is answered.
- **On the servers.** The development sandbox kills background processes after about 3 minutes, so run the long benchmarks on the servers (`<key> bench` on carbonio or mochi, or `run_benchmarks` then `bench_status` through MCP). On each machine, `llmt selftest slurm --like <key>` checks SLURM for the waits, holds, dependency moves, the gate and cancel at job end, with your translator's `SLURM_ARGS`, without the translator.
- **Tests that depend on timing** must pass on a loaded machine, because `llmt deploy --test` runs them on servers at full load. The runtime's self-test failed on mochi, at load 20, with 3 s jobs. Leave margins, and have a failing test print its own step.

### Conventions

- **Versions** are `YYYY.MM.DD.x`, in `__init__.py`, `pyproject.toml`, every module's docstring (`Version: ...`) and the README's version table. Update the README with every change.
- **README sections** (cfdt's order):
  - What it is, and how it relates to the other translators; its scope.
  - Install: the backend, then `llmt install <key>` in llm-translators (not a venv of its own).
  - Use.
  - Running on SLURM: settings, per-study run settings, what a submission writes, states, tools, the run record.
  - A study, with Results.
  - The checker: invariants, the reference, benchmarks with a results table, assumptions about the backend.
  - How it works, step by step, by module.
  - For LLM tools: the MCP server, with the tools and the polling note.
  - Files, Versions.
- **`planning.md` is the session context,** separate from the README. It holds:
  - What this is, and the other translators.
  - Interconnections and conventions.
  - Architecture, and the environment (Mac, sandbox, servers).
  - A dated state log: what was tested where, with numbers, and what is still to do.
  - Open ideas.

  A new chat starts with "Read planning.md and start."
- **Lessons from the first four:**
  1. Never rely on the backend's numbering.
  2. Make checks exact where possible, and loose only with a stated reason.
  3. Every run checks itself.
  4. Validate before running.
  5. Keep `study_format` compact.
  6. Report a long run as pending with an ETA, and tell the user when to come back.
  7. Write result files atomically.
  8. Size jobs from the study.
  9. No tool waits on a queue.
  10. Kill whole process groups.
  11. Shared behaviour goes in llmt-runtime, used through its functions; a translator keeps only what is its own.
  12. A refused resource (a pool, a submission) becomes the call's error, says what to do, and leaves no study pending forever.
  13. Answer the MCP handshake at once.
  14. Never reuse a version number.
- **Authorship and commits.** The author is Nicola Ferralis <ferralis@generalflash.com>. Commits and pushes are done from the Mac; the servers pull through llm-translators.

---

## Part 2: adding it to llm-translators

llm-translators is driven by `translators.json`: `llmt` (setup, install, status, tune, jobs, MCP entries) and `bin/mcp` need no code changes for a new translator.

### 1. Publish the repository

Create `github.com/feranick/<name>-translator`, then push `main`.

### 2. Add the submodule

In `~/Software/llm-translators`:

```bash
git submodule add ../<name>-translator.git <name>-translator     # relative URL, like the others
git config -f .gitmodules submodule.<name>-translator.branch main
```

### 3. Add it to translators.json

Add an entry; cfdt's is the fullest example. The fields, with what `llmt` and the router do with them:

```json
"<key>": {
 "repo": "<name>-translator",              // the submodule folder
 "package": "<name>_translator",           // imported to tell the installed version
 "summary": "<DOMAIN>: <analyses> (<backend>)",            // llmt list, setup
 "cost": "light | moderate | heavy (<time per study>): <where it should run>",
 "suggest": "here | remote",               // setup's default placement on a machine with the Claude app
 "cli": "<key>",                           // '<cli> status' is run and parsed; SLURM job names start with '<cli>:'
 "port": 8770,                             // llmt mcp serve
 "extras": ["mcp"],                        // installed always ("mesh" too if it meshes with Gmsh)
 "test_extra": "test",
 "optional_extras": {},                    // {"extra": "question asked in setup"}
 "uses": {"b3dt": "why"},                  // offered with it in setup (optional companions)
 "status": true,                           // has '<cli> status' printing JSON
 "bench": true,                            // has '<cli> bench'
 "executor": {"prefix": "<KEY>", "kind": "mpi | threads", "min_minutes": 1},   // omit if it never runs long
 "after_since": "YYYY.MM.DD.x",            // the first release that accepts run.after
 "chain": {"path": "solve.transient.chain", "together": "together", "share": "share"},   // only with a chain mode
 "tuning": {"procs": "<KEY>_PROCS", "workers": "<KEY>_WORKERS", "workers_default": 2,
            "set": {}, "why": ""},         // llmt tune: ranks per study from the cores; omit if not parallel
 "backends": [
  {"key": "<status key>", "name": "<Backend>", "doc": "<anchor in BACKENDS.md>",
   "without": "what still works without it"}
 ],
 "env": {"<KEY>_<BACKEND>": "...", "<KEY>_OUT": "...", "<KEY>_WORKERS": "...", "<KEY>_TIMEOUT": "...",
         "<KEY>_SOLVER": "...", "<KEY>_PROCS": "...", "<KEY>_MPIRUN": "...", "<KEY>_<UNIT>_PER_RANK": "...",
         "<KEY>_EXECUTOR": "...", "<KEY>_SLURM_MIN_MINUTES": "...", "<KEY>_SLURM_ARGS": "...",
         "<KEY>_MAX_TIMEOUT": "...", "<KEY>_MAX_MEM": "...", "<KEY>_MAX_PROCS": "..."}
}
```

(JSON has no comments: drop them.)

- **`after_since`.** The router passes `run.after` only to machines whose installed version is at least this, and names the version needed otherwise. Without it, the router refuses `run.after` for this translator. Add it in the same commit as the pin of that release.
- **`chain`.** Some machines' schedulers don't let a run's early-submitted next job go first (`llmt status`: `slurm.chain_together: no`). There, the router sends a study that doesn't choose a chain mode with the `share` value, and says so.

- **`backends[].key` must match a key of the translator's `tool_status()`.** `llmt` reads that value as:
  - **missing:** `null`, `false`, or a dict with `error`;
  - **found:** a dict with `version`, `exe`, `foamRun` or `bashrc`, or a string such as a path. An absolute path that does not exist counts as missing.
- **`extras` and `optional_extras` must exist** in the translator's `pyproject.toml` (`[project.optional-dependencies]`). pip skips an extra a package doesn't define, with only a warning: feat asked for `remnder` from 2026-10-28 to 2026-11-19 and installed without rendering. llm-translators' tests check this for every checked-out translator.
- **`env` lists every setting the translator reads,** each with a one-line description and its default. It becomes the commented template in `config/env`, and setup prompts for the backend paths among them. To list them all, search the code: `grep -rhoE "<KEY>_[A-Z_]+" <name>_translator/*.py | sort -u`.

### 4. Document the backend

- **`BACKENDS.md`:** a row in the table, and a section whose anchor is `backends[].doc`. The section covers how to install it on each platform, how it is found, which setting overrides that, and how to check it. Do not script the installation: `llmt` only reports what is missing.
- **`README.md`:** a row in the translators table at the top (what it does at the pinned commit).

### 5. Check, then commit the pin

```bash
./llmt list                       # the new row
./llmt install <key>              # checks out the submodule, pip install -e, reports backends
./llmt status                     # version, commit 'pinned', backends ok, how it runs (local / SLURM jobs)
./llmt test <key>                 # its pytest
./llmt bench <key>
./llmt tune <key>                 # if it has executor/tuning: SLURM and ranks
./llmt mcp setup                  # local, remote or auto (router) on the client
./llmt mcp test <key>             # handshake as the Claude app does it, tool list; through the router it also
                                  # saves config/router_tools_<key>.json, so the router answers the next startup at once
git add .gitmodules <name>-translator translators.json BACKENDS.md README.md
git commit -m "Add <name>-translator" && git push
```

Then set it up on each machine that runs it: `git pull`, `./llmt install <key>` and `./llmt tune <key>`, or `sudo ./llmt deploy --test` on a shared server. On each client:
- **Claude app:** if the MCP server is new there, run `./llmt mcp setup`.
- **Gemini Mac app:** add it by hand (README, "Connecting an app"), and run `./llmt mcp test <key>` once.

Later versions: `./llmt pin <key> --commit --push` here, then `./llmt update` on each personal machine or `sudo ./llmt deploy --test` on each shared server, and reconnect the apps. When a version adds settings, add them to `env` in `translators.json` in the same commit as the pin.

**When a version needs a newer runtime,** go in this order:
1. Push and tag llmt-runtime.
2. Push and tag the translator, with its raised floor.
3. Run `./llmt pin runtime <key> --commit --push`.

`llmt status` flags an install whose runtime is older than a translator's floor ("installed as …, code …: llmt update --no-pull").

### 6. Connect it to the other translators

- **Shared keys.** If it consumes another translator's output, add a chain benchmark on its side, and list the key under that translator's "Interface" section.
- **`uses`.** If another translator should offer this one, add it to that translator's `uses` in `translators.json`.

---

## Checklist

**In the translator's repository:**

- [ ] `planning.md`: backend versions per machine, how it is found, scope and typical requests, assumptions with what catches each, checks, benchmarks with tolerances, size and cost.
- [ ] Names: repo, package, key, `<key>-mcp`, `<KEY>_` prefix, port.
- [ ] `pyproject.toml` floor `llmt-runtime>=…`, and the same tag in `requirements-standalone.txt`; `runtime.py` with the hooks.
- [ ] `format.py`: compact `study_format` (with `status`); `validate` with fixable errors, including `run` against the caps; `RUN_KEYS` with `after` and `after_mode`; `RT.jobs.after_problems` in `validate` and in `start`.
- [ ] Translation with our own numbering; the backend's inputs written by us.
- [ ] Checks on every run; reference implementation; `reliable` / `unreliable_because`; `expect`; selection report.
- [ ] `bench.py`: one or more benchmarks per analysis kind, plus chains.
- [ ] `api.py` contract, including `bench_status`; `tool_status()` with the backend key and `executor` (+ `caps`); states from `RT.jobs.job_states`, `waiting` included; times from `RT.requests`.
- [ ] `cli.py`, including `status` (JSON), `jobs`, `cancel` and `run-study`.
- [ ] `mcp_server.py`: tool names; `initialize` answered at once; `wait_s` ≤ 50; no tool waits for SLURM; `cancel` with `at`; instructions; nothing on stdout.
- [ ] SLURM per docs/EXECUTION.md:
  - [ ] the states and the `execution` record with `limits`;
  - [ ] `jobs` and `cancel` (with the flag file);
  - [ ] sizing from the study (ranks, memory, time) and per-study `run` with caps;
  - [ ] process-group kills;
  - [ ] every job through `RT.jobs.submit`; multi-job runs with `next_job`, `dropped` and `stop_requested`;
  - [ ] every pool through `workers.make_pool`, `make_executor` or `make_process`, with `WorkersNotPermitted` reaching the caller.
- [ ] Tests: offline, with the backend (skipping without it), and fake SLURM.
- [ ] README sections; versions `YYYY.MM.DD.x` everywhere.
- [ ] Benchmarks pass on the server(s) that will run it; scaling measured there; `llmt selftest slurm --like <key>` passes there.

**In llm-translators:**

- [ ] Submodule added (relative URL, branch main).
- [ ] `translators.json` entry; backend keys match `tool_status()`; every extra defined in its `pyproject.toml`; all settings in `env`; `after_since`, and `chain` if it has a chain mode.
- [ ] `BACKENDS.md` section and table row; `README.md` row.
- [ ] `llmt install / status / test / bench / tune / mcp test` pass.
- [ ] Committed and pushed; `llmt install <key>` and `llmt tune <key>` on the machines that run it (or `deploy` on shared servers); `llmt mcp setup` and `llmt mcp test <key>` on the clients.
