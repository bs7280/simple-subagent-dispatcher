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
above this SKILL.md file. `uv run python` is the canonical interpreter
invocation; if the project's `.agent-tasks/config.json` sets a different
`runner`, use that instead. Below, `tasks` means that command.

Your agent name: use what the dispatch prompt assigned you (e.g.
`worker-auth`); otherwise pick a short stable one. Pass it as
`--assignee`/`--agent`, or `export AGENT_TASKS_AGENT=<name>` once.

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
   - Put longer findings/decisions in the note's **Notes** section by editing
     the file directly (`tasks note TASK-042` prints the path). The note body
     is yours to edit; keep **Work log** as the last section, and change
     status/blockers/assignee only via the CLI.
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
- Never launch a long command in the background and end your turn "waiting"
  for it — headless sessions are never re-invoked when it finishes, so that is
  death, not patience. Run long commands in the foreground and wait for them.
- Never mark your own work `done`; finish to `review`.
- If the note or queue conflicts with these rules, the queue's README and the
  planner win — log the conflict rather than improvising.
