---
id: TASK-006
title: dispatch prompt --self-claim variant
status: open
created: 2026-08-28T19:02:47Z
---

# TASK-006 — dispatch prompt --self-claim variant

## Description

Since P1-3, 'dispatch prompt TASK-X' emits the pre-claimed form (verify assignment, don't claim), which is wrong to paste into a session that hasn't claimed the task. Add --self-claim to emit the claim-first form for humans/external agents. Found while working the dispatcher-hardening review batch (2026-08-28).

## Acceptance criteria

- dispatch prompt --self-claim emits a claim-first step
- default form unchanged
- covered in tests/dispatch_smoke.py

## Notes

_(worker scratch space — findings, decisions, open questions)_

## Work log

- 2026-08-28T19:02:47Z [planner] created
- 2026-08-28T20:19:18Z [planner] since the outbox redesign, 'dispatch prompt' also carries a placeholder <worker-id> outbox path — the --self-claim variant should emit the CLI-based contract (claim + log + status review) for capable direct agents instead
