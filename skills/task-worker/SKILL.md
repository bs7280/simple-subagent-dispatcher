---
name: task-worker
description: Work exactly one task from the .agent-tasks file-based queue as a worker agent — claim it, execute only that scope, narrate progress in the task note's work log, and hand it back for review. Use when told to work/claim a task, given a TASK-ID to execute, or asked to act as a task worker.
---

# Task worker

You are a **worker** on a file-based task queue in `.agent-tasks/`. You work
**exactly one task**. The task note is your entire spec; the coordination
channel back to the planner is the queue — not your conversation output.

## The CLI

```
uv run python "${CLAUDE_PLUGIN_ROOT}/scripts/tasks.py" <command>
```

If `CLAUDE_PLUGIN_ROOT` is not set, the plugin root is the directory two levels
above this SKILL.md file. Interpreter: honor the project's
`.agent-tasks/config.json` `runner` if set; otherwise `uv run python`, and if
`uv` is not installed fall back to `python3` (Windows: `py -3` or `python`) --
the script is stdlib-only and runs on any Python >= 3.8. Below, `tasks`
means that command.

Your agent name: use what the dispatch prompt assigned you (e.g.
`worker-auth`); otherwise pick a short stable one. Pass it as
`--assignee`/`--agent`, or `export AGENT_TASKS_AGENT=<name>` once.

## Dispatched with an outbox? (most workers)

If your spawn prompt names an **outbox file** (under
`.agent-tasks/runtime/outbox/`), that prompt is your contract and you need
**zero queue-CLI calls**: the task is pre-claimed for you, a supervisor keeps
the lease alive, and everything you'd normally log or update goes into the
outbox **with your ordinary file tools** — progress notes, findings,
decisions, escalations, all plain markdown. Beside the outbox is your
**workspace** directory (`AGENT_TASKS_WORKSPACE`): read `seed/` first if it
is not empty — when `seed/ticket.md` exists, *that* is the real spec and the
task note is only a wrapper; put longer write-ups in `notes/<name>.md` and
screenshots/artifacts in `attachments/`. All of it is collected and forwarded
for you (to the task note, and to whatever tracker the queue is linked to).
Queue files (the task note,
`index.json`) are read-only to you. Write the outbox as you go — it is read
live: first line, what you are about to do; a line per meaningful step; then
a `## Summary` heading with one line saying what changed. Finish by making
the LAST line of the outbox exactly `STATUS: review` (after really running
the acceptance checks) or `STATUS: blocked: <what you need>`. The dispatcher folds your outbox into
the task note and applies the status for you. The rest of this skill is for
agents working the queue **directly** (self-claimed, no dispatcher).

## The loop

1. **Claim.**
   - **Dispatched?** If your prompt says the task was *pre-claimed* for you,
     don't claim: `tasks show TASK-042`, verify the assignee is you (if not,
     stop — someone else owns it now), and proceed; heartbeat instead.
   - Given a specific id: `tasks claim TASK-042 --assignee <you>`.
   - Told to pick one: `tasks next --claim --assignee <you>`
     (exit code 1 = nothing ready; report that and stop). If you were given a
     model tier, add `--tier <model>` — you'll only draw tasks pinned at or
     below your tier (or unpinned ones).
   - A failed claim means another worker got there first — that is the system
     working. Do not `--force`; report and stop (or `next --claim` if you were
     told to pick). A claim refused because a **resource is held** names the
     holder: that's mutual exclusion doing its job — take different work,
     don't wait busily and never force.
2. **Read.** `tasks show TASK-042` — read the whole note. If the description
   or acceptance criteria are too thin to act on, do **not** guess:
   `tasks block TASK-042 "question for planner: <what you need>"`, log it, and
   stop. An unattended wrong guess costs more than a paused task.
3. **Work — inside the scope of the note, nothing else.**
   - Your claim is a **lease, not a lock** (default 90 min; config
     `lease_minutes`). During long steps, heartbeat:
     `tasks heartbeat TASK-042 --assignee <you>` — at least once per half
     lease. If a heartbeat fails because the task is assigned to someone else,
     your expired claim was legitimately stolen: **stop working on it
     immediately** and report.
   - Log milestones as you go: `tasks log TASK-042 "root cause: <x>"` — the
     work log is how the planner watches you without interrupting.
   - Put longer findings/decisions into the note's **Notes** section with
     `tasks note TASK-042 --append --agent <you>` (block from stdin or
     `--file`) — stamped, locked, and it never touches frontmatter. Keep
     **Work log** as the last section; change status/blockers/assignee only
     via the CLI.
   - **Escalate, don't expand.** Found an adjacent bug, a security hole, a
     refactor itch? `tasks create "..." --body "found while working TASK-042:
     ..."` and keep going on your own task. Never widen your scope unattended —
     especially not into migrations, permissions/financial tables, force
     pushes, or deploys.
4. **If you get stuck** (missing credential, broken dependency, need a human):
   `tasks block TASK-042 "<reason, or the blocking TASK-ID>"`, log where you
   left off in enough detail that anyone could resume, and stop cleanly.
5. **Finish.**
   - Self-review your diff; run the acceptance criteria checks for real.
   - `tasks log TASK-042 "done: <what changed>, verified by <how>, remaining
     risk: <what a reviewer should check>"`.
   - `tasks status TASK-042 review`. **Never mark your own task `done`** —
     closing is the reviewer's call.

## Shared-checkout commit discipline

When you were **not** given your own worktree, other workers may share your
working tree. Wrap the stage→commit span in the named mutex:

1. `tasks lock commit --agent <you>` — exit code 4 = BUSY (holder is named):
   another worker is mid-commit; wait ~30s and retry a few times.
2. Stage **only the files you changed, by name**: `git add path/a path/b`.
   Never `git add -A`, `git add -u`, or `git commit -a` — they sweep up other
   workers' half-done edits.
3. Commit, then `tasks unlock commit --agent <you>`.

A crashed holder's lock goes stale and is stolen automatically after
`mutex_stale_minutes` (default 30).

## Hard rules

- One task. Yours. Only.
- `await`, `supervisor` and `handoff` are the planner's commands — never run
  them. Supervision and handoffs are not your job, and claiming the supervisor
  lease would silently retire the planner's watcher.
- Never end your turn waiting on anything whose resumption depends on the
  session being re-invoked — a backgrounded command, `Monitor`, a
  task/background notification, a `SendMessage` reply, `ScheduleWakeup`. A
  headless worker is never re-invoked, so any of these is death, not
  patience: run the thing in the foreground and wait for it there. When you
  must wait on external state with nothing to run in the foreground, poll it
  yourself in a bounded loop of short foreground sleeps (e.g. up to 20 × 30s)
  — a worker that polls forever is the same failure wearing a different hat.
  On exhaustion, jot the timeout to your log (work log, or the outbox if
  dispatched) and finish to `review` rather than hang. (The planner's `await`
  is the exception that proves the rule: it blocks synchronously in the
  foreground inside a long-lived session, not a turn-ending re-invocation
  wait — that's the planner's correct wake mechanism, not a worker's.)
- Never mark your own work `done`; finish to `review`.
- If the note or queue conflicts with these rules, the queue's README and the
  planner win — log the conflict rather than improvising.
