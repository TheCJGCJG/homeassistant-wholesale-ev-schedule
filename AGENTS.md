# Agent guidance

Entry point for AI coding agents (Claude Code, Codex, Copilot, etc.) working
in this repo. This file is a pointer, not a duplicate — if you learn
something that belongs in one of the docs below, edit that doc, not this one.

## Read these first

1. [README.md](README.md) — what the integration does and how a user sets it
   up. Read this to understand the product before changing behavior.
2. [DEVELOPING.md](DEVELOPING.md) — file structure, architecture notes (the
   *why* behind non-obvious design decisions), the full test inventory, and
   CI/release mechanics. Read this before touching code.

## Non-negotiables

- **No local Python environment.** There's no venv/`pip install` workflow —
  every test run happens inside Docker (`./run-tests.sh`, `make test`, or
  `docker-compose run --rm test`). See DEVELOPING.md → Testing.
- **`scheduler.py` stays pure.** No Home Assistant imports, no I/O. A change
  that needs HA state belongs in `coordinator.py` instead — see
  DEVELOPING.md → Architecture notes.
- **Docstrings/comments explain *why*, not *what*.** The existing style is
  dense with rationale — a hidden constraint, a past design decision, a
  pointer to the test that pins the behavior. Match that; don't add
  restating-the-code comments.
- **`strings.json` and `translations/en.json` are kept byte-identical.**
  Any config-flow schema change needs both updated together, or config-flow
  labels silently break.
- **Bumping `manifest.json`'s `version` triggers a GitHub release** on merge
  to `main` (see DEVELOPING.md → CI/releases). Only bump it as a deliberate
  release decision, not a drive-by.

## Workflow

1. If the change is tied to a GitHub issue, read it first:
   `gh issue view N --repo TheCJGCJG/homeassistant-wholesale-ev-schedule`.
2. Branch off `main` as `<type>/issue-N-slug` (e.g.
   `fix/issue-1-minute-aligned-desired-transitions`,
   `feat/issue-2-sensible-setup-defaults`) — the pattern every merged branch
   in this repo follows.
3. Make the change, keeping the `scheduler.py`/`coordinator.py` boundary
   intact and updating `strings.json` + `translations/en.json` together if
   `config_flow.py`'s schema changed.
4. Add or extend tests — check DEVELOPING.md → Testing for which existing
   file already covers the area you're touching before adding a new one.
5. Run `make test` and `make lint` — both are CI-enforced gates and must pass
   before opening a PR. `make security` (bandit + pip-audit) and
   `make format-check` (ruff format) are also available and worth running,
   but aren't CI gates yet — see DEVELOPING.md → Linting and security
   scanning.
6. Update README.md (user-facing behavior) and/or DEVELOPING.md
   (architecture/rationale) if the change affects either — docs drift is a
   real failure mode in a repo this size.
7. Open a PR against `main`. Use `Closes #N` in the description if it closes
   a tracked issue.

## Where things live

See DEVELOPING.md → Structure for the annotated file layout — it's kept
current there rather than repeated here.
