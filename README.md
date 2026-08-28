# simple-subagent-dispatcher

A Claude Code plugin (**`agent-tasks`**) for coordinating a planner agent and
multiple worker agents through a **file-based task queue** that lives in your
repo, plus a **dispatcher** that spawns, watches, and resumes headless
`claude -p` workers against it. No server, no database, no framework — a JSON
index plus one markdown note per task.

```
your-repo/
  .agent-tasks/
    index.json           # metadata: status, assignee, blockers, priority, tags
    tasks/
      TASK-001.md        # one note per task: description, criteria, notes, work log
      TASK-002.md
```

## Why files?

Coordinating agents through conversation context (e.g. plain subagent fan-out)
has two failure modes this design removes:

1. **Opaque** — you can't watch a subagent work. Here, every worker narrates
   into its task note's work log; `tail` it, `git diff` it, read it from any
   session.
2. **Not resumable** — if the parent session dies mid-run, in-flight progress
   is lost. Here, all state is on disk: any agent (or human) can pick the
   queue back up cold.

Because it's just files + a stdlib-Python CLI, *anything* can participate:
Claude Code subagents, independent headless `claude -p` sessions, other tools,
or you in an editor.

## Install

As a Claude Code plugin:

```
/plugin marketplace add bs7280/simple-subagent-dispatcher
/plugin install agent-tasks@simple-subagent-dispatcher
```

Or standalone — copy `scripts/tasks.py` anywhere; it has zero dependencies
(Python ≥ 3.8).

## Quickstart

```bash
tasks() { python3 /path/to/scripts/tasks.py "$@"; }

tasks init                                        # creates ./.agent-tasks/
tasks create "Fix login redirect loop" \
  --body "Repro: hit /app logged out..." \
  --criteria "- loop gone\n- auth tests pass" \
  --priority high
tasks create "Add e2e test for login" --blocked-by TASK-001

tasks next --claim --assignee worker-a            # atomically claim best ready task
tasks log TASK-001 "root cause: middleware matcher" --agent worker-a
tasks status TASK-001 review --agent worker-a     # worker hands back for review

tasks board                                       # planner's one-screen overview
tasks done TASK-001 --summary "verified via curl" --agent planner
tasks next                                        # TASK-002 now unblocked
```

With the plugin installed you mostly won't run this yourself — you'll say
things like *"plan this feature into tasks and dispatch workers"* (loads the
**task-planner** skill) or spawn workers with *"you are a task worker; work
TASK-042"* (loads the **task-worker** skill). `/agent-tasks:board` shows the
queue at any time.

## CLI reference

| command | what it does |
|---|---|
| `init` | create `.agent-tasks/` in the current directory |
| `create TITLE [--body --criteria --priority --tags --blocked-by]` | create a task + note |
| `list [--status s1,s2] [--assignee] [--all] [--json]` | list tasks (hides done/cancelled by default) |
| `show ID [--json]` | metadata + full note |
| `next [--claim --assignee NAME] [--json]` | best ready task (open, unblocked, priority-ordered); exit 1 if none |
| `claim ID --assignee NAME [--force]` | atomically claim an open, unblocked task |
| `heartbeat ID --assignee NAME` | extend your claim's lease during long steps |
| `status ID STATUS` | set status: `open`, `in_progress`, `review`, `done`, `cancelled` |
| `done ID [--summary]` | mark done (reviewer's call, not the worker's) |
| `block ID BLOCKER...` / `unblock ID BLOCKER...` | manage blockers |
| `assign ID NAME` | set assignee |
| `log ID MESSAGE` | append a timestamped work-log entry to the note |
| `note ID` | print the note file's path |
| `board [--json]` | one-screen status overview |

IDs are forgiving: `TASK-012`, `task-012`, and `12` all work. Every mutating
command takes `--agent` (who's acting, for the work log); set
`AGENT_TASKS_AGENT` once instead of repeating it. `AGENT_TASKS_DIR` overrides
the queue location (default: nearest `.agent-tasks/` walking up from cwd).

## Dispatcher

`scripts/dispatch.py` turns tasks into running workers. Each worker is a
**top-level headless `claude -p` process** with its own minted `--session-id` —
not an in-process subagent — which buys the two properties subagent fan-out
lacks:

- **Observable**: Claude Code persists every session's transcript to disk
  incrementally (`~/.claude/projects/…/<session-id>.jsonl`). `watch` tails the
  real transcript (located by globbing the session id), parsed into compact
  events — no duplicated logging.
- **Resumable**: if a worker dies (usage limit, crash, laptop sleep), nothing
  is lost. `resume` continues the same session — context and uncommitted
  working-tree edits intact.

| command | what it does |
|---|---|
| `start TASK-042 [--worktree\|--in-place] [--model] [--agent-name] [--force]` | spawn a worker with a ready-made prompt (claim → work → log → finish to `review`) |
| `list [--json]` | all workers; flags `[NEEDS-RESUME]` on any that exited while its task was still `in_progress` |
| `watch WORKER [--follow] [--tail N] [--from-start]` | show/tail the worker's transcript as compact events |
| `wait WORKER [--timeout]` | block until it exits — exit 3 = died mid-task, 2 = timeout |
| `resume WORKER [--prompt]` | continue a dead worker's session (default continuation prompt re-orients it: re-read task, check `git status`, carry on) |
| `stop WORKER` | SIGTERM; the session survives for `resume` |
| `prompt TASK-042` | print the worker prompt without spawning (paste into any session) |

`WORKER` accepts a worker id, a unique prefix, or a task id (→ that task's
latest worker).

### In-place vs. worktree — the project's call

By default workers run **in the repo checkout**. `--worktree` gives each worker
an isolated `git worktree` (branch `agent-tasks/<worker-id>`, placed in a
sibling `<repo>-worktrees/` dir) — necessary when parallel workers touch
overlapping files, overkill when they don't. Neither is required: set your
project's default in **`.agent-tasks/config.json`** (all keys optional):

```json
{
  "worktree": false,
  "worktree_root": null,
  "model": null,
  "permission_mode": "acceptEdits",
  "allowed_tools": [],
  "bootstrap": ".claude/task-worker-bootstrap.sh",
  "claude_bin": "claude",
  "extra_args": []
}
```

- **Permissions** — the default is `acceptEdits`, plus an automatic allowlist
  entry for the queue CLI itself (so claim/log/block/finish always work
  unattended), plus whatever rules you put in `allowed_tools` (e.g.
  `"Bash(pnpm test:*)"`, `"Bash(git commit:*)"` — the commands *your* workers
  need). Everything else is **denied, never prompted** — a headless session
  can't answer a prompt, and a denied action lets the run continue and shows
  up in the transcript. Tested live: `--permission-mode auto` sounds right for
  unattended work, but in a repo with no accumulated allowlist it denies even
  in-project file writes headless — set it in config only for repos whose
  `.claude/settings` already allow what workers need. `bypassPermissions` is
  scoped by Anthropic's docs to isolated environments (containers/VMs); don't
  use it on a bare laptop.
- **`bootstrap`** — a fresh worktree has no `node_modules` and no gitignored
  `.env` files. If the repo has this script, the dispatcher runs it (cwd = the
  new worktree, non-fatal) *before* launching the worker, so the worker doesn't
  burn turns rediscovering `pnpm install`. Mechanism generic, policy local:
  each repo writes its own (e.g. hardlink-seed `node_modules` from the main
  checkout + `pnpm install --frozen-lockfile` + copy `.env` files).
- Workers get `AGENT_TASKS_DIR` pointing at the **shared** queue, so in
  worktree mode the worktree's own checked-out copy of `.agent-tasks/` is never
  written to.

### The worker prompt bans the known death mode

A headless worker that launches a long command in the background and ends its
turn "waiting" **dies** — `-p` sessions are never re-invoked when background
work finishes. The dispatch prompt bans this outright, and `resume` exists for
when it (or anything else) kills a worker anyway.

## How coordination works

- **`index.json` is the source of truth for metadata** — status, assignee,
  blockers, priority. Only the CLI touches it: writes are serialized through a
  lock file and land via atomic rename, so concurrent workers can't corrupt it
  and two workers can't claim the same task.
- **The note body belongs to agents.** Description, acceptance criteria, notes,
  findings — edit the markdown directly; that's the point. Keep **Work log**
  as the last section (the CLI appends there).
- **Blockers** are strings. One that names a task id auto-resolves when that
  task reaches `done`/`cancelled`; free text (*"waiting on API key from Ben"*)
  stays until explicitly removed — and doubles as a question channel back to
  the planner. `next`/`claim` refuse blocked tasks.
- **Claims are leases, not locks.** `claim` stamps `claimed_at` +
  `lease_until` (config `lease_minutes`, default 90). Workers `heartbeat`
  during long steps; if a worker crashes, its lease simply expires and the
  task becomes claimable again — `next`/`claim` steal it and record the steal
  (old assignee, expiry time) in the work log. `board` shows expired-lease
  tasks in their own bucket. No human unsticking required.
- **Workers finish to `review`, never `done`.** Closing is the reviewer's
  (planner's or human's) call, after actually running the acceptance checks.
- **Scope discipline**: a worker that finds adjacent work *creates a new task*
  instead of expanding its own — the queue is the escalation channel.

**The folder belongs in the repo.** Commit `.agent-tasks/` by default — task
state travels with the project and you get history for free. Machine-local
dispatcher state (`runtime/`: worker registry, pids, spawn logs) self-gitignores
so it never ends up committed. Projects that prefer to keep the queue out of
version control (e.g. heavy multi-branch work where the index would conflict)
can gitignore the folder instead — that's a per-project call.

## What's in the plugin

- **`task-planner` skill** — break work into self-contained task notes,
  sequence with blockers (including serializing exclusive resources: DB
  migrations, shared dev DB resets, browser/e2e — chain them, don't parallelize
  them), dispatch workers, monitor, review.
- **`task-worker` skill** — claim one task, work only that scope, narrate into
  the work log, block-and-stop instead of guessing, finish to `review`. Bans
  the known headless death mode: launching a long command in the background
  and ending the turn "waiting" (headless sessions are never re-invoked).
- **`/agent-tasks:board`** — summarize the queue: what's in review, what's
  blocked on what, who's working on what.
- **`scripts/dispatch.py`** — the dispatcher (see above): headless workers as
  durable, watchable, resumable `claude -p` sessions.

## Roadmap

This repo dogfoods itself: the backlog lives in [`.agent-tasks/`](.agent-tasks/)
in exactly the format the plugin ships. Browse the task notes there, or:

```bash
python3 scripts/tasks.py board
```

## License

MIT
