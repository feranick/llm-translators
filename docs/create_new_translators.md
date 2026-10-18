# Creating a new translator

To build a new translator for another backend program, start a new chat with this file. The translator lives in its own repository and fits into llm-translators like the existing four:

| Key | Repository | Backend |
|---|---|---|
| `b3dt` | build123d-translator | build123d (OpenCascade) |
| `feat` | fea-translator | Gmsh + CalculiX |
| `cfdt` | cfd-translator | Gmsh + OpenFOAM |
| `lmpt` | lammps-translator | LAMMPS |

**Starting the chat.** Connect `~/Software/llm-translators`, plus one existing translator as a working example (lammps-translator is the most complete: SLURM, run record, reference implementation). Then write:

> Read `~/Software/llm-translators/docs/create_new_translators.md`. I want a new translator for **<backend>** (<what it computes; the machines it runs on; typical requests>). Start with the research step and the scope; then build it in `~/Software/<name>-translator`.

Part 1 describes the translator (its own repository). Part 2 adds it to the umbrella. The checklist at the end covers both.

---

## Part 1: the translator

### The idea

An LLM is good at turning a request into a structured description, and bad at the bookkeeping a solver needs. A translator splits the work:

1. **The LLM writes a study.** This is JSON, with a short, documented format: the physical problem in the user's terms (geometry or structure, materials, conditions, what to compute, expected values).
2. **The translator computes everything else, deterministically.** That means meshes and numbering, which faces or atoms get which condition, every coefficient and unit conversion, and the backend's input files. Nothing is guessed, and nothing relies on the backend's own numbering.
3. **A checker confirms each run.** It uses invariants on every run (balances, conservation, symmetry), an independent reference implementation where possible, convergence, and the study's expected values. The result says `reliable: false` with `unreliable_because` when a hard check fails, and the model is told not to report such numbers.
4. **Errors go back as fixes.** Validation runs before anything starts. Every error message says what to change.

### Research first

Before writing code, write down (it goes into the README):

- **The backend.** Its exact version on each machine that will run it, how to install it (distribution, PPA, Homebrew, PyPI, built from source), how to find it (a `<KEY>_<BACKEND>` setting, then PATH, then common folders) and how to tell its version.
- **Its inputs and outputs.** The file formats, which output holds which number, and how errors and convergence show up in its logs. Compute things the way the backend does: read its files the way it reads them, and use its sign conventions and units.
- **Scope.** The first version's analyses, what is out of scope, and five or more typical requests a user would make.
- **Assumptions about the backend.** List each one together with the check or benchmark that would catch it if it were wrong. They become a README section ("Assumptions about <backend>").
- **Benchmarks.** Cases with closed-form or published answers, each with its tolerance and why. Include at least one benchmark per analysis kind, and one chain with another translator if they connect.
- **Interconnections.** Which other translators this one exchanges data with, and through which keys:
  - Geometry: build123d-translator goals or STEP files, with the shared face selectors `{nx,ny,nz}`, `{pts}`, `{cyl_r, side}`, `{all}` and the `selection` report.
  - Materials: lmpt's `feat_material`.
  - Loads: cfdt's wall forces for feat.

  The servers may run on different machines, so tools pass specs (JSON) to each other, not paths. `get_file` returns files as base64.

### Names

| Thing | Rule | Example |
|---|---|---|
| Repository | `<name>-translator`, under github.com/feranick | `em-translator` |
| Package | `<name>_translator` | `em_translator` |
| Key = command | short, lowercase, letters and digits only (bin/mcp accepts `[a-z0-9_-]`). Existing keys end in `t`. | `emt` |
| MCP server command | `<key>-mcp`. Required, because llm-translators' `bin/mcp KEY` runs `.venv/bin/<KEY>-mcp`. | `emt-mcp` |
| Settings prefix | the key in capitals | `EMT_` |
| HTTP port | the next free one; 8766–8769 are taken | 8770 |
| Output folder | `~/<key>_runs` (`<KEY>_OUT`) | `~/emt_runs` |

### Repository layout

```
<name>-translator/
  pyproject.toml
  README.md
  CLAUDE.md                 context for new chats: what this is, conventions, environment, state, open ideas
  examples/studies.json     a few studies covering the scope
  <name>_translator/
    __init__.py             __version__ = "YYYY.MM.DD.x"
    format.py               study_format(), validate(studies), validate_study(study): the study format and static checks
    runner.py               run_study(name, study, folder, ...): one study end to end -> result dict
    <backend>.py            find the backend, run it (serial / MPI / threads), read its version and capabilities
    deck.py / case.py       writes the backend's inputs (from our own numbering)
    results.py              reads the backend's outputs and logs
    ref*.py                 the independent reference (solver or calculator) for the checks
    bench.py                the benchmark cases, with known answers and tolerances
    executor.py             local pool or SLURM jobs (docs/EXECUTION.md); copy lmpt's and adapt it
    api.py                  the functions below (JSON in, JSON out)
    cli.py                  the command line
    mcp_server.py           the MCP server
  tests/
    test_<key>.py           format, validation, translation, checks; backend tests skip when it is missing
    fakeslurm.py            fake sbatch/squeue/scancel/sacct for the SLURM tests
    test_slurm.py
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
dependencies = ["numpy>=1.24", "scipy>=1.10"]

[project.optional-dependencies]
mcp = ["mcp>=1.2"]
test = ["pytest>=7"]
# mesh = ["gmsh>=4.11; sys_platform != 'linux' or platform_machine != 'aarch64'"]   if it meshes

[project.scripts]
<key> = "<name>_translator.cli:main"
<key>-mcp = "<name>_translator.mcp_server:main"

[tool.setuptools]
packages = ["<name>_translator"]
```

### api.py: the contract

The same functions and shapes as the other translators, so the model, the CLI and llm-translators all treat it the same way.

| Function | Returns |
|---|---|
| `study_format()` | The format as JSON. Keep it compact, around 10–12 kB: kinds, parameters, units, conventions, one example. Also include `status: tool_status()`, so the model sees what the machine has. |
| `validate(studies)` | `{name: {ok, errors: [...], warnings: [...]}}`. Each error says the fix. Static only: never runs the backend. |
| `run(studies, wait)` | Creates a request folder `<KEY>_OUT/run_<date>_<time>_<id>/`, with `request.json`, `studies.json`, and per study `<name>/` and `<name>/<name>.json`. Returns after at most `wait` seconds: `{request, translator, done, pending: [...], studies: {name: result}, progress: {name: {...}}, next}`. |
| `status(request, wait)` | The same shape, for an existing request. It must work from any server process: read files and the queue, not in-memory handles. |
| `bench(names, request)` | `{passed, failed, skipped, cases: {name: {checks: [{quantity, expected, got, rel_error, tol, pass}], ...}}}`. |
| `read_file(path, max_mb)` | Base64 of a file under `<KEY>_OUT` only. |
| `tool_status()` | Versions and what the machine has: `{translator, <backend key>: ..., procs, executor: {...}, <other translator>: version or null}`. llm-translators reads it, as described below. |
| `jobs(limit, active)`, `cancel(request, names)` | As in docs/EXECUTION.md. |

**A study's result** (`<name>.json`) has these keys:

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
| `files` | paths of what was written |
| `seconds` | timing |
| `execution` | the run record (docs/EXECUTION.md) |

**Interface keys.** Keys other translators read, such as `feat_material` and loads, are listed in the README under "Interface (read by other tools; kept stable)".

### cli.py

```
<key> format                         the study format
<key> validate studies.json          static checks
<key> run studies.json [NAMES...]    run and wait; one summary line per study, JSON with --json
<key> bench [NAMES...] [--queue]     benchmarks
<key> status                         versions, backend found or not, executor (JSON)
<key> status REQUEST                 a request's studies
<key> jobs [--active]  /  <key> cancel REQUEST [NAMES...]
<key> run-study REQUEST NAME         one study, blocking: what a SLURM job runs
```

The output is JSON (or short summaries with JSON on request), and `-` reads studies from stdin. `<key> status` with no arguments must print JSON with the backend's key, because llm-translators parses it.

### mcp_server.py

Copy lammps-translator's file. It works with the MCP Python SDK 1.x and 2.x, and has `--http PORT [--host H]`. Rules:

- **Tool names:**
  - `study_format`, `validate_studies`, `run_studies`, `study_status`, `run_benchmarks`, `jobs`, `cancel`, `get_file`.
  - Plus a "show me the inputs without running" tool, such as `get_inputs`, `get_deck` or `get_case`.
- **Waiting.** `run_studies` and `study_status` take `wait_s` capped at 50 s, because MCP calls must return well inside the client's timeout. Long studies come back `pending`, with `progress` (stage, iteration, `eta_min_max`).
- **The server never prints to stdout,** because stdout is the MCP channel. Logs go to stderr.
- **Instructions:** at most about 25 lines, in this order:
  1. What it does (one paragraph).
  2. Workflow: read `study_format`, `validate_studies` and fix every error, then `run_studies`.
  3. What to check before trusting numbers (`reliable`, the checks, `warnings`, `selection`).
  4. Polling: "polling costs tokens: when a run needs more than about 5 minutes, tell the user when it should finish and stop".
  5. The SLURM paragraph from docs/EXECUTION.md.
  6. Units.
- **Docstrings.** Each tool's docstring says what it returns, in one paragraph.

### Settings (environment variables)

These are read from the environment only, never from shell startup files. llm-translators' `bin/mcp` loads them from `config/env`.

| Variable | Meaning |
|---|---|
| `<KEY>_<BACKEND>` | Where the backend is: a command, or a script to source, like `CFDT_FOAM`. |
| `<KEY>_OUT` | The output folder (`~/<key>_runs`). |
| `<KEY>_WORKERS` | Studies at once in the local pool (2). |
| `<KEY>_TIMEOUT` | Seconds per study. |
| `<KEY>_PROCS` | MPI ranks or threads per study (1). A study's `procs` overrides it. |
| `<KEY>_SOLVER` | `auto` / `<backend>` / `ref`, when there is a reference solver. |
| `<KEY>_EXECUTOR`, `<KEY>_SLURM_*` | SLURM, as in docs/EXECUTION.md. |

### Long runs and SLURM

Implement [EXECUTION.md](EXECUTION.md) from the start:

- **Executors.** A local pool, or one SLURM job per study (`job.sh`, `job.json`, `<key> run-study`).
- **States.** The states and the `execution` run record in every result.
- **Tools.** `jobs` and `cancel`, and the `executor` key in `tool_status()`.
- **Memory and time.** `<KEY>_SLURM_MEM=auto` needs a memory estimate from the study, and `<KEY>_SLURM_TIME=estimate` needs a time estimate. So give every study a time estimate before it runs; it also feeds `eta_min_max` and `<KEY>_SLURM_MIN_MINUTES`.

### Tests

- **Without the backend.** pytest covers the format, validation, translation (the written inputs), the result parsing on stored logs, the reference implementation and the checks.
- **With the backend.** The backend tests skip when it is not found, with a reason.
- **SLURM.** Use fake `sbatch`/`squeue`/`scancel`/`sacct` (`tests/fakeslurm.py`): submit, then queued/running/done, collection by a new process, and each failure state.
- **On the servers.** The development sandbox kills background processes after about 3 minutes, so run the long benchmarks on the servers (`<key> bench` on carbonio or mochi, or through MCP).

### Conventions

- **Versions** are `YYYY.MM.DD.x`, in `__init__.py`, `pyproject.toml`, every module's docstring (`Version: ...`) and the README's version table. Update the README with every change.
- **README sections,** in order:
  - What it is, and how it relates to the other translators.
  - Install, with backend notes; point to llm-translators for the venv.
  - Use, A study, Results.
  - The checker, Benchmarks, Assumptions about <backend>.
  - Long runs: SLURM.
  - How it works.
  - For LLM tools: the MCP server.
  - Files, Versions.
- **CLAUDE.md** (or `context_new_version.md`) holds the state log: what was tested where, with numbers, and what is still to do. A new chat starts from it.
- **Lessons from the first four:**
  1. Never rely on the backend's numbering.
  2. Make checks exact where possible, and loose only with a stated reason.
  3. Every run checks itself.
  4. Validate before running.
  5. Keep `study_format` compact.
  6. Report a long run as pending with an ETA, and tell the user when to come back.
  7. Write result files atomically.
- **Authorship and commits.** The author is Nicola Ferralis <ferralis@generalflash.com>. Commits and pushes are done from the Mac.

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

Add an entry. The fields, with what `llmt` does with them:

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
 "tuning": {"procs": "<KEY>_PROCS", "workers": "<KEY>_WORKERS", "workers_default": 2,
            "set": {}, "why": ""},         // llmt tune: ranks per study from the cores; omit if not parallel
 "backends": [
  {"key": "<status key>", "name": "<Backend>", "doc": "<anchor in BACKENDS.md>",
   "without": "what still works without it"}
 ],
 "env": {"<KEY>_<BACKEND>": "...", "<KEY>_OUT": "...", "<KEY>_TIMEOUT": "...", "<KEY>_PROCS": "...",
         "<KEY>_EXECUTOR": "...", "<KEY>_SLURM_MIN_MINUTES": "...", "<KEY>_SLURM_ARGS": "..."}
}
```

(JSON has no comments: drop them.)

- **`backends[].key` must match a key of the translator's `tool_status()`.** `llmt` reads that value as:
  - **missing:** `null`, `false`, or a dict with `error`;
  - **found:** a dict with `version`, `exe` or similar, or a string such as a path. An absolute path that does not exist counts as missing.
- **`env` lists every setting the translator reads.** It becomes the commented template in `config/env`, and setup prompts for the backend paths among them.

### 4. Document the backend

- **`BACKENDS.md`:** a row in the table, and a section whose anchor is `backends[].doc`. The section covers how to install it on each platform, how it is found, which setting overrides that, and how to check it. Do not script the installation: `llmt` only reports what is missing.
- **`README.md`:** a row in the translators table at the top (what it does at the pinned commit).

### 5. Check, then commit the pin

```bash
./llmt list                       # the new row
./llmt install <key>              # checks out the submodule, pip install -e, reports backends
./llmt status                     # version, commit 'pinned', backends ok, executor
./llmt test <key>                 # its pytest
./llmt bench <key>
./llmt mcp setup                  # local or remote on the client
./llmt mcp test <key>             # handshake as the Claude app does it, tool list
./llmt tune <key>                 # if it has executor/tuning: SLURM and ranks
git add .gitmodules <name>-translator translators.json BACKENDS.md README.md
git commit -m "Add <name>-translator" && git push
```

Later versions: `./llmt pin <key> --commit --push` here, then `./llmt update` on each machine, and reconnect the Claude app.

### 6. Connect it to the other translators

- **Shared keys.** If it consumes another translator's output, add a chain benchmark on its side, and list the key under that translator's "Interface" section.
- **`uses`.** If another translator should offer this one, add it to that translator's `uses` in `translators.json`.

---

## Checklist

**In the translator's repository:**

- [ ] Research notes: backend versions per machine, how it is found, scope, typical requests, assumptions with what catches each, benchmarks with tolerances.
- [ ] Names: repo, package, key, `<key>-mcp`, `<KEY>_` prefix, port.
- [ ] `format.py`: compact `study_format`; `validate` with fixable errors.
- [ ] Translation with our own numbering; the backend's inputs written by us.
- [ ] Checks on every run; reference implementation; `reliable` / `unreliable_because`; `expect`.
- [ ] `bench.py`: one or more benchmarks per analysis kind, plus chains.
- [ ] `api.py` contract; `tool_status()` with the backend key and `executor`.
- [ ] `cli.py`, including `status` (JSON) and `run-study`.
- [ ] `mcp_server.py`: tool names, `wait_s` ≤ 50, instructions (workflow, trust, polling, SLURM, units), nothing on stdout.
- [ ] SLURM per docs/EXECUTION.md: states, `execution` record, `jobs`, `cancel`, memory and time estimates.
- [ ] Tests: offline, with the backend (skipping without it), and fake SLURM.
- [ ] README sections; `CLAUDE.md` state log; versions `YYYY.MM.DD.x` everywhere.
- [ ] Benchmarks pass on the server(s) that will run it.

**In llm-translators:**

- [ ] Submodule added (relative URL, branch main).
- [ ] `translators.json` entry; backend keys match `tool_status()`; all settings in `env`.
- [ ] `BACKENDS.md` section and table row; `README.md` row.
- [ ] `llmt install / status / test / bench / mcp test / tune` pass.
- [ ] Committed and pushed; `llmt update` and `llmt install <key>` on the machines that run it; `llmt mcp setup` on the clients.
