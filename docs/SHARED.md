# A shared install for all users

Status: design and implementation, llmt 2026.10.22.1; old servers and pin check 2026.10.25.2.

Today each user who runs the translators on a server (mochi, carbonio, a DGX Spark) has a personal llm-translators checkout with its own venv, and has to keep it up to date. This design adds a **shared install**:

- The admin maintains one install in `/opt/llm-translators`, and every user's MCP servers run from it.
- Updating it updates everyone at once.
- Work that is already running is never disrupted by an update.

Personal checkouts keep working as before: they are what a developer uses. Both kinds can live on the same machine.

## Layout

```
/opt/llm-translators/                       the prefix (llmt deploy --prefix; default /opt/llm-translators)
  releases/2026.10.22.1/                    a complete, read-only install:
    llmt, bin/, translators.json, docs/       the umbrella at its commit
    build123d-translator/ ... (sources)       each deployed translator at its pinned commit (git archive)
    .venv/                                    its own venv: the translators installed from these sources
    RELEASE.json                              what this release is (below); marks the folder as a release
  releases/2026.10.25.1/
  current -> releases/2026.10.25.1          the release new sessions use
  src/                                      (suggested) the admin's checkout that releases are built from

/etc/llm-translators/env                    machine settings: backend paths, SLURM defaults, caps (admin)
~/.config/llm-translators/env               a user's own settings (optional)
~/.config/llm-translators/setup.json        a user's choices: MCP placement, alias
~/cfdt_runs, ~/lmpt_runs, ...               results and caches: per user, as before
```

`RELEASE.json`:

```json
{"release": "2026.10.22.1", "llmt": "2026.10.22.1", "created": "2026-10-22T10:15:00", "built_by": "root",
 "python": "/usr/bin/python3 (3.12.3)", "keys": ["b3dt", "feat", "cfdt", "lmpt", "omct"],
 "umbrella": "dc5a14f", "commits": {"cfd-translator": "2df6416", "...": "..."}, "source": "/opt/llm-translators/src"}
```

**Why releases side by side, and not an update in place.** A server process imports the translator when it starts, but it spawns workers later, and SLURM jobs start later still. All of these would load whatever code is on disk at that moment. After an in-place update, a request made by the old code would be run or collected by the new code. With releases:

- `bin/mcp` resolves its real path (`pwd -P`). A server started through `current` therefore runs from `releases/<X>`, and so do its workers.
- The jobs it submits run `releases/<X>/.venv/bin/python`.
- Switching `current` affects only sessions that start afterwards. Running servers and queued jobs finish on the release they started with.

**Editable installs from the release's own copy.** Each translator is installed with `pip install -e` from the sources inside the release. The release is never changed after it is built, so this is as fixed as a wheel. It also keeps every data file the translators read from their package folders (benchmark references, templates), which a wheel could leave out. After installing, `compileall` writes the bytecode, since users cannot write `__pycache__` there.

## Settings: three layers

`bin/run`, `bin/mcp` and `bin/activate` load, in this order:

1. **`/etc/llm-translators/env`**: the machine. The admin keeps the backend paths here (`CFDT_FOAM`, `LMPT_LMP`, `OMCT_OMC`, `OPENMODELICALIBRARY`), along with the SLURM defaults (`*_EXECUTOR`, `*_SLURM_*`) and the caps (`*_MAX_*`). `LLMT_SYSTEM_ENV` points elsewhere, for tests.
2. **The user's file:** `~/.config/llm-translators/env` with a shared release (`$XDG_CONFIG_HOME` is respected), or `config/env` inside a personal checkout. This is for personal preferences: output folders, `*_PROCS`, `*_WORKERS`, timeouts.
3. **The MCP entry's `VAR=value` arguments** (`bin/mcp cfdt CFDT_PROCS=8`).

**Caps.** With a shared release, the machine file's `*_MAX_*` values are applied again after the user's file, and `bin/mcp` ignores `*_MAX_*` arguments, saying so on stderr. So a user cannot raise them. The caps bound what a *study* may ask for (`run`, docs/EXECUTION.md); they don't police a user's own defaults. Real limits on a shared machine belong in SLURM: per-user or per-QOS limits on CPUs, memory and time. The caps only keep studies within what the admin intends.

A personal checkout also reads `/etc/llm-translators/env` first, so its backend paths work without being copied, but nothing is enforced there.

## Commands

**Admin**, run from a checkout (e.g. `/opt/llm-translators/src`), with write access to the prefix (usually as root):

| Command | What it does |
|---|---|
| `llmt deploy [KEY...] [--prefix P] [--python PY] [--test] [--bench] [--no-switch]` | Builds a new release and switches `current` to it (detailed below) |
| `llmt releases` | Lists the releases, with which one is current, their translators and versions, and whether they are in use |
| `llmt rollback [RELEASE]` | Switches `current` back to the previous release (or the one named) |
| `llmt servers [--stop]` | Lists the translator MCP servers running on the machine (user, release) and flags those not on `current`; `--stop` ends them (SIGTERM), never jobs or other processes |
| `llmt gc [--keep N]` | Deletes old releases: never `current`, never one in use, and always keeps the newest N (default 3) |
| `llmt tune --system`, `llmt env --system [--edit]` | Machine settings in `/etc/llm-translators/env` |

`llmt deploy` in detail:

1. Checks the checkout: each translator to deploy is checked out at its pinned commit. Without `--allow-dirty`, the umbrella has no uncommitted changes.
2. Names the release with the date and the next free index (`YYYY.MM.DD.N`), and builds it in `releases/.staging-<name>`.
3. Copies the umbrella and each translator with `git archive`, so only committed files go in.
4. Creates the venv with `--python` (on Linux ARM with Gmsh: the system's Python, and links `python3-gmsh`). Installs each translator with its extras and its test extra, then compiles the bytecode.
5. Writes `RELEASE.json` and makes the release read-only (`a+rX`, `go-w`).
6. Creates `/etc/llm-translators/env` from the template if it is missing, or adds the descriptions of new settings. It never changes a value.
7. Runs `llmt status --json` of the new release. With `--test` it also runs each translator's tests, and with `--bench` the benchmarks. A failure stops the deploy before the switch.
8. Renames the staging folder to its release name and switches `current` in one atomic step (a new symlink renamed over the old one), unless `--no-switch`.

A typical update:

```bash
cd /opt/llm-translators/src
sudo git pull && sudo git submodule update --init      # the umbrella's pins, as committed with llmt pin
sudo ./llmt deploy --test
```

`deploy`, `rollback` and `gc` work from a checkout, not from inside a release. A release can't build or remove itself.

**Users** run `/opt/llm-translators/current/llmt`, or `llmt` through the alias's environment:

| Command | With a shared release |
|---|---|
| `llmt status` | the release, its translators, the backends, the settings files in effect, the SLURM load |
| `llmt setup` | skips installing (the admin does that). It offers the alias, then the MCP servers for a Claude app on this machine. |
| `llmt mcp setup / config / test` | as before; local entries run `/opt/llm-translators/current/bin/mcp KEY` |
| `llmt alias` | `alias llmt-venv='source /opt/llm-translators/current/bin/activate'` |
| `llmt tune`, `llmt env [--edit]` | the user's own file |
| `llmt jobs`, `llmt test`, `llmt bench` | as before (tests run without writing caches into the release) |
| `llmt install / remove / update / pin` | refused, saying who updates a shared release and how |

**On a client** (the Mac), `llmt mcp setup` → *remote* checks the host over ssh. If it finds `/opt/llm-translators/current` there, and no personal install at the folder you named, it proposes the shared one. The entry is then:

```json
"cfdt": {"command": "/usr/bin/ssh",
         "args": ["-T", "-o", "BatchMode=yes", "nicola@mochi.mit.edu", "/opt/llm-translators/current/bin/mcp", "cfdt"]}
```

To pin a client to one release, for a project that must not change mid-way, use that release's path instead of `current`.

## How llmt tells which kind of install it is

- **A release:** `RELEASE.json` sits next to `llmt`. The prefix is then two levels up, and user state lives in `~/.config/llm-translators/`.
- **A checkout:** anything else. Everything works as before, with `config/` inside the checkout.

The shell scripts use the same test.

## What the translators must respect

Nothing new, but a shared release makes it a requirement: a translator writes only to its output folder (`<KEY>_OUT`, under the user's home), to its caches (configurable, also under the home), and to temporary folders. It never writes inside its package folder or the venv. This was checked on 2026-10-22:

- cfdt's geometry library is under `CFDT_OUT/geometry`.
- omct's build cache is `OMCT_CACHE`, under `~/omct_runs`.
- omct's job scripts put the package's folder on `PYTHONPATH`. That is the release's copy, which is correct.

A running server keeps the release it started from (`bin/mcp` resolves `current` once), and the router keeps its servers for hours. So after a deploy a translator's server should notice that `<prefix>/current` now points to another release and, while idle, refuse a call with a message containing `STALE_SERVER` and exit. The router (2026.10.23.5) then drops that server and repeats the call once on a new one. Implemented by all four (2026-10-25):

- **omct** 2026.10.23.3. An idle stale server also exits within 20 s by itself. Since 2026.10.25.1, only a running server refuses calls or exits; tests and `run-study` run normally in a release that is not yet current.
- **cfdt** 2026.10.25.4 (`CFDT_EXIT_WHEN_STALE`; only a server started by `main()` refuses or exits).
- **lmpt** 2026.10.25.1 (`stale.py`, as cfdt; `LMPT_EXIT_WHEN_STALE`, `LMPT_STALE_CHECK_S` 20 s).
  - A busy server (a call in progress, or a study in its local worker pool) still answers, adds a `server_stale` note, and exits once idle.
  - In a checkout, a changed `__init__.py` version also counts as stale.
- **feat** 2026.10.25.1 (`FEAT_EXIT_WHEN_STALE`; only a server started by `main()` refuses or exits).

`<KEY>_EXIT_WHEN_STALE`: `1` always, `0` never; by default only servers from a shared release exit.

**Backstops** (llmt 2026.10.25.2):

- **The router.** It compares the version each server process reports (`"translator"` in its answers) with the installed one. It shows it as `serving` / `server_stale` in `machines()`, and closes such a server once idle (docs/ROUTER.md, "Servers that run an old release"). This catches servers older than the rule above, which never exit.
- **`llmt servers [--stop]`,** run on the machine as the admin.
  - It lists the running translator MCP servers: each process whose program or script is `<root>/.venv/bin/<key>-mcp`, with its user and its release. The path is resolved with `readlink -f`, so a symlinked prefix works.
  - It flags those whose release is not `current`.
  - **`--stop`** sends SIGTERM to those only. It never touches SLURM jobs (`<key> run-study`), personal checkouts, or its own command line, so `ssh host 'sudo .../llmt servers --stop'` is safe.

  Jobs already running keep their code. Only new submissions use the new release.
- **`llmt deploy`'s pin check.** Before building, it shows each translator's pinned commit and version against the newest commit on its tracked branch. When a pin is behind, it warns, and asks in interactive use. This catches a deploy made before `llmt pin` was committed and pulled, which is how the old cfdt reached spark2.
  - `--yes` only warns.
  - `--no-pin-check` skips the check, e.g. without network access.

Libraries a backend installs per user, such as OpenModelica's `~/.openmodelica/libraries`, are better installed once by the admin. Point to them in the machine file, e.g. `OPENMODELICALIBRARY=/opt/openmodelica/libraries`.

## Security notes

- **The release is read-only for users.** Users can't change code that other users run.
- **The machine file is read by every user's server.** It is shell syntax, sourced by `bin/run`, so only the admin may write it (`root:root 0644`). The user's file is sourced only for that user.
- **Results stay private.** Results and caches stay in each user's home, with their permissions. SLURM shows everyone's jobs, but `jobs` and `llmt jobs` list only your own.
