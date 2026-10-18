# llm-translators

One place to install and run the translators that connect LLMs to engineering and science tools. Each translator stays in its own repository and is included here as a git submodule. This repository supplies the parts they share: one Python venv, an installer that lets you choose which translators each machine gets, per-machine settings, and the MCP server entries for the Claude app, which can run locally or over ssh.

| Key | Translator | What | Backends |
|---|---|---|---|
| `b3dt` | [build123d-translator](https://github.com/feranick/build123d-translator) | CAD parts from goals: build123d solids, STEP files, scripts | none (pip only) |
| `feat` | [fea-translator](https://github.com/feranick/fea-translator) | FEA: static, modal, buckling, steady thermal | Gmsh, CalculiX |
| `cfdt` | [cfd-translator](https://github.com/feranick/cfd-translator) | CFD: steady incompressible flow | Gmsh, OpenFOAM |
| `lmpt` | [lammps-translator](https://github.com/feranick/lammps-translator) | Atomistic simulations | LAMMPS |

The translators work together: fea and cfd take b3dt goals as geometry, cfd passes wall loads to fea, and lammps produces fea materials. That only works when they are installed in the same venv, which is why there is a single venv here.

Version: 2026.10.17.1.

## Quick start

```bash
cd ~/Software        # anywhere
git clone https://github.com/feranick/llm-translators.git
cd llm-translators
./llmt setup
```

`llmt setup` walks through four steps:

1. **What to install on this machine.** It lists the translators with their backends and how heavy they are. Translators that others use are offered too (b3dt with feat, for example).
2. **Python environment.** It creates `.venv`, checks out only the chosen submodules, and runs `pip install -e` on each with its extras (`mcp`, `mesh`, optionally `test`).
3. **Backends.** It reports whether Gmsh, CalculiX, OpenFOAM and LAMMPS are found, using each translator's own `status` command. It does not install them: see [BACKENDS.md](BACKENDS.md). You can enter their paths in `config/env`.
4. **MCP servers.** For the Claude app on this machine, choose for each translator whether it runs locally, on another machine over ssh, or not at all. It writes the entries into `claude_desktop_config.json`, keeping a backup.

`llmt` needs only git and Python 3.7+ (standard library). The venv needs Python 3.10+. On macOS, `/usr/bin/python3` is 3.9: `llmt` runs with it but builds the venv with the newest `python3.1x` it can find (or the one given with `--python`).

## A typical layout

- **The Mac with the Claude app.** Run `./llmt setup`, install `b3dt` and `feat`, and choose MCP *local* for those two and *remote* for `cfdt` and `lmpt` (`nicola@carbonio`, `~/Software/llm-translators`).
- **The Linux workstation (carbonio).** Run `./llmt setup`, install `cfdt` and `lmpt` (plus `feat` and `b3dt`, which they use), and answer *no* to "Will a Claude app on this machine use the translators?".

The client's remote entry runs `ssh -T -o BatchMode=yes HOST <root>/bin/mcp KEY`, so the remote host needs key-based ssh and the umbrella at that path. During setup `llmt` offers to check the remote host over ssh: it runs `llmt status --json` there.

## Commands

```
llmt setup                      guided setup (also what plain ./llmt does the first time)
llmt list                       the translators, their backends, what is installed here
llmt install KEY... [--python P] [--extra lmpt:lammps] [--no-test] [--no-uses] [--link-system-gmsh]
llmt remove KEY...              pip uninstall (sources stay checked out)
llmt status [KEY...] [-v] [--json]   versions, submodule commits, backends found
llmt update [KEY...] [--latest] [--no-pull]
llmt test [KEY...]              pytest in each translator
llmt bench KEY [NAMES...]       a translator's benchmarks
llmt env [--edit]               per-machine settings (config/env)
llmt mcp setup                  local / remote / none per translator
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

**An interactive shell.** Replace the old `cad` alias (which activated `build123d-translator/.venv`) with:

```bash
alias llmt-env='source ~/Software/llm-translators/bin/activate'
```

## Submodules and versions

The umbrella pins one commit of each translator, so a given umbrella commit is a combination that was installed and tested together. The submodules track `main` (`branch = main` in `.gitmodules`), and their URLs are relative (`../fea-translator.git`): cloning the umbrella over ssh fetches them over ssh, and cloning over https fetches them over https.

- `llmt update`: runs `git pull` on the umbrella, checks out the pinned commits, then reinstalls.
- `llmt update --latest`: checks out the latest `main` of each translator. After testing (`llmt test`, `llmt bench`), record the new combination:

  ```bash
  git add build123d-translator fea-translator ... && git commit -m "Update pins"
  ```
- Submodules with uncommitted changes are left as they are. To work on a translator inside the umbrella, run `git checkout main` in its folder (submodules are checked out on a detached HEAD). It installs in editable mode, so your changes take effect at once.
- `llmt status` shows each submodule as `pinned`, `moved` (checked out at a commit other than the pin) or `absent`.

## Moving from the old layout

Before this, each machine had `build123d-translator/.venv`, shared by the others (the `cad` alias), and MCP entries pointing into it. To move:

1. Clone llm-translators and run `./llmt setup` on each machine (the workstation first, so the client's remote check finds it).
2. On the client, step 4 replaces the `b3dt`, `feat`, `cfdt` and `lmpt` entries in `claude_desktop_config.json` and keeps a backup. Restart the Claude app.
3. Move any backend variables you had put in the ssh commands (`env LMPT_LMP=... lmpt-mcp`) into `config/env`.
4. Once `llmt status` is all `ok`, delete the old `build123d-translator/.venv` and the separate translator clones that only served for installing. Keep the ones you develop in, or develop in the submodules.
