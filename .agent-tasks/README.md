# agent-tasks queue

Machine-managed task queue shared by planner and worker agents
(https://github.com/bs7280/simple-subagent-dispatcher).

- `index.json` — source of truth for task **metadata**: status, assignee,
  blockers, priority, tags. Change these via the `tasks.py` CLI only, never by
  hand-editing this file (the CLI serializes concurrent writers).
- `tasks/TASK-NNN.md` — one note per task. The note body is free-form and
  agents are meant to edit it directly (description, notes, findings) — that is
  the point of the system. Keep **Work log** as the last section; the CLI
  appends entries to the end of the file.

- `config.json` — optional per-project dispatcher defaults (this is where a
  project records its own judgment calls). All keys optional:
  `worktree` (false), `worktree_root` (sibling `<repo>-worktrees/`),
  `model` (claude CLI default), `permission_mode` ("acceptEdits"),
  `allowed_tools` ([] — extra permission rules for what your workers may run),
  `bootstrap` (".claude/task-worker-bootstrap.sh"), `claude_bin` ("claude"),
  `extra_args` ([]).
- `runtime/` — machine-local dispatcher state (worker registry, spawn logs);
  self-gitignored, never committed.

Statuses: open → in_progress → review → done (or cancelled).
A blocker that names a task id auto-resolves when that task is done/cancelled;
free-text blockers stay until removed with `unblock`.

This folder belongs in the repo: commit it and task state travels with the
project (with history for free). Projects that prefer not to can gitignore it.
