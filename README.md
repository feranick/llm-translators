# llm-translators

One place to install and run the translators that connect LLMs to engineering and science tools. Each translator stays in its own repository and is included here as a git submodule. This repository supplies the parts they share:

- one Python venv;
- an installer that lets you choose which translators each machine gets;
- settings per machine and per user;
- the MCP server entries for the Claude app, local or over ssh;
- optionally, a shared install for every user of a server.

| Key | Translator | What | Backends |
|---|---|---|---|
| `b3dt` | [build123d-translator](https://github.com/feranick/build123d-translator) | CAD parts from goals, i.e. ordered features (bosses, holes, pockets, revolves, patterns, fillets, lofts, sweeps, threads, text), turned into build123d solids, STEP files and scripts | none (pip only) |
| `feat` | [fea-translator](https://github.com/feranick/fea-translator) | FEA: linear static, modal, linear buckling, steady heat transfer (with thermal stress), DC electric conduction, electrostatics, Joule heating | Gmsh, CalculiX |
| `cfdt` | [cfd-translator](https://github.com/feranick/cfd-translator) | CFD: steady flow, laminar or k-ω SST, internal or external, 2D or 3D. Incompressible, or with `energy`: compressible air from low speed to transonic, heated or cooled walls, buoyancy. Rotating zones (pumps, fans) | Gmsh, OpenFOAM |
| `lmpt` | [lammps-translator](https://github.com/feranick/lammps-translator) | Atomistic: crystals and molecular liquids. Minimization, NVE/NVT/NPT MD, elastic constants, EOS, vacancies, surfaces, thermal expansion. Results include an FEA material for feat | LAMMPS |
| `omct` | [openmodelica-translator](https://github.com/feranick/openmodelica-translator) | System dynamics on the Modelica Standard Library: circuits, translational and rotational mechanics, DC motors, heat paths, controllers (PI/PID/LimPID). Transient, steady, linearisation (modes, Bode), sweeps on one compiled model; energy balance and an independent reference on every run; takes feat's stiffness, mass and conductance and b3dt's masses | OpenModelica + MSL 4.0.0 |

Each translator's README has the details. This table describes the commits pinned here (`llmt status` shows them).

**The translators work together:**

- fea and cfd take b3dt goals as geometry;
- cfd passes wall loads to fea;
- lammps produces fea materials;
- omct takes feat's and b3dt's results as lumped parameters.

That only works when they are installed in the same venv, which is why there is a single venv here.

Version: 2026.10.22.1.

**Contents:**

- [Installation](#installation): prerequisites, then three paths (one machine; a Mac and Linux workstations; a shared server), what setup does, and checking an install
- [Commands](#commands)
- [Settings](#settings)
- [Long runs: SLURM](#long-runs-slurm)
- [Updating](#updating)
- [How it fits together](#how-it-fits-together)
- [Moving and migrating](#moving-and-migrating)
- [Troubleshooting](#troubleshooting)
- [Uninstalling](#uninstalling)
- [Other documents](#other-documents)

---

## Installation

### Before you start

On every machine that will run translators:

- **git** and **Python 3.10+** for the venv. `llmt` itself runs on Python 3.7+, using only the standard library. On macOS, `/usr/bin/python3` is 3.9: `llmt` runs with it, but builds the venv with the newest `python3.1x` it finds (Homebrew, MacPorts, python.org), or the one given with `--python`.
- **Internet access** to github.com and PyPI (pip downloads the translators' Python dependencies).
- **The backends of the translators you will run there,** installed first. [BACKENDS.md](BACKENDS.md) has the details for each:

  | Translator | Backend |
  |---|---|
  | feat | Gmsh, CalculiX |
  | cfdt | Gmsh, OpenFOAM .org 11+ (14 is the target) |
  | lmpt | LAMMPS |
  | omct | OpenModelica with the Modelica Standard Library 4.0.0, and a C compiler |
  | b3dt | nothing (pip only) |

  `llmt` never installs backends: it reports what is missing and where to look.

On the machine with the **Claude desktop app** (usually a Mac):

- **Keep llm-translators outside `~/Desktop`, `~/Documents`, `~/Downloads` and iCloud Drive,** e.g. in `~/Software`. macOS protects those folders: the Claude app can be refused permission to start the servers there, while Terminal works ([Troubleshooting](#troubleshooting)).
- **For translators on another machine: ssh with a key and no password prompt.** The MCP entries use `ssh -o BatchMode=yes`, so the connection must work without a password or a question.

  ```bash
  ssh-keygen -t ed25519                     # once, if you have no key (~/.ssh/id_ed25519)
  ssh-copy-id nicola@mochi.mit.edu          # once per server
  ssh nicola@mochi.mit.edu true             # the first time: accept the host key
  ssh -o BatchMode=yes nicola@mochi.mit.edu true && echo ok    # must print ok without asking anything
  ```

### Choose a layout

| Layout | When |
|---|---|
| [A. One machine](#a-one-machine) | Everything runs where the Claude app runs, e.g. a Linux workstation with a desktop, or a Mac with only b3dt and feat |
| [B. A Mac and Linux workstations](#b-a-mac-and-linux-workstations) | The usual case: light translators (b3dt, feat) on the Mac, heavy ones (cfdt, lmpt, omct) on servers reached over ssh |
| [C. A shared server install](#c-a-shared-server-install) | A server with several users: the admin installs and updates once, for everyone |

Layouts mix: a Mac can use one workstation through a personal install and another through a shared one.

### A. One machine

```bash
mkdir -p ~/Software && cd ~/Software
git clone https://github.com/feranick/llm-translators.git
cd llm-translators
./llmt setup
```

In setup ([what setup does](#what-setup-does)):

1. Answer *here* for every translator you want.
2. Let it create the venv.
3. Fix any backend reported missing (BACKENDS.md), then enter its path if it is installed somewhere unusual.
4. Accept the alias.
5. Answer *local* for each MCP server, and let it write the Claude config.

Then [check the install](#checking-an-install) and restart the Claude app.

### B. A Mac and Linux workstations

Do the workstations first, so the Mac's setup can check them.

**On each workstation** (e.g. mochi), logged in as yourself:

```bash
mkdir -p ~/Software && cd ~/Software
git clone https://github.com/feranick/llm-translators.git
cd llm-translators
./llmt setup
```

1. **Step 1:** answer *here* for the translators this machine runs, e.g. `cfdt`, `lmpt`, `omct`, and accept `feat` and `b3dt` when offered (the others use them).
2. **Steps 2–3:** the venv and the backends. The run limits are suggested from the machine's cores, and SLURM is used if `sbatch` is found.
3. **Alias:** accept it.
4. **"Will a Claude app on this machine use the translators?":** *no*.

Then check it:

```bash
./llmt status                 # every translator: a version, backends ok
./llmt bench lmpt             # optional: the benchmarks of a translator (a few seconds to minutes)
```

**On the Mac:**

```bash
mkdir -p ~/Software && cd ~/Software
git clone https://github.com/feranick/llm-translators.git
cd llm-translators
./llmt setup
```

1. **Step 1:** *here* for `b3dt` and `feat`, *remote* for `cfdt`, `lmpt`, `omct`. These are the defaults.
2. **Steps 2–3:** only b3dt and feat are installed. Install Gmsh and CalculiX first if feat should run on the Mac (BACKENDS.md).
3. **Step 4:** b3dt and feat are *local*. For each remote one:
   - give the host (`nicola@mochi.mit.edu`);
   - let setup check it over ssh: it finds llm-translators there (or a shared install, layout C) and reports versions and missing backends;
   - confirm the folder (`~/Software/llm-translators`).
4. **Saving and testing:** let it write the Claude config (a backup is kept), and accept the test it offers.

Then restart the Claude app, and [check the install](#checking-an-install). Translators can sit on different servers, e.g. cfdt and omct on mochi and lmpt on carbonio: give each its host in step 4.

### C. A shared server install

The admin installs the translators once in `/opt/llm-translators`. Users don't keep their own copies, and an update reaches all of them, while running work finishes on the version it started with. The design is in [docs/SHARED.md](docs/SHARED.md).

**The admin, once:**

1. **Backends, system-wide** (BACKENDS.md). For omct, install the Modelica library where every user can read it, not in the admin's home ([BACKENDS.md](BACKENDS.md#openmodelica)).
2. **A source checkout and the first release:**

   ```bash
   sudo git clone --recurse-submodules https://github.com/feranick/llm-translators.git /opt/llm-translators/src
   cd /opt/llm-translators/src
   sudo ./llmt deploy --test
   ```

   `deploy` does the following:
   - builds `/opt/llm-translators/releases/<YYYY.MM.DD.N>/`, with its own venv and every translator checked out in the source checkout at its pin;
   - runs their tests (`--test`), and their benchmarks if you add `--bench`;
   - points `/opt/llm-translators/current` to the release;
   - creates `/etc/llm-translators/env`, the machine's settings, if it is missing.
3. **The machine's settings,** for everyone:

   ```bash
   sudo ./llmt tune --system             # SLURM, MPI ranks per study, timeouts, from the machine's cores
   sudo ./llmt env --system --edit       # backend paths (CFDT_FOAM, LMPT_LMP, OMCT_OMC, ...), caps (*_MAX_*)
   ```
4. **Check it as a normal user:**

   ```bash
   /opt/llm-translators/current/llmt status
   /opt/llm-translators/current/llmt releases
   ```

If git refuses the checkout with "dubious ownership" (the checkout belongs to another user than the one running git), run `sudo git config --global --add safe.directory /opt/llm-translators/src`.

**Each user, on the server:**

```bash
/opt/llm-translators/current/llmt setup
```

Installing is skipped. Setup shows the release and its translators, offers the `llmt-venv` alias, and, if a Claude app runs on this machine, writes MCP entries that run `/opt/llm-translators/current/bin/mcp KEY`. Personal settings, if any, go in `~/.config/llm-translators/env` (`llmt env --edit`); the machine's caps always win.

**Each user, on their Mac:** layout B, Mac part. In step 4 → *remote*, setup finds `/opt/llm-translators/current` on the host and proposes it as the folder.

**Updates:**

```bash
cd /opt/llm-translators/src
sudo git pull && sudo git submodule update --init
sudo ./llmt deploy --test
```

- **Running work isn't disrupted.** New sessions get the new release. Running servers, and the SLURM jobs they submitted, finish on theirs.
- **`sudo ./llmt rollback`** goes back.
- **`sudo ./llmt gc`** removes old releases that nobody uses; it always keeps the newest 3.

**Moving users from personal installs:** each user runs `llmt mcp setup` on their Mac with the shared folder, then removes their personal `~/Software/llm-translators` on the server (keep `~/cfdt_runs` and the other results folders: they are unchanged).

### What setup does

`llmt setup` in a personal checkout has four steps:

1. **Where each translator runs, seen from this machine.** For each one: *here* (installed on this machine), *remote* (installed on another machine and reached over ssh; nothing is installed here for it), or *not used*.
   - The defaults follow how heavy each one is: b3dt and feat *here*; cfdt, lmpt and omct *remote*.
   - On a machine without the Claude app, the default is *here* for the heavy ones.
   - Choosing a translator offers the ones it uses (b3dt with feat, for example).
2. **Python environment.** It creates `.venv`, checks out only the chosen submodules, and runs `pip install -e` on each with its extras (`mcp`, `mesh`, optionally `test`). On Linux ARM it can link the system's Gmsh into the venv.
3. **Backends and run limits.**
   - It reports whether each backend is found, using each translator's own `status` command. Paths can be entered into `config/env`.
   - It offers SLURM when `sbatch` works, and suggests MPI ranks per study and timeouts from the machine's cores ([Settings](#settings)).
   - It offers the `llmt-venv` alias.
4. **MCP servers.** For the Claude app on this machine, each translator is one of:
   - *local*: llm-translators here;
   - *remote*: llm-translators, personal or shared, on another machine over ssh;
   - *existing*: an older install outside llm-translators ([Moving and migrating](#moving-and-migrating));
   - *none*.

   It writes the entries into `claude_desktop_config.json` (a backup is kept), and offers to test each server the way the Claude app starts it.

Setup can be run again at any time: it remembers the earlier answers (`config/setup.json`).

### Checking an install

```bash
./llmt status              # versions, pinned commits, backends found, how each runs (local / SLURM), the queue
./llmt status -v           # plus each translator's own status
./llmt mcp test            # starts every MCP entry as the Claude app does (also over ssh): handshake and tools
```

**Then restart the Claude app.** Each server's tools appear in a new chat; a quick check is to ask for a translator's benchmarks, e.g. "run lmpt's madelung_nacl benchmark". If a server shows "Server disconnected", see [Troubleshooting](#troubleshooting).

---

## Commands

```
llmt setup                      guided setup (also what plain ./llmt does the first time)
llmt list                       the translators, their backends, what is installed here
llmt status [KEY...] [-v] [--json]   versions, commits, backends, how each runs, the SLURM queue
llmt install KEY... [--python P] [--extra lmpt:lammps] [--no-test] [--no-uses] [--link-system-gmsh]
llmt remove KEY...              pip uninstall (sources stay checked out)
llmt update [KEY...] [--latest] [--no-pull] [--no-tune]   pull, check out the pins, reinstall, fill config/env
llmt pin [KEY...] [--to C] [--commit [--push]]   move the pins to the latest pushed commits
llmt test [KEY...]              pytest in each translator
llmt bench KEY [NAMES...]       a translator's benchmarks
llmt tune [KEY...] [--print | --apply] [--system]   SLURM use, MPI ranks and timeouts from the machine's cores
llmt jobs [KEY...]              the translators' SLURM jobs on this machine
llmt env [--edit] [--system]    the settings file (config/env; yours in a shared release; --system: the machine's)
llmt alias [--name N] [--rc F]  shell alias for the venv (default llmt-venv; replaces the old 'cad')

llmt mcp setup                  local / remote / existing / none per translator
llmt mcp config                 write the entries into the Claude desktop config (backup kept)
llmt mcp config --print         the mcpServers JSON, for any MCP client
llmt mcp config --claude-code   'claude mcp add-json' commands for Claude Code
llmt mcp test [KEY...]          start each server as the Claude app does (bare environment), handshake, list tools;
                                on failure, the server's error output
llmt mcp serve [KEY...] [--host H]   streamable HTTP, each on its own port

admin, shared install (docs/SHARED.md), from a checkout:
llmt deploy [KEY...] [--test] [--bench] [--prefix P] [--python PY] [--no-switch]   build a release, switch to it
llmt releases                   the releases, which is current, which are in use
llmt rollback [RELEASE]         point 'current' to the previous (or the named) release
llmt gc [--keep N] [--yes]      remove old releases that nobody uses
```

**`KEY`** is `b3dt`, `feat`, `cfdt`, `lmpt` or `omct`, or the repository name, a number from `llmt list`, or `all`.

**HTTP ports** for `llmt mcp serve`: b3dt 8766, feat 8767, cfdt 8768, lmpt 8769, omct 8770.

In a shared release, `install`, `remove`, `update` and `pin` are refused: the admin updates it with `deploy`.

---

## Settings

The translators read their settings from the environment: backend paths, output folders, MPI ranks, timeouts, SLURM, caps. `bin/run`, `bin/mcp` and `bin/activate` load them in three layers. The MCP servers don't read `~/.bashrc`, because the Claude app and non-interactive ssh shells skip it, so settings belong here.

1. **The machine:** `/etc/llm-translators/env`, if there is one. The admin edits it with `llmt env --system --edit` or `llmt tune --system`. Every install on the machine reads it first.
2. **Yours:** `config/env` in a personal checkout, or `~/.config/llm-translators/env` with a shared release. Edit it with `llmt env --edit`. It starts as a commented template listing every variable the translators read. In a shared release, the machine's caps (`*_MAX_*`) are applied again afterwards, so a user cannot raise them.
3. **Per MCP server:** `VAR=value` arguments after the key in an entry, e.g. `bin/mcp cfdt CFDT_TIMEOUT=10800 CFDT_PROCS=18`. They override the files, for that entry only, so two clients can use the same workstation with different limits. In `llmt mcp setup`, answer yes to "Settings for this cfdt server"; the entry becomes:

   ```json
   "cfdt": {"command": "/usr/bin/ssh",
            "args": ["-T", "-o", "BatchMode=yes", "nicola@mochi.mit.edu",
                     "~/Software/llm-translators/bin/mcp", "cfdt", "CFDT_TIMEOUT=10800", "CFDT_PROCS=18"]}
   ```

   Put such settings after the key. Prefixing the command with `env CFDT_TIMEOUT=...` doesn't work, because the files are loaded afterwards.

**Run limits from the machine's cores.** `llmt tune` suggests them (`--print` only shows them, `--apply` writes them). `llmt setup`, `install` and `update` also fill in the ones `config/env` doesn't have yet:

- **SLURM:** the executor, when `sbatch` works.
- **MPI ranks per study:** the physical cores, minus 1 or 2 kept free, divided by the studies that may run at once (`CFDT_WORKERS`, `LMPT_WORKERS`; default 2). A 20-core workstation gets 9 ranks with two studies at a time, or 18 with one.
  - Hyperthreads don't count: they don't speed up MPI solvers.
  - With mixed core types (Apple performance and efficiency cores, ARM big.LITTLE such as the DGX Spark), `llmt` says so. Ranks on the slower cores hold the others back, so compare with as many ranks as there are performance cores.
- **Timeouts:** cfdt gets `CFDT_TIMEOUT=10800` (3 h per study).
- **What's kept:** values already set (also `local`, or an empty `VAR=`) are kept, and differences are reported. Descriptions of settings added since the file was written are added, commented out. `--no-tune` leaves the file alone.

A study's own `run` settings (`timeout_s`, `mem`, `procs`, `queue`) override these for that study, within the caps ([docs/EXECUTION.md](docs/EXECUTION.md)).

**An interactive shell.** `llmt-venv` activates the venv and loads the same settings; `deactivate` leaves it. `llmt alias` (or setup) writes this to `~/.bashrc` (`~/.zshrc` for zsh, or `--rc FILE`):

```bash
# >>> llm-translators >>>
# the llm-translators venv and its settings
alias llmt-venv='source ~/Software/llm-translators/bin/activate'
# <<< llm-translators <<<
```

With a shared release it points to `/opt/llm-translators/current/bin/activate`.

- **Old aliases** that activate a translator's own venv, such as the old `alias cad='source ~/Software/build123d-translator/.venv/bin/activate'`, are commented out in `~/.bashrc`, `~/.bash_aliases`, `~/.bash_profile`, `~/.profile` and `~/.zshrc`, and each changed file is backed up first.
- **Options:** `--keep-old` leaves the old aliases alone, `--list` only shows them, and `--print` only prints the line.

---

## Long runs: SLURM

On a workstation with SLURM (one node is enough), long studies run as SLURM jobs instead of inside the MCP server. Set `CFDT_EXECUTOR=slurm` (and `LMPT_`, `FEAT_`, `OMCT_`); `llmt setup` and `llmt tune` offer it when they find `sbatch`.

- **One queue for every chat.** Several projects running cfdt at once don't overload the machine: their studies wait in SLURM's queue.
- **Runs outlive the conversation.** A job keeps running when the chat closes or ssh drops.
- **Less polling.** The model submits, reports when to come back and stops. Later, or from another chat, it collects results with `study_status` or finds runs with `jobs`.
- **For you:** `llmt jobs` lists the translators' jobs, and `llmt status` shows how busy the machine is.

The contract the translators implement is [docs/EXECUTION.md](docs/EXECUTION.md): job files, states, the `jobs` and `cancel` tools, job sizing, and per-study `run` settings with caps. Setting up SLURM itself is in [BACKENDS.md](BACKENDS.md#slurm).

---

## Updating

The umbrella pins one commit of each translator, so a given umbrella commit is a combination that was installed and tested together. The submodules track `main` (`branch = main` in `.gitmodules`). Their URLs are relative (`../fea-translator.git`): cloning the umbrella over ssh fetches them over ssh, and over https over https.

- **Moving the pins,** after pushing new commits to a translator's repository: `llmt pin feat cfdt --commit --push`. It reads each translator's latest pushed `main`, moves the pin, lists the new commits and commits `Pin feat 02d34a8, cfdt c2f5668`.
  - It works whether or not the submodules are checked out here.
  - Without `--commit`, the new pins are only staged.
  - With no keys, every translator is checked.
  - `--to COMMIT` pins one translator to a given commit.
- **On each machine with a personal install:** `llmt update`. It pulls the umbrella, checks out the new pins of the translators installed there, reinstalls them, and fills in new settings. For a remote translator, that means on the machine that runs it.
- **On a shared server:** the admin runs `git pull`, `git submodule update` and `llmt deploy` in the source checkout ([layout C](#c-a-shared-server-install)).
- **Then restart the Claude app,** or reconnect the servers: an MCP server loads its code when it starts.
- **`llmt update --latest`** tries each translator's latest `main` on one machine without moving the pins. After testing (`llmt test`, `llmt bench`), record it with `llmt pin KEY --commit`.
- **Developing inside the umbrella.** Submodules with uncommitted changes are left alone by `update`. To work on a translator here, run `git checkout main` in its folder (submodules are checked out on a detached HEAD). It is installed in editable mode, so changes take effect at once.
- **Submodule states:** `llmt status` shows each one as `pinned`, `moved` (checked out at another commit than the pin), `absent`, or `release` (a shared release).

---

## How it fits together

```
llm-translators/
  llmt                  the manager (Python standard library only)
  translators.json      the registry: package, extras, CLI, port, backends, settings, executor, what each uses
  bin/run               runs a command with the venv and the settings:  bin/run feat status
  bin/mcp               starts one MCP server: bin/mcp KEY [VAR=value ...] [--http PORT] (what the Claude app runs)
  bin/activate          source it for an interactive shell with the venv and the settings
  bin/llmt-env.sh       loads the settings in their three layers (used by the three above)
  config/env            your settings in a personal checkout (not tracked)
  config/setup.json     what was chosen here: installed translators, extras, MCP placement, alias (not tracked)
  BACKENDS.md           installing Gmsh, CalculiX, OpenFOAM, LAMMPS, OpenModelica, MPI, SLURM
  docs/                 EXECUTION.md, SHARED.md, create_new_translators.md
  .venv/                the venv (not tracked)
  build123d-translator/ fea-translator/ cfd-translator/ lammps-translator/ openmodelica-translator/   submodules
```

A shared release has the same layout, plus `RELEASE.json`, and keeps user state in `~/.config/llm-translators/` instead of `config/`.

**MCP servers.**

- **One per translator.** Each keeps its own server, with its own name (`b3dt`, `feat`, `cfdt`, `lmpt`, `omct`), tools and instructions, so each client picks the ones it needs and where they run.
- **Every entry runs `bin/mcp KEY`,** locally or as `ssh HOST <folder>/bin/mcp KEY`, and that loads the settings before starting the server.
- **Other entries are left alone:** entries with other names in the Claude config, such as Taiga, are never changed.

---

## Moving and migrating

**Moving the folder.** The venv records its own path, so it is recreated:

```bash
mv ~/Desktop/llm-translators ~/Software/llm-translators
cd ~/Software/llm-translators
rm -rf .venv
./llmt install          # reinstalls what this machine had (config/setup.json is kept)
./llmt mcp config       # rewrites the local entries in the Claude config (backup kept)
./llmt alias            # points llmt-venv to the new place
./llmt mcp test
```

Then restart the Claude app. `config/env` moves with the folder.

**Translators installed before llm-translators.** Earlier, each translator had its own install, typically `build123d-translator/.venv` shared through the `cad` alias, locally or on a workstation. `llmt` finds these and keeps them working while you move over:

- **On the machine you set up:** `llmt setup` lists the `<key>-mcp` commands it finds outside llm-translators: on the PATH, in `~/Software/*/.venv`, `~/*/.venv`, `~/Desktop/*/.venv` and `~/.local/bin`. They are left untouched, and step 4 lets the Claude app use either.
- **In the Claude config:** step 4 shows each translator's current entry. If `llmt` didn't write it, the default choice is *existing*, which keeps it as it is.
- **On a remote machine:** if llm-translators isn't at the folder you give, `llmt` looks for an older install there over ssh and offers it for now (*existing*). Such an install doesn't read the settings files, so `llmt` puts the settings it needs into the entry: `ssh HOST env VAR=value ... /path/to/<key>-mcp`.

To finish moving over:

1. Install llm-translators on each machine (layouts B or C).
2. Rerun `llmt mcp setup` and choose *local* or *remote*.
3. Move backend variables that were in ssh commands into the settings files.
4. Run `llmt alias`; it replaces `cad`.
5. Once `llmt status` is all `ok`, delete the old venvs. First check nothing still uses one: `pgrep -af translator/.venv`.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Claude app: "MCP KEY: Server disconnected"; `~/Library/Logs/Claude/mcp-server-KEY.log` says `/bin/sh: .../bin/mcp: Operation not permitted` | llm-translators is in a folder macOS protects (`~/Desktop`, `~/Documents`, `~/Downloads`, iCloud Drive). Move it ([Moving the folder](#moving-and-migrating)). If it persists, look for a quarantine flag with `xattr -l bin/mcp`, and clear it with `xattr -dr com.apple.quarantine <folder>`. |
| `llmt mcp test` passes but the Claude app fails | Terminal and the app differ (permissions, environment). Read the app's log, `~/Library/Logs/Claude/mcp-server-KEY.log`, and restart the app after any change. |
| A remote server fails at once; `llmt mcp test` says `Permission denied (publickey)` or `Host key verification failed` | The ssh key isn't set up for BatchMode, or the host key was never accepted. Run `ssh-copy-id HOST`, then `ssh HOST true` once ([Before you start](#before-you-start)). |
| `llmt status`: a backend `MISSING` | Install it (BACKENDS.md), or set its path in the settings (`llmt env --edit`, or `--system` on a shared server): `FEAT_CCX`, `CFDT_FOAM`, `LMPT_LMP`, `OMCT_OMC`. |
| omct: `Modelica Standard Library 4.0.0: MISSING`, "Failed to open file ... .openmodelica/cache/...zip" | The `omlibrary` package's cache is incomplete. Install the library with OpenModelica's package manager instead ([BACKENDS.md](BACKENDS.md#openmodelica)). |
| feat or cfdt on Linux ARM: Gmsh missing | PyPI has no Gmsh wheels there. Run `sudo apt install python3-gmsh`, then `llmt install --link-system-gmsh KEY`; the venv must use the system's Python ([BACKENDS.md](BACKENDS.md#gmsh)). |
| `llmt status`: "executor 'slurm' set, but this version ignores it" | The installed translator predates SLURM support. Run `llmt update`, or `llmt pin` then `update`. |
| SLURM jobs wait with reason `Resources` while cores are free | Jobs ask for too much memory, or SLURM gives each job the whole node. Set `--mem` (e.g. `CFDT_SLURM_ARGS=--mem=16G`, `LMPT_SLURM_MEM=auto`), and check `slurm.conf` ([BACKENDS.md](BACKENDS.md#slurm)). |
| Changes don't show up in the Claude app | An MCP server keeps the code it started with. Restart the app, or reconnect the server. |
| Shared release: `permission denied` from `llmt` | Machine settings and releases belong to the admin: use `sudo`. Users change only their own settings (`llmt env --edit`). |
| Shared release: a user still gets the old version | Their server started before the switch: reconnect. `llmt releases` shows which releases are still in use. |
| `llmt gc` keeps an old release | A running server or a queued job still uses it. It goes once they finish. |

---

## Uninstalling

**A personal install:**

1. Remove the MCP entries. `llmt mcp setup`, answering *none* for each, removes the entries `llmt` wrote; or edit `claude_desktop_config.json` by hand.
2. Delete the block between `# >>> llm-translators >>>` and `# <<< llm-translators <<<` in `~/.bashrc` (or `~/.zshrc`).
3. Delete the folder (`rm -rf ~/Software/llm-translators`).

Results stay in `~/feat_runs`, `~/cfdt_runs`, `~/lmpt_runs`, `~/omct_runs` and `~/b3d_parts` until you remove them.

**A shared install** (the admin):

```bash
sudo rm -rf /opt/llm-translators /etc/llm-translators
```

Users then remove their entries and alias as above, plus `~/.config/llm-translators/`.

---

## Other documents

- [BACKENDS.md](BACKENDS.md): installing and checking each backend, MPI and SLURM.
- [docs/SHARED.md](docs/SHARED.md): the shared install's design (releases, settings layers, caps, security).
- [docs/EXECUTION.md](docs/EXECUTION.md): the SLURM contract the translators implement.
- [docs/create_new_translators.md](docs/create_new_translators.md): building a new translator and adding it here.
