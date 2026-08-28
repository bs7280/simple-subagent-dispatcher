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
uv run python "${CLAUDE_PLUGIN_ROOT}/scripts/tasks.py" <command>
```

If `CLAUDE_PLUGIN_ROOT` is not set, the plugin root is the directory two levels
above this SKILL.md file. `uv run python` is the canonical interpreter
invocation; if the project's `.agent-tasks/config.json` sets a different
`runner`, use that instead. Below, `tasks` means that command. Run `tasks --help`
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
- **Pin the model when it matters.** `--model opus` on a hard task,
  `--model haiku` on a mechanical one; leave it unset when any model will do.
  The dispatcher uses the task's model automatically (task > config > CLI
  default), and tier-limited workers (`next --tier`) respect it: a haiku-tier
  worker never picks up an opus-pinned task.
- **Sequence with blockers.** `--blocked-by TASK-001` (or later
  `tasks block TASK-005 TASK-001`). A blocker naming a task id auto-resolves
  when that task is done/cancelled; free-text blockers (e.g. "waiting on API
  key from Ben") stay until `tasks unblock` removes them.
- **Declare exclusive resources.** Two tasks that both touch DB migrations,
  reset a shared dev database, or drive the same browser must not run
  concurrently: give them the same `--resources` tag (e.g.
  `--resources db-migrations` or `browser`). The queue refuses to let two
  live claims hold the same tag, and the refusal names the holder — mutual
  exclusion without imposing an order. Use blockers when order matters,
  resources when only exclusivity does. E2E-suite tasks should still run solo
  and last.

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
- **The dispatcher (recommended for unattended workers)** — durable,
  observable, resumable. `dispatch` = `python3
  "${CLAUDE_PLUGIN_ROOT}/scripts/dispatch.py"`:
  - `dispatch start TASK-042` — atomically **pre-claims** the task (loser of
    a double-dispatch exits before spawning a session), then spawns a headless
    `claude -p` worker with its own session id and a ready-made worker prompt
    (no need to write one). Runs
    **in the repo checkout by default**; add `--worktree` for an isolated git
    worktree per worker. Which is right is the project's call — record the
    default in `.agent-tasks/config.json` (`worktree`, `model`,
    `permission_mode`, `allowed_tools` — the commands your workers may run
    unattended, e.g. `"Bash(pnpm test:*)"` — `bootstrap`, …). The queue CLI is
    always pre-approved for workers; everything outside the allowlist is
    denied-not-prompted, so thin allowlists show up as denied actions in the
    transcript, not hangs.
  - `dispatch list` — all workers, with `[NEEDS-RESUME]` on any that exited
    while its task was still in_progress.
  - `dispatch watch <worker> --follow` — tail the worker's real transcript.
  - `dispatch wait <worker>` — block until it exits (exit 3 = died mid-task).
  - `dispatch resume <worker>` — continue a dead worker's session; context and
    uncommitted edits survive on disk, nothing is lost.
  - `dispatch stop <worker>` — SIGTERM; the session survives for `resume`.
  Only run workers **in parallel in-place** if their tasks touch disjoint
  files; otherwise use `--worktree` or serialize with blockers. Dispatched
  workers get `AGENT_TASKS_DIR` pointing at the shared queue, so worktree
  copies of `.agent-tasks/` are never written to.
- **Humans / interactive sessions** — just hand over the task id (the
  task-worker skill self-claims). `dispatch prompt TASK-042` output assumes a
  pre-claimed task, so only paste it after claiming for that agent name.

Don't pre-claim on a worker's behalf — workers claim for themselves, and
claims are atomic (`tasks claim` / `tasks next --claim`), so an accidental
double-dispatch loses cleanly: the second claimant errors out instead of
duplicating work.

## 4. Monitor

- `tasks board` — one-screen overview (statuses, assignees, blockers).
- Dispatched workers report through per-worker **outboxes** that the
  dispatcher folds into the task note when it observes the exit (`wait`,
  `watch`, or `list`) — ending with `STATUS: review` or `STATUS: blocked:
  <reason>` (blocked reopens the task with the reason as a blocker, i.e. a
  question addressed to you). `wait`/`watch` also auto-heartbeat live
  workers, so a running worker's lease never decays under supervision.
- `tasks list --status in_progress` / `tasks show TASK-042` — the note's Work
  log is the worker's live narration; read it before assuming a worker is stuck.
- `tasks doctor` — periodic integrity check: index/note drift, orphan
  claims (crashed workers whose leases expired), stray notes. Exit 1 means
  findings; `--fix` repairs drifted frontmatter.
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
