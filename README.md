# llm-translators

One place to install and run the translators that connect LLMs to engineering and science tools. Each translator stays in its own repository and is included here as a git submodule. This repository supplies the parts they share: one Python venv, an installer that lets you choose which translators each machine gets, per-machine settings, and the MCP server entries for the Claude app, which can run locally or over ssh.

| Key | Translator | What | Backends |
|---|---|---|---|
| `b3dt` | [build123d-translator](https://github.com/feranick/build123d-translator) | CAD parts from goals: build123d solids, STEP files, scripts | none (pip only) |
| `feat` | [fea-translator](https://github.com/feranick/fea-translator) | FEA: static, modal, buckling, steady thermal | Gmsh, CalculiX |
| `cfdt` | [cfd-translator](https://github.com/feranick/cfd-translator) | CFD: steady incompressible flow | Gmsh, OpenFOAM |
| `lmpt` | [lammps-translator](https://github.com/feranick/lammps-translator) | Atomistic simulations | LAMMPS |

The translators work together: fea and cfd take b3dt goals as geometry, cfd passes wall loads to fea, and lammps produces fea materials. That only works when they are installed in the same venv, which is why there is a single venv here.

Version: 2026.10.18.1.

## Quick start

```bash
cd ~/Software        # anywhere
git clone https://github.com/feranick/llm-translators.git
cd llm-translators
./llmt setup
```

`llmt setup` walks through four steps:

1. **Where each translator runs, seen from this machine.** For each one: *here* (installed on this machine), *remote* (installed on another machine and reached over ssh; nothing is installed here for it), or *not used*. The defaults follow how heavy each one is: b3dt and feat *here*, cfdt and lmpt *remote*. On a machine without the Claude app, such as a workstation, the default is *here* for the heavy ones. If you choose a translator here, the ones it uses are offered too (b3dt with feat, for example).
2. **Python environment.** It creates `.venv`, checks out only the chosen submodules, and runs `pip install -e` on each with its extras (`mcp`, `mesh`, optionally `test`).
3. **Backends.** It reports whether Gmsh, CalculiX, OpenFOAM and LAMMPS are found, using each translator's own `status` command. It does not install them: see [BACKENDS.md](BACKENDS.md). You can enter their paths in `config/env`.
4. **MCP servers.** For the Claude app on this machine, choose for each translator where it runs: *local* (llm-translators here), *remote* (llm-translators on another machine, over ssh), *existing* (an install outside llm-translators, here or over ssh; see [below](#translators-already-installed-elsewhere)), or *none*. It writes the entries into `claude_desktop_config.json`, keeping a backup.

`llmt` needs only git and Python 3.7+ (standard library). The venv needs Python 3.10+. On macOS, `/usr/bin/python3` is 3.9: `llmt` runs with it but builds the venv with the newest `python3.1x` it can find (or the one given with `--python`).

## A typical layout

- **The Mac with the Claude app.** Run `./llmt setup`. In step 1, answer *here* for `b3dt` and `feat` and *remote* for `cfdt` and `lmpt` (these are the defaults). Only b3dt and feat are installed. In step 4, b3dt and feat are local, and cfdt and lmpt ask for the host (`nicola@carbonio`) and the folder (`~/Software/llm-translators`).
- **The Linux workstation (carbonio).** Run `./llmt setup` and answer *here* for `cfdt` and `lmpt`. Also accept `feat` and `b3dt`, which they use. Answer *no* to "Will a Claude app on this machine use the translators?".

The client's remote entry runs `ssh -T -o BatchMode=yes HOST <root>/bin/mcp KEY`, so the remote host needs key-based ssh and the umbrella at that path. When you choose *remote*, `llmt` offers to check the host over ssh: it runs `llmt status --json` there and reports the translator's version and missing backends. If llm-translators or the translator is not there yet, it says so, and it offers any older install it finds on that host (next section).

## Translators already installed elsewhere

Before llm-translators, each translator was installed on its own, typically in `build123d-translator/.venv` (shared by the others through the `cad` alias) or in its repository's own `.venv`, locally or on a workstation. `llmt` finds these installs and keeps them working while you move over:

- **On the machine you set up.** `llmt setup` first lists the `<key>-mcp` commands it finds outside llm-translators: on the PATH, in `~/Software/*/.venv`, `~/*/.venv`, `~/Desktop/*/.venv` and `~/.local/bin`. They are left untouched. Installing a translator here makes a separate copy in llm-translators' venv, and step 4 lets the Claude app use either one.
- **Current Claude entries.** Step 4 shows each translator's current entry in `claude_desktop_config.json`. If it was not written by `llmt`, the default choice is *existing*, which keeps it as it is.
- **On a remote machine.** If you choose *remote* and llm-translators is not at the given folder yet (or the translator is not installed in it), `llmt` looks for an older install on that host over ssh and offers to use it for now. Choosing *existing* and giving the host does the same search directly. The entry then runs `ssh HOST [env VAR=value ...] /path/to/<key>-mcp`. An install outside llm-translators does not read `config/env`, so `llmt` asks for any settings it needs (such as `CFDT_FOAM` or `LMPT_LMP`) and puts them into the entry.

To move a translator into llm-translators later, install it there and rerun `llmt mcp setup`, choosing *local* or *remote*. The old entry is replaced, and a backup of the config is kept.

## Commands

```
llmt setup                      guided setup (also what plain ./llmt does the first time)
llmt list                       the translators, their backends, what is installed here
llmt install KEY... [--python P] [--extra lmpt:lammps] [--no-test] [--no-uses] [--link-system-gmsh]
llmt remove KEY...              pip uninstall (sources stay checked out)
llmt status [KEY...] [-v] [--json]   versions, submodule commits, backends found
llmt update [KEY...] [--latest] [--no-pull]   pull, check out the pins, reinstall
llmt pin [KEY...] [--to C] [--commit [--push]]   move the pins to the latest pushed commits
llmt test [KEY...]              pytest in each translator
llmt bench KEY [NAMES...]       a translator's benchmarks
llmt alias [--name N] [--rc F]  shell alias for the venv (default llmt-venv; replaces the old 'cad')
llmt env [--edit]               per-machine settings (config/env)
llmt mcp setup                  local / remote / existing / none per translator
llmt mcp config                 write the entries into the Claude desktop config (backup kept)
llmt mcp config --print         the mcpServers JSON, for any MCP client
llmt mcp config --claude-code   'claude mcp add-json' commands for Claude Code
llmt mcp serve [KEY...] [--host H]   streamable HTTP, each on its own port (b3dt 8766, feat 8767, cfdt 8768, lmpt 8769)
```

`KEY` is `b3dt`, `feat`, `cfdt`, `lmpt` (or the repository name, a number from `llmt list`, or `all`).

## How it fits together

```
llm-translators/
  llmt                  the manager (standard library only)
  translators.json      the registry: package, extras, CLI, port, backends, settings, what each uses
  bin/run               runs a command with the venv and config/env:  bin/run feat status
  bin/mcp               starts one MCP server:  bin/mcp KEY [--http PORT]  (what the Claude app runs)
  bin/activate          source it for an interactive shell with the venv and config/env
  config/env            per-machine settings, shell syntax (not tracked)
  config/setup.json     what was chosen here: installed translators, extras, MCP placement (not tracked)
  BACKENDS.md           installing Gmsh, CalculiX, OpenFOAM, LAMMPS
  .venv/                the shared venv (not tracked)
  build123d-translator/ fea-translator/ cfd-translator/ lammps-translator/   submodules
```

**MCP servers.** Each translator keeps its own MCP server, with its own name (`b3dt`, `feat`, `cfdt`, `lmpt`), tools and instructions, so each client can pick the ones it needs and where they run. Every entry runs `bin/mcp KEY`, which loads `config/env` before starting the server. Backend paths set there therefore reach servers started by the Claude app or over a non-interactive ssh shell, neither of which reads `~/.bashrc`. Entries with other names in the Claude config, such as Taiga, are left alone.

**Settings.** `config/env` starts as a commented template listing every variable the translators read (`FEAT_CCX`, `CFDT_FOAM`, `LMPT_LMP`, output folders, workers, timeouts, MPI ranks). It also has room for `PATH` additions such as Homebrew's `/opt/homebrew/bin`.

Settings can be made at two levels:

- **Per machine:** `config/env` on the machine that runs the translator. Use it for backend paths and limits that belong to the hardware, such as `CFDT_PROCS=18` on an 18-core workstation. `llmt setup` asks for backend paths, and optionally for run limits (timeouts, MPI ranks, workers). `llmt env --edit` opens the file.
- **Per MCP server:** `VAR=value` arguments after the key, for example `bin/mcp cfdt CFDT_TIMEOUT=10800 CFDT_PROCS=18`. They apply after `config/env`, so they override it, and only for that entry. This lets two clients use the same workstation with different limits. In `llmt mcp setup`, answer yes to "Settings for this cfdt server", and the entry becomes:

  ```json
  "cfdt": {"command": "/usr/bin/ssh",
           "args": ["-T", "-o", "BatchMode=yes", "nicola@carbonio",
                    "~/Software/llm-translators/bin/mcp", "cfdt", "CFDT_TIMEOUT=10800", "CFDT_PROCS=18"]}
  ```

  Prefixing the command with `env CFDT_TIMEOUT=... bin/mcp cfdt` does not work if `config/env` also sets that variable, because `config/env` is loaded afterwards. Put the settings after the key instead.

**An interactive shell.** `llmt-venv` replaces the old `cad` alias, which activated `build123d-translator/.venv`. It activates llm-translators' venv and also loads `config/env`; `deactivate` leaves it. `llmt setup` offers to add it after installing, or run `llmt alias` at any time. Either way it writes this block to `~/.bashrc` (`~/.zshrc` for zsh; `--rc` for another file):

```bash
# >>> llm-translators >>>
# the llm-translators venv and its settings (config/env)
alias llmt-venv='source ~/Software/llm-translators/bin/activate'
# <<< llm-translators <<<
```

It also comments out old aliases that activate a translator's own venv, such as `alias cad='source ~/Software/build123d-translator/.venv/bin/activate'`, in `~/.bashrc`, `~/.bash_aliases`, `~/.bash_profile`, `~/.profile` and `~/.zshrc`, backing up each file first. Running it again replaces the block. `--name` picks another name, `--keep-old` leaves the old aliases, `--list` only shows them, and `--print` only prints the line.

## Submodules and versions

The umbrella pins one commit of each translator, so a given umbrella commit is a combination that was installed and tested together. The submodules track `main` (`branch = main` in `.gitmodules`), and their URLs are relative (`../fea-translator.git`): cloning the umbrella over ssh fetches them over ssh, and cloning over https fetches them over https.

- **Moving the pins** (after pushing new commits to a translator's repository): `llmt pin feat cfdt --commit --push`. It reads each translator's latest pushed `main`, moves the pin, lists the new commits (when the submodule is checked out here) and commits `Pin feat 02d34a8, cfdt c2f5668`. It works whether or not the submodules are checked out on this machine. Without `--commit`, the new pins are only staged. With no keys, every translator is checked. `--to COMMIT` pins one translator to a specific commit.
- **On each machine afterwards:** `llmt update`. It runs `git pull` on the umbrella, checks out the new pins of the translators installed there, and reinstalls them. Then restart the Claude app (or reconnect the servers): an MCP server loads the code when it starts. For a remote translator, this means on the machine that runs it, e.g. `llmt update` on carbonio, then reconnecting on the Mac.
- `llmt update --latest`: tries each translator's latest `main` on one machine without moving the pins. After testing (`llmt test`, `llmt bench`), record it with `llmt pin KEY --commit`.
- Submodules with uncommitted changes are left as they are. To work on a translator inside the umbrella, run `git checkout main` in its folder (submodules are checked out on a detached HEAD). It installs in editable mode, so your changes take effect at once.
- `llmt status` shows each submodule as `pinned`, `moved` (checked out at a commit other than the pin) or `absent`.

## Moving from the old layout

Before this, each machine had `build123d-translator/.venv`, shared by the others (the `cad` alias), and MCP entries pointing into it. You can move all at once or one machine at a time; until a machine is moved, its old install keeps serving through *existing* entries ([above](#translators-already-installed-elsewhere)).

1. Clone llm-translators and run `./llmt setup` on each machine, the workstation first so the client's remote check finds it. If the workstation has to wait, run setup on the client only and choose *existing* for the translators that stay there for now.
2. On the client, step 4 replaces the `b3dt`, `feat`, `cfdt` and `lmpt` entries in `claude_desktop_config.json` and keeps a backup. Restart the Claude app.
3. Move any backend variables you had put in the ssh commands (`env LMPT_LMP=... lmpt-mcp`) into `config/env` on the machine that runs the translator.
4. Replace the `cad` alias with `llmt-venv` (`llmt alias`, or `llmt setup` offers it).
5. Once `llmt status` shows everything as `ok` and no entry is *existing* any more, delete the old `build123d-translator/.venv` and the separate clones that were only used for installing. Keep the ones you develop in, or develop in the submodules.
