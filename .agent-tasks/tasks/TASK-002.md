---
id: TASK-002
title: Make work-log appending robust to sections after Work log
status: open
created: 2026-08-28T17:43:16Z
---

# TASK-002 — Make work-log appending robust to sections after Work log

## Description

tasks.py append_log() appends to end-of-file, so the Work log must stay the note's last section — a documented but fragile convention. Make append_log insert entries at the end of the '## Work log' section wherever it sits (append the section if missing), so an agent adding a section below it doesn't silently misfile log entries. Keep it stdlib-only; update the note template docs if the 'keep it last' rule can be dropped.

## Acceptance criteria

- log entries land under ## Work log even when another section follows it
- covered by a case in tests/smoke.sh

## Notes

_(worker scratch space — findings, decisions, open questions)_

## Work log

- 2026-08-28T17:43:16Z [planner] created
