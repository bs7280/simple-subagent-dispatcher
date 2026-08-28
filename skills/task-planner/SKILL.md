---
name: task-planner
description: Plan and coordinate work for multiple agents through the .agent-tasks file-based queue — break a feature/project into self-contained tasks, sequence them with blockers, dispatch workers, monitor progress, and review finished work. Use when the user wants to break work into tasks for agents, dispatch parallel workers, act as a planner/coordinator, or review tasks sitting in the queue.
---

# Task planner

You are the **planner** for a file-based task queue in `.agent-tasks/`. Workers
(subagents, headless `claude -p` sessions, or humans) coordinate with you
entirely through that folder — not through conversation context. Your job:
write tasks good enough that a worker with **zero conversation context** can
execute them, then dispatch, monitor, and review.

## The CLI

All queue mutations go through the CLI (it serializes concurrent writers):

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tasks.py" <command>
```

If `CLAUDE_PLUGIN_ROOT` is not set, the plugin root is the directory two levels
above this SKILL.md file. Below, `tasks` means that command. Run `tasks --help`
/ `tasks <command> --help` for full flags; every read command takes `--json`.

Identify yourself as `planner`: pass `--agent planner` on mutations, or
`export AGENT_TASKS_AGENT=planner` once.

## 1. Initialize (once per repo)

`tasks init` — creates `.agent-tasks/` in the current directory. Decide with
the user whether to commit the folder or gitignore it (committing makes task
state travel with the repo; ignoring avoids merge conflicts across branches).

## 2. Plan: write self-contained tasks

```
tasks create "Fix login redirect loop" \
  --body "Users hitting /app while logged out bounce forever. Start in middleware.ts; repro: ..." \
  --criteria "- redirect loop gone (verify with curl -I)\n- existing auth tests pass" \
  --priority high --tags auth
```

Rules for a good task note:
- **Self-contained.** Everything the worker needs is in the note: context, file
  paths, repro steps, constraints, acceptance criteria. Never rely on "as we
  discussed". After `create`, edit the note file directly (path is printed) to
  add detail — the note body is free-form markdown; only keep **Work log** as
  the last section.
- **One reviewable unit.** If you can't state acceptance criteria, split it.
- **Sequence with blockers.** `--blocked-by TASK-001` (or later
  `tasks block TASK-005 TASK-001`). A blocker naming a task id auto-resolves
  when that task is done/cancelled; free-text blockers (e.g. "waiting on API
  key from Ben") stay until `tasks unblock` removes them.
- **Serialize exclusive resources with blockers.** Two tasks that both touch DB
  migrations, reset a shared dev database, or drive the same browser must not
  run concurrently — chain them (`--blocked-by`) rather than hoping. E2E-suite
  tasks should run solo and last.

## 3. Dispatch workers

Give every worker the same shape of prompt — one explicit task, never "pick
whatever":

> You are a task worker. Load the `agent-tasks:task-worker` skill and follow
> it exactly. Repo: `<path>`. Your assigned task: **TASK-042**. Your agent
> name: `worker-auth`. Work only that task.

Options, by weight:
- **Subagents (Agent tool)** — cheapest; fine for small independent tasks. One
  subagent per task, all dispatched in parallel *only if* their tasks don't
  share exclusive resources (see above).
- **Independent headless sessions** — durable and observable; survives this
  session dying. From the repo root (or a dedicated git worktree per worker
  for parallel file isolation):
  `claude -p --permission-mode auto --session-id "$(uuidgen)" "<worker prompt>"`.
  Record the session id in the task's work log (`tasks log TASK-042 "worker
  session <id>"`) so a dead worker can be resumed later with
  `claude -p --resume <id> --permission-mode auto "continue your task"`.
- **Humans / interactive sessions** — just hand over the task id.

Don't pre-claim on a worker's behalf — workers claim for themselves, and
claims are atomic (`tasks claim` / `tasks next --claim`), so an accidental
double-dispatch loses cleanly: the second claimant errors out instead of
duplicating work.

## 4. Monitor

- `tasks board` — one-screen overview (statuses, assignees, blockers).
- `tasks list --status in_progress` / `tasks show TASK-042` — the note's Work
  log is the worker's live narration; read it before assuming a worker is stuck.
- Workers that hit a wall add a blocker and stop, so check
  `tasks list` for `[blocked ← ...]` markers regularly — free-text blockers
  are usually questions addressed to *you*.

## 5. Review

Workers finish to `review`, never `done` — closing is your call (or the
user's):
1. `tasks show TASK-042` — read the completion summary in the work log.
2. Verify against the acceptance criteria (run the checks; don't take the
   worker's word for it).
3. Pass → `tasks done TASK-042 --summary "verified: <how>"`.
   Fail → `tasks log TASK-042 "review feedback: <what's wrong>"` then
   `tasks status TASK-042 in_progress` (same worker continues) or
   `tasks status TASK-042 open` + `tasks assign` (someone else takes it).

Workers also `create` new tasks for out-of-scope discoveries instead of
expanding their own — triage those (priority, blockers) as they appear.
