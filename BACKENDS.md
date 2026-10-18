# Backends

The translators call outside programs to mesh and solve. `llmt` never installs them: it reports what is missing (`llmt status`) and points here. Install them however suits the machine (distribution packages, PPAs, Homebrew, your own builds), then tell the translators where they are in `config/env` if they are not found on their own (`llmt env --edit`).

`config/env` matters for the MCP servers: the Claude app starts them without your login shell, and over ssh they run in a non-interactive shell that does not read `~/.bashrc`. Anything set only in `~/.bashrc` or `~/.zshrc` is invisible to them. `bin/run`, `bin/mcp` and `bin/activate` all load `config/env`.

| Backend | Used by | Without it | Setting |
|---|---|---|---|
| [Gmsh](#gmsh) | feat, cfdt | box parts and domains only | (Python module in the venv) |
| [CalculiX](#calculix) | feat | reference solver: static, modal, thermal on small meshes; no buckling | `FEAT_CCX` |
| [OpenFOAM](#openfoam) | cfdt | meshing, case writing, 2D reference solver | `CFDT_FOAM` |
| [LAMMPS](#lammps) | lmpt | inputs only, nothing runs | `LMPT_LMP`, `LMPT_POTENTIALS` |
| [MPI](#mpi) | cfdt, lmpt (`procs` > 1) | one rank per study | `LMPT_MPIRUN` |
| [SLURM](#slurm) (optional) | cfdt, lmpt, feat | each MCP server runs its studies itself, with no queue shared between chats | `CFDT_EXECUTOR`, `LMPT_EXECUTOR`, `FEAT_EXECUTOR` |

build123d-translator needs nothing beyond pip (build123d and its OpenCascade wheels). Those wheels exist for the common platforms and recent Python versions; if `pip` cannot find one for yours, use another Python for the venv (`llmt install --python /path/to/python3.x`, on a fresh venv).

After installing a backend, `llmt status` should show it as `ok`; `llmt status -v` shows what each translator found and where.

## Gmsh

The `mesh` extra of fea-translator and cfd-translator installs Gmsh from PyPI, so on macOS, Linux x86-64 and Windows there is nothing to do.

**Linux on ARM (aarch64, e.g. DGX Spark):** PyPI has no Gmsh wheels. Use the distribution's package (Gmsh 4.12 on Ubuntu 24.04) and make its Python module visible in the venv:

```bash
sudo apt install -y python3-gmsh
```

`llmt install` and `llmt setup` then offer to link `/usr/lib/python3/dist-packages/gmsh.py` into the venv (or pass `--link-system-gmsh`). This works only when the venv uses the system's Python version (3.12 on 24.04), because the module loads the system's `libgmsh`; `llmt setup` proposes `/usr/bin/python3` on ARM Linux for that reason. By hand:

```bash
ln -sf /usr/lib/python3/dist-packages/gmsh.py .venv/lib/python3.12/site-packages/gmsh.py
bin/run python -c "import gmsh; print(gmsh.__version__)"
```

## CalculiX

fea-translator runs `ccx`. It looks for `ccx` (or `ccx_2.*`) on the PATH, then in `/opt/homebrew/bin`, `/usr/local/bin` and `/usr/bin`. `FEAT_CCX=/path/to/ccx` in `config/env` overrides the search.

- **macOS:** a Homebrew tap packages current ccx for Apple Silicon (`costerwi/calculix`; see its README for the formula name), or build it from source. Then `ccx -v` should print the version.
- **Ubuntu/Debian:** `sudo apt install calculix-ccx`.

Check: `bin/run feat status` (shows `ccx`), then `llmt bench feat` (11 benchmarks with closed-form answers; buckling needs CalculiX).

## OpenFOAM

cfd-translator needs OpenFOAM from openfoam.org (the Foundation release), version 11 or later (`foamRun`, `solver incompressibleFluid`); 14 is the target. The ESI/openfoam.com releases are a different code line and are not supported.

On Ubuntu, from the Foundation's repository (for the release your Ubuntu version is supported by, see openfoam.org/download):

```bash
sudo sh -c "wget -O - https://dl.openfoam.org/gpg.key > /etc/apt/trusted.gpg.d/openfoam.asc"
sudo add-apt-repository http://dl.openfoam.org/ubuntu
sudo apt update && sudo apt -y install openfoam14
```

On ARM, check that the repository has an arm64 package first (`apt-cache policy openfoam14` after adding it). On macOS, OpenFOAM is not packaged natively: run cfd-translator on a Linux machine and reach it over ssh (`llmt mcp setup`, remote).

How it is found: `CFDT_FOAM=/opt/openfoam14/etc/bashrc` (or `CFDT_FOAM=PATH` when the environment already has OpenFOAM); without it, `foamRun` on the PATH, else the newest `/opt/openfoam*/etc/bashrc` or `~/OpenFOAM/OpenFOAM-*/etc/bashrc`. Do not source OpenFOAM's bashrc in `config/env`: the translator sources it itself for each run.

Check: `bin/run cfdt status` (shows `openfoam` with its version), then `llmt bench cfdt`.

## LAMMPS

lammps-translator runs a LAMMPS executable. It needs the packages your studies use: MANYBODY (EAM, Tersoff, SW), KSPACE (Ewald, PPPM), MOLECULE, RIGID (SHAKE), MEAM, ML-SNAP, ...; validation refuses a style the binary lacks, and `lmpt status` lists the installed packages.

- **Ubuntu/Debian:** the distribution's `lammps` package (MPI + OpenMP builds), or your own build.
- **PyPI wheel:** `llmt install --extra lmpt:lammps lmpt` (or say yes in `llmt setup`) installs LAMMPS into the venv. It needs `libmpi.so.12`: `bin/run pip install mpich` and that package's `lib` folder on `LD_LIBRARY_PATH` (set it in `config/env`).
- **macOS:** Homebrew has a `lammps` formula; or build from source.

How it is found: `LMPT_LMP` (a command, e.g. `/usr/bin/lmp` or `lmp_mpi`; `~` allowed), else `lmp`, `lmp_mpi`, `lmp_serial`, `lmp_stable`, `lmp_openmpi` or `lammps` on the PATH, else in `/usr/bin`, `/usr/local/bin`, `/opt/lammps/bin`, `~/.local/bin`.

Potential files are looked up in `LMPT_POTENTIALS` (`:`-separated), `LAMMPS_POTENTIALS`, `/usr/share/lammps/potentials`, `/usr/local/share/lammps/potentials`, the `lammps` Python package's `share/lammps/potentials`, and `../share/lammps/potentials` next to the executable.

Check: `bin/run lmpt status` (version, packages, potential folders), then `llmt bench lmpt`.

## MPI

Only for studies with `procs` > 1 (or `CFDT_PROCS`, `LMPT_PROCS`). OpenFOAM's packages bring their own MPI. For LAMMPS, `mpirun` must match the MPI the executable was built with; set `LMPT_MPIRUN` (e.g. `mpirun.openmpi`) if the default `mpirun` is a different one.

## SLURM

Optional, for machines that run long studies: cfdt, lmpt, and feat for large studies. With `P_EXECUTOR=slurm` in `config/env`, a translator submits each long study as a SLURM job instead of running it in the MCP server's own processes. Studies from every chat then share the machine through one queue, and a job keeps running when the conversation or its ssh connection ends. `llmt setup` and `llmt tune` offer it when `sbatch` is found; `llmt jobs` lists the jobs. The contract each translator implements is in [docs/EXECUTION.md](docs/EXECUTION.md).

A single-node install is enough: slurmctld and slurmd on the same machine. What matters for the translators:

- **Jobs share the machine by cores.** In `slurm.conf`, use `SelectType=select/cons_tres` with `SelectTypeParameters=CR_Core` (or `CR_Core_Memory`). With `select/linear`, each job takes the whole node and studies run one at a time; `llmt` warns about it.
- **The node's CPUs.** `CPUs`, `Sockets`, `CoresPerSocket` and `ThreadsPerCore` in the node line should match the hardware (`slurmd -C` prints them). `llmt tune` sizes the ranks per study from the physical cores, capped by what SLURM offers.
- **Time limits.** The partition's `MaxTime` must allow the translators' timeouts. A cfdt job asks for `CFDT_TIMEOUT` plus 10 minutes (3 h 10 min with `CFDT_TIMEOUT=10800`). `sinfo` shows the limit, and `llmt tune` prints it.
- **No accounting needed.** Without `sacct` (`AccountingStorageType=accounting_storage/none`, common on one machine), the translators read the end of each job's `job.log` to tell a time-out or a cancellation from a failure.
- **MPI inside a job.** OpenMPI's `mpirun` uses the job's allocation (`--ntasks`). OpenFOAM's and LAMMPS's MPI runs need nothing else.

Check:

```bash
sinfo                                   # the node is idle or mix, not down or drain
scontrol show config | grep -i select   # select/cons_tres
srun -n 2 hostname                      # a two-task job runs
./llmt status                           # SLURM: CPUs in use, translator jobs
```

- **Memory.** Check `scontrol show config | grep -E 'SelectTypeParameters|DefMem'`. With `CR_Core_Memory` and `DefMemPerNode = UNLIMITED` (no `DefMemPerCPU`), a job that does not ask for memory gets the whole node, and jobs run one at a time however many cores are free (mochi: every job `mem=60000M`). lmpt and feat ask for memory themselves (`LMPT_SLURM_MEM=auto`, `FEAT_SLURM_MEM=auto`: feat from the mesh size when it is known before meshing, else 8 GB); for cfdt, put `--mem=...` in `CFDT_SLURM_ARGS` (e.g. `--mem=16G`). Alternatively set `DefMemPerCPU` in `slurm.conf` (about RealMemory / CPUs, e.g. 1500 on a 64 GB, 40-CPU node), which changes the default for everyone's jobs.
- **Time limits.** Long limits block backfill: a short study waits behind a large job even when cores are free. `LMPT_SLURM_TIME=estimate` sizes lmpt's limits from each study's estimate.

## Where things run

The translators are cheap to install everywhere; the backends are not. A common layout:

- **The machine with the Claude app** (e.g. a Mac): b3dt, and feat with Gmsh and CalculiX for quick FEA. MCP servers local.
- **A Linux workstation**: cfdt with OpenFOAM, lmpt with LAMMPS, and feat with CalculiX for large models. MCP servers reached over ssh (`llmt mcp setup` on the client, choosing remote).

A translator can be installed in both places; the MCP entry on the client decides which one the Claude app uses.
