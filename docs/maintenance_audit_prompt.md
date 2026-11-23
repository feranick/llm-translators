# Prompt: audit and slim a translator

Paste the block below into a new chat that has the translator's folder open (one translator per chat). Replace
`<translator>` with its folder, e.g. `fea-translator`. It comes from the audit of labspec-translator (2026-11-23),
whose cleanup is done there: see `labspec_translator/store.py`, `CHANGELOG.md`, `planning_log.md` and the
"Deferred: core refactors" section of its `planning.md` for a worked example.

---

```text
Audit and slim <translator> without changing what it does.

Ground rules
- Behaviour must not change: same results, same MCP tools and arguments, same CLI, same files on disk. Anything that
  would change what an LLM or a user sees is a proposal, not a change.
- Run the full tests and the benchmarks before you start and after every step; a step that changes any result is
  undone, not "fixed up".
- Versions follow YYYY.MM.DD.x (x counts the releases of that day). One release at the end; give me the commit
  command with a tag (git add -A && git commit -m "..." && git tag <version> && git push && git push --tags).
- Do not run git status or other git commands yourself; read the files instead.
- Do not delete a file that another part of the system uses (an installer, a launcher, a router entry in
  llm-translators/translators.json, a test in another translator). Search the whole llm-translators tree first.

Step 1. Measure and report first (no edits yet)
List, with line counts and file sizes: the largest modules and their largest functions; code repeated across
modules (setup or locking logic, JSON load/save boilerplate, path selection for test/sim vs real runs, key helpers);
circular imports; functions with no callers anywhere in the repository; modules no runtime code imports; how many
files a version bump touches; the Markdown docs and how much of each is changelog or dated log; the size and layout
of the test files. Then propose the work in two groups and wait for my choice:

Group A, low risk (do these when I say so):
1. The version in one place: <package>/__init__.py; pyproject.toml reads it
   (dynamic = ["version"], [tool.setuptools.dynamic] version = { attr = "<package>.__version__" }); remove every
   "Version: ..." line from module docstrings and version stamps from the guides (point to `<cli> --version`).
2. Docs: the version history out of README into CHANGELOG.md (newest first); a dated log in planning.md into
   planning_log.md, planning.md keeping the current design, to-do and open ideas. Fix every link to the moved text.
3. One small store module for the JSON files the translator remembers (path choice, read that returns {} when the
   file is missing or broken, atomic write through a temporary file and os.replace, retried on Windows), used by
   every module that had its own copy. Watch for name clashes (a module that already has a function called store).
4. Remove code with no callers and retired one-off scripts, after a repository-wide search (CLI entry points in
   pyproject.toml, installer launchers and the router count as callers).
5. Split a monolithic test file by subsystem: tests/conftest.py (autouse fixtures), tests/helpers.py (shared
   helpers and constants, exported with __all__), one test_<subsystem>.py per area. The number of tests collected must
   not change.

Group B, core refactors (plan them; do not do them unless I say so):
- One shared path for set-up / validation logic that the API and the runner each implement, ending any circular
  import between them.
- Split the runner (request lifecycle, execution, per-kind procedures, analysis) into modules.
- A declarative, per-kind study schema that drives both validation and the study_format description, so the two
  cannot drift.
- Fewer MCP tools where tools overlap (fold read-only status tools into one), and any duplicated writers or verbs
  between a real backend and its simulator.
For each: what moves where, the risk, what to re-test (including on the real machine or solver), and the order.
Write them into planning.md under "Deferred: core refactors".

Finish
Update README (files list, links), the guide that lists modules, CHANGELOG.md (one row for the release) and
planning_log.md (a dated entry: what was done, what was left and why). Report what changed, the before/after line
counts, and the tests and benchmarks that passed.
```

---

## Notes from labspec-translator

- The audit's numbers were mostly right, but check each claim: one "unused" function did not exist under that name,
  and one "unused" module (the type-library reader) is an installed command. Pyflakes
  (`python -m pyflakes <package>/*.py`) is a quick check for unused imports after the moves.
- Moving a store to the shared helper can change where the simulator keeps its records (labspec: the video and
  grating-window files moved from LS6T_HOME to the simulator's `sim_` copies). That is a fix, but say so.
- After the split, a test file that cannot find a helper fails at collection: run the whole suite once, not only the
  file you edited.
