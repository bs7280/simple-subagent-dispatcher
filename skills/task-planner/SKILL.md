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
above this SKILL.md file. Interpreter: honor the project's
`.agent-tasks/config.json` `runner` if set; otherwise `uv run python`, and if
`uv` is not installed fall back to `python3` (Windows: `py -3` or `python`) --
the script is stdlib-only and runs on any Python >= 3.8. Below, `tasks`
means that command. Run `tasks --help` / `tasks <command> --help` for full
flags; every read command takes `--json`.

Identify yourself as `planner`: pass `--agent planner` on mutations, or
`export AGENT_TASKS_AGENT=planner` once.

## 0. Boot: inherit, don't rediscover

Before anything else, in this order:

```
tasks handoff --show                        # the last planner's document
tasks since --cursor <cursor it names> --actionable   # what happened after it
tasks board                                 # current state, one screen
```

A planner session is expensive to keep alive and cheap to replace *if* the
queue carries what it knew. The handoff document is that carrier: read it and
you resume the last session's judgment without inheriting its transcript. "No
handoff recorded" just means nobody wrote one — start from `board`.

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
  discussed". After `create`, add detail with `tasks note TASK-042 --append`
  (stdin or `--file`; stamped and locked) or edit the note file directly —
  the note body is free-form markdown; only keep **Work log** as the last
  section.
- **One reviewable unit.** If you can't state acceptance criteria, split it.
- **Pin the model when it matters.** `--model opus` on a hard task,
  `--model haiku` on a mechanical one; leave it unset when any model will do.
  The dispatcher uses the task's model automatically (task > config > CLI
  default), and tier-limited workers (`next --tier`) respect it: a haiku-tier
  worker never picks up an opus-pinned task.
- **Bridging an existing tracker?** Two patterns. If the worker can reach
  the tracker itself, use the wrapper-task pattern: the task body says
  "execute task <X> from this repo's own tracker via its own protocol; outbox
  + sentinel are your only duties to this queue", and the acceptance criteria
  reference the *other* system's receipts (its done status, its verification
  note) — review checks the real system, not the wrapper's word. If the
  worker should stay on plain files (cheap model, sandbox, no credentials),
  link the task instead: `--remote <ref>` (or `tasks remote TASK-042 <ref>`)
  — an opaque handle the project's configured `seed_hook`/`sync_hook`
  interpret. The dispatcher then seeds the remote item into the worker's
  `workspace/seed/` before spawn and pushes its outbox + workspace back at
  spawn, on an interval while supervised, and at fold with the outcome. Check
  `.agent-tasks/config.json` / `config.local.json` for whether hooks exist
  and what `remote` values they expect; without hooks, `remote` is inert
  metadata.
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
  - `dispatch sync [<worker>… | --all]` — push workers' outbox + workspace
    through the project's `sync_hook` now (running workers by default);
    `wait`/`watch` already do this on an interval, so this is for cron-style
    supervision or when you want the remote tracker fresh before you look.
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

## 4. Monitor — one wake per decision

**You are the expensive part of this system.** Your entire context is re-read
every time something wakes you, whether that wake carried a decision or a
heartbeat. So wait with `await`, which is built to make wakes rare and small:

```
tasks await --agent planner        # blocks, prints ONE digest, exits
```

It wakes you only for things you must decide: a task entering `review`, a
free-text blocker (a worker's question addressed to you), a task a worker
filed, a worker that died mid-task. It ignores narration, heartbeats, lease
renewals, and **your own writes** — a planner should never be woken by its own
bookkeeping. A burst of finishes inside the debounce window arrives as one
wake carrying all of them.

Read its exit code, and obey it:

| exit | meaning | what you do |
|---|---|---|
| 0 | the digest names what happened and what to read next | act on it, then re-arm |
| 2 | quiet timeout — nothing needed you for hours | **hand off** (§6), don't wait again |
| 4 | superseded or retired — another session owns this queue | **stop.** Never re-arm |

Run it in the background if you have other work in this session, in the
foreground if all you are doing is waiting. Never run two watchers, and don't
pair it with `dispatch watch --follow` — that streams a worker's whole
transcript into your context, which is for actively debugging one worker, not
for supervision.

Reads, cheapest first:
- `tasks since --cursor N` — everything that changed after cursor N (the
  delta, not the world). `--actionable` narrows it to decisions.
- `tasks board` — one-screen state; also prints the cursor and who is
  supervising.
- `tasks show TASK-042` — when you are about to act on that one task. The
  note's Work log is the worker's live narration; read it before concluding a
  worker is stuck.
- `tasks doctor` — periodic integrity check: index/note drift, orphan claims
  (crashed workers whose leases expired), stray notes, an abandoned supervisor
  lease. Exit 1 means findings; `--fix` repairs drifted frontmatter.

Dispatched workers report through per-worker **outboxes** that the dispatcher
folds into the task note when it observes the exit (`wait`, `watch`, or
`list`) — ending with `STATUS: review` or `STATUS: blocked: <reason>` (blocked
reopens the task with the reason as a blocker). `wait`/`watch` also
auto-heartbeat live workers, so a supervised worker's lease never decays.

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

## 6. Know when to die: the handoff

A long-lived planner turns into a liability once its context is mostly spent
or its work is mostly waiting. Hand off when any of these is true:

- your context is well past half gone and the queue still holds hours of work;
- the remaining work is long-running and nothing needs your judgment
  (`await` exited 2);
- the user is wrapping up for the day, or work will run unattended overnight;
- you just closed out a batch and the next one is a fresh planning problem.

One command:

```
tasks handoff --write --retire --agent planner --note "<what you know>"
```

- `--write` composes the machine facts: what is in review, what is blocked and
  on what, what is in flight and under which worker, what is ready to
  dispatch, the repo's branch/HEAD/dirty state, and the journal cursor.
- `--note` (or `--file -` for something longer) adds the only part the queue
  cannot reconstruct: **what you know that the files don't** — why you
  sequenced it this way, which worker's output you don't trust, what you would
  check first. Write it like you are briefing your replacement, because you
  are. A handoff with no intent is a board printout.
- `--retire` marks the queue unsupervised: any watcher still running exits,
  and nothing wakes this session again.

Then tell the user plainly: dispatched workers keep running headless, and in
the morning a fresh planner picks it up from `tasks handoff --show` for a few
hundred tokens instead of resurrecting a spent session.

The same discipline applies in reverse. If `await` exits 4, you have been
superseded or retired: write a handoff if you are holding anything worth
keeping, then stop. Do not re-arm a watcher, and do not keep working the queue
behind the session that took it over.
