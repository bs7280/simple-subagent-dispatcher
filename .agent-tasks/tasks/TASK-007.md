---
id: TASK-007
title: Auto-heartbeat coverage for list-only supervisors
status: open
created: 2026-08-28T20:19:18Z
---

# TASK-007 -- Auto-heartbeat coverage for list-only supervisors

## Description

wait/watch auto-heartbeat live workers, but a supervisor that only polls 'dispatch list' leaves leases decaying — a live worker could be lease-stolen between list calls. Options: heartbeat running workers observed by list (side-effectful read), or a 'dispatch supervise' loop command. Found while working the Windows-dogfood review batch (2026-08-28).

## Acceptance criteria

- a supervisor polling only list keeps live workers' leases fresh (or docs explicitly demand wait/watch)

## Notes

_(worker scratch space -- findings, decisions, open questions)_

## Work log

- 2026-08-28T20:19:18Z [planner] created
