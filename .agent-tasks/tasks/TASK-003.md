---
id: TASK-003
title: Handle stale worker registry entries after reboot
status: open
created: 2026-08-28T17:43:16Z
---

# TASK-003 — Handle stale worker registry entries after reboot

## Description

dispatch.py worker_state() calls pid_alive(); after a reboot a recycled pid can make a long-dead worker show 'running' and suppress [NEEDS-RESUME]. Record enough to detect staleness (e.g. process start-time via ps, or a boot-id) and treat mismatches as exited. Keep it stdlib-only and darwin+linux portable.

## Acceptance criteria

- a registry entry whose pid was recycled reports exited, not running
- NEEDS-RESUME fires for it when its task is still in_progress

## Notes

_(worker scratch space — findings, decisions, open questions)_

## Work log

- 2026-08-28T17:43:16Z [planner] created
