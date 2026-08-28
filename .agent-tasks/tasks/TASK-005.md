---
id: TASK-005
title: MCP wrapper for the queue CLI
status: open
created: 2026-08-28T17:43:17Z
---

# TASK-005 — MCP wrapper for the queue CLI

## Description

Graduate tasks.py to the usual dual-mode CLI+MCP pattern once the CLI surface has stabilized through real use. Not before: MCP schema changes need a reconnect, CLI edits don't. Unblock this when a few real projects have run the queue without the CLI surface changing.

## Acceptance criteria

- MCP server exposes the same operations as the CLI against the same .agent-tasks state
- CLI remains the source of truth / still works standalone

## Notes

_(worker scratch space — findings, decisions, open questions)_

## Work log

- 2026-08-28T17:43:17Z [planner] created
