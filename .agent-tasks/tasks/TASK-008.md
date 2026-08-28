---
id: TASK-008
title: Don't pre-create the outbox file
status: open
created: 2026-08-28T21:12:12Z
---

# TASK-008 -- Don't pre-create the outbox file

## Description

Live run 2026-08-28 (v0.5.0 validation): the worker's first Write to its pre-created empty outbox hit the harness's read-before-write guard ('File has not been read yet') and burned two turns recovering via Read+Edit. The dispatcher pre-creates the outbox at spawn; if it merely ensured the DIRECTORY, the worker's first Write would create the file fresh with no guard. fold_outbox already tolerates a missing file (returns None) — semantics stay: no file/no sentinel = died-mid-task. Check the empty-file vs missing-file fold difference (empty archives + logs, missing does nothing) and keep resume's recreate-or-not consistent.

## Acceptance criteria

- first worker Write to the outbox is guard-free
- fold/no-sentinel semantics unchanged
- covered in tests/dispatch_smoke.py

## Notes

_(worker scratch space -- findings, decisions, open questions)_

## Work log

- 2026-08-28T21:12:12Z [planner] created
