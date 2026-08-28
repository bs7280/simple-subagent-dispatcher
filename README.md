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

Or standalone — copy `scripts/tasks.py` (and `dispatch.py` + `procs.py` if
you want the dispatcher) anywhere; zero dependencies (Python ≥ 3.8).

## Requirements & compatibility

- **Python ≥ 3.8, stdlib only** — no runtime dependencies, ever. The canonical
  interpreter invocation is **`uv run python`**, set as config `runner`
  (change it to `["python"]` or an absolute interpreter if you don't use uv).
  Everything the system composes — worker prompts, permission pre-approvals,
  the bootstrap invocation — is built from the same `runner`, so they cannot
  drift apart.
- **Cross-platform, Windows included.** All process handling goes through
  `scripts/procs.py` (detached spawn, liveness probe that never signals,
  tree terminate) with native Windows implementations — no POSIX-only calls
  on the main path. All executable surfaces (tests, bootstrap hooks) are
  Python, not shell.
- `claude_bin` may be a string or an argv list, and is resolved through
  `shutil.which` at spawn time — on Windows the claude CLI is an npm shim
  (`claude.cmd`), which plain `Popen` can't find; `which` honors `PATHEXT`.
  A `.cmd`/`.bat` result is **unwrapped to a sibling `.exe`** when one exists;
  when the binary is still a batch file, the multi-line worker prompt does
  **not** ride argv (cmd.exe truncates argv at the first newline — a bug that
  hides, because line 1 survives): it is piped on **stdin** from a durable
  prompt file, with a one-line argv pointer. Prompt files are written to
  `.agent-tasks/runtime/prompts/<worker-id>.txt` for *every* spawn and resume
  (audit + resume trail), and `prompt_via` (`auto`/`argv`/`stdin`) can force
  either path. An unresolvable binary fails fast with the fix named, and the
  pre-claim is reverted.

## Quickstart

```bash
tasks() { uv run python /path/to/scripts/tasks.py "$@"; }

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
| `create TITLE [--body --criteria --priority --tags --blocked-by --model --resources]` | create a task + note (`--model` pins the model it needs; `--resources` declares exclusive-resource tags) |
| `list [--status s1,s2] [--assignee] [--all] [--json]` | list tasks (hides done/cancelled by default) |
| `show ID [--json]` | metadata + full note |
| `next [--claim --assignee NAME] [--tier MODEL] [--json]` | best ready task (open/expired-lease, unblocked, priority-ordered); `--tier` only draws tasks at/below that `model_tiers` entry; exit 1 if none |
| `claim ID --assignee NAME [--tier MODEL] [--force]` | atomically claim an open (or expired-lease), unblocked task |
| `heartbeat ID --assignee NAME` | extend your claim's lease during long steps |
| `status ID STATUS` | set status: `open`, `in_progress`, `review`, `done`, `cancelled` |
| `done ID [--summary]` | mark done (reviewer's call, not the worker's) |
| `block ID BLOCKER...` / `unblock ID BLOCKER...` | manage blockers |
| `assign ID NAME` | set assignee |
| `log ID MESSAGE` | append a timestamped work-log entry to the note |
| `note ID [--append [--file F] --agent X]` | print the note's path — or `--append` a stamped block (stdin or `--file`) into its `## Notes` section under the queue lock: the direct-agent equivalent of the worker outbox |
| `board [--json]` | one-screen status overview |
| `lock NAME --agent X` / `unlock NAME --agent X` | named mutex for shared-checkout spans (e.g. `lock commit`); exit 4 = BUSY naming the holder; stale locks stolen after `mutex_stale_minutes` (default 30) |
| `doctor [--fix]` | integrity report: index/note status drift, orphan claims, stray/missing notes; exit 1 on findings (`--fix` rewrites drifted frontmatter from the index) |

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
| `start TASK-042 [--worktree\|--in-place] [--model] [--agent-name] [--force]` (alias: `run`) | **pre-claims the task atomically, then** spawns a worker with a ready-made prompt (verify assignment → work → log → heartbeat → finish to `review`) |
| `list [--json]` | all workers; flags `[NEEDS-RESUME]` on any that exited while its task was still `in_progress` |
| `watch WORKER [--follow] [--tail N] [--from-start]` | one merged timeline from both evidence streams — `[session]` transcript events beside `[spawn]` log lines (permission warnings, CLI errors), so a deny-rule warning shows up next to the tool call it explains; degrades to spawn-only when the transcript can't be located, and picks the transcript up live if it appears |
| `wait WORKER [--timeout]` | block until it exits — exit 3 = died mid-task, 2 = timeout; on exit it folds the outbox and runs `tasks doctor`, printing findings (exit code stays task-status-driven) |
| `resume WORKER [--prompt]` | continue a dead worker's session (default continuation prompt re-orients it: re-read task, check `git status`, carry on) |
| `stop WORKER` | SIGTERM; the session survives for `resume` |
| `prompt TASK-042` | print the worker prompt without spawning (paste into any session) |

`WORKER` accepts a worker id, a unique prefix, or a task id (→ that task's
latest worker).

### Config: project file + machine overlay

`.agent-tasks/config.json` is **project** config — committed and shared. A
claude path is a **machine** fact, so there's a second layer:
`.agent-tasks/config.local.json`, a gitignored overlay (init adds the
gitignore entry) merged key-by-key over `config.json` — any key can be
overridden there (`claude_bin`, `runner`, `model`, `worktree`, …). For the
claude binary specifically the full ladder is:

1. `--claude-bin` CLI flag
2. `AGENT_TASKS_CLAUDE_BIN` env var
3. `config.local.json`
4. `config.json`
5. auto-resolution (`shutil.which` + the batch-shim exe-unwrap)

Config never replaces resolution: configuring the *name* `claude` still
resolves to the shim and still gets the unwrap/stdin treatment.

### In-place vs. worktree — the project's call

By default workers run **in the repo checkout**. `--worktree` gives each worker
an isolated `git worktree` (branch `agent-tasks/<worker-id>`, placed in a
sibling `<repo>-worktrees/` dir) — necessary when parallel workers touch
overlapping files, overkill when they don't. Shared-tree workers are taught
commit discipline: wrap stage→commit in the `lock commit` named mutex and
stage only named files (never `git add -A`), so concurrent workers can't
sweep up each other's half-done edits. Neither is required: set your
project's default in **`.agent-tasks/config.json`** (all keys optional):

```json
{
  "runner": ["uv", "run", "python"],
  "lease_minutes": 90,
  "model_tiers": ["haiku", "sonnet", "opus"],
  "worktree": false,
  "worktree_root": null,
  "model": null,
  "permission_mode": "acceptEdits",
  "allowed_tools": [],
  "bootstrap": ".claude/task-worker-bootstrap.py",
  "claude_bin": "claude",
  "extra_args": []
}
```

- **Permissions** — the default is `acceptEdits`, plus an automatic allowlist
  entry for the queue CLI itself, composed from the configured `runner` in
  both quoting styles so pre-approvals always match the prompts, plus whatever
  rules you put in `allowed_tools`. In the other direction, dispatched workers
  get `--disallowedTools` rules `Edit(…/index.json)` and `Edit(…/tasks/**)` —
  only `Edit(path)` rules are matched by file permission checks, and they
  cover *all* file-editing tools, so those two rules are the whole fence.
  Queue state is read-only to workers while the outbox stays writable (it's
  the sanctioned surface) (e.g.
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
  `.env` files. If the repo has this **Python** script, the dispatcher runs it
  via the configured `runner` (cwd = the new worktree, non-fatal) *before*
  launching the worker, so the worker doesn't burn turns rediscovering
  `pnpm install`. Mechanism generic, policy local: each repo writes its own
  (e.g. seed `node_modules` from the main checkout, `subprocess.run` a
  `pnpm install --frozen-lockfile`, copy `.env` files).
- Workers get `AGENT_TASKS_DIR` pointing at the **shared** queue, so in
  worktree mode the worktree's own checked-out copy of `.agent-tasks/` is never
  written to.

### Dispatch pre-claims; the worker never touches the queue

`start` claims the task (assignee = the worker id it mints) **before** spawning
— atomically, under the queue lock. Of two concurrent dispatches of the same
task, the loser exits immediately with the holder named, instead of burning a
whole session to discover its claim fails. If worktree setup or the spawn
itself fails, the pre-claim is reverted to `open`. Worker-side self-claim
still works for humans and external agents using the task-worker skill.

### The outbox: reporting with the grain of the model

Live dogfooding taught the defining lesson: a cheap worker model **ignored the
CLI entirely**, hand-edited the task note with its native file tools, and
reported success — because small models are heavily trained on read/write/edit
file operations and reliably fumble custom CLI invocations that replace them.
So dispatched workers don't get a CLI contract at all:

- Each worker gets a **per-worker outbox** (`.agent-tasks/runtime/outbox/
  <worker-id>.md`, also exported as `AGENT_TASKS_OUTBOX`): one writer, ordinary
  file tools, plain markdown. Progress, findings, escalations — all prose.
- The worker signals its terminal state with one sentinel line: `STATUS:
  review` or `STATUS: blocked: <reason>`. Tiny, forgiving grammar — one token,
  last occurrence wins, case-insensitive. Nothing else to get wrong.
- When the dispatcher observes the exit (`wait`, `watch`, or `list`), it
  **folds** the outbox into the task note's work log under the queue lock,
  validates the sentinel (workers can only reach `review` or `blocked` —
  `done` stays reviewer-only; a stolen task's stale sentinel is ignored), and
  applies it through the same primitives the CLI uses. `blocked: <reason>`
  reopens the task with the reason recorded as a blocker. Folding archives the
  outbox to `<worker-id>.folded.md` in the same locked span, so a crashed fold
  can never double-append. No sentinel = the existing died-mid-task handling.
- `wait`/`watch` **auto-heartbeat** while the worker's pid is verifiably
  alive, so dispatched workers carry no heartbeat duty (the CLI `heartbeat`
  remains the backstop for externally-run agents).

Net: a dispatched worker needs **zero queue-CLI calls** — task work, outbox
writes, one sentinel line.

### The worker prompt bans the known death mode

A headless worker that launches a long command in the background and ends its
turn "waiting" **dies** — `-p` sessions are never re-invoked when background
work finishes. The dispatch prompt bans this outright, and `resume` exists for
when it (or anything else) kills a worker anyway.

## How coordination works

- **`index.json` is the source of truth for metadata** — status, assignee,
  blockers, priority. Only the CLI touches it: writes are serialized through a
  lock file and land via atomic rename, so concurrent workers can't corrupt it
  and two workers can't claim the same task. The `status:` line in a note's
  frontmatter is display-only — written on change, never read back; `tasks
  doctor` reports drift, orphan claims, and stray/missing notes (exit 1 on
  findings).
- **The note body belongs to agents.** Description, acceptance criteria, notes,
  findings — edit the markdown directly; that's the point. Keep **Work log**
  as the last section (the CLI appends there).
- **Blockers** are strings. One that names a task id auto-resolves when that
  task reaches `done`/`cancelled`; free text (*"waiting on API key from Ben"*)
  stays until explicitly removed — and doubles as a question channel back to
  the planner. `next`/`claim` refuse blocked tasks.
- **Claims are leases, not locks.** `claim` stamps `claimed_at` +
  `lease_until` (config `lease_minutes`, default 90). Dispatched workers are
  auto-heartbeated by their supervisor (`wait`/`watch`); direct/external
  agents `heartbeat` themselves during long steps. If a worker crashes, its
  lease simply expires and the task becomes claimable again — `next`/`claim` steal it and record the steal
  (old assignee, expiry time) in the work log. `board` shows expired-lease
  tasks in their own bucket. No human unsticking required.
- **Resources are mutexes, blockers are ordering.** `--resources db,browser`
  tags a task's exclusive needs; the queue never lets two live claims hold the
  same tag (`next` skips conflicted tasks, `claim` refuses naming the holder),
  and an expired lease releases its holds. `board` shows who holds what.
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
