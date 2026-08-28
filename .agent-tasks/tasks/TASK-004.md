---
id: TASK-004
title: Dedicated verification workers (VERIFY- tasks)
status: open
created: 2026-08-28T17:43:17Z
---

# TASK-004 — Dedicated verification workers (VERIFY- tasks)

## Description

From the POC harness queue item 7: planner accumulates untested gaps as VERIFY- tasks; dedicated verification workers (e.g. Playwright-driving) drain that queue, serializing naturally on the browser slot. Design first: probably an id prefix + tag convention plus a task-verifier skill, maybe a 'tasks create --prefix VERIFY' option. Bigger design — sketch in this note's Notes section before building.

## Acceptance criteria

- design sketch agreed in this note
- a worker can drain VERIFY- tasks one at a time via the existing dispatcher

## Notes

_(worker scratch space — findings, decisions, open questions)_

## Work log

- 2026-08-28T17:43:17Z [planner] created
