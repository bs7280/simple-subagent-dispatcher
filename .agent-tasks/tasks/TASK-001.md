---
id: TASK-001
title: Add CI: run both smoke suites on push
status: open
created: 2026-08-28T17:43:16Z
---

# TASK-001 — Add CI: run both smoke suites on push

## Description

Add a GitHub Actions workflow (.github/workflows/ci.yml) that runs tests/smoke.sh and tests/dispatch_smoke.sh on push and PR, on ubuntu-latest and macos-latest. Both suites are self-contained (stdlib Python + git + a stub claude binary) — no real claude CLI or auth needed. Keep it to one small workflow file.

## Acceptance criteria

- both suites run green in CI on both OSes
- no claude CLI installation required

## Notes

_(worker scratch space — findings, decisions, open questions)_

## Work log

- 2026-08-28T17:43:16Z [planner] created
- 2026-08-28T19:02:46Z [planner] test suites are now Python (tests/smoke.py, dispatch_smoke.py, procs_smoke.py) run via 'uv run python' — CI workflow should install uv (astral-sh/setup-uv) and run all three; windows-latest is now a viable third OS target thanks to the procs shim
