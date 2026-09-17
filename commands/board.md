---
description: Show the agent-tasks board — statuses, assignees, blockers, and what needs attention
---

Run `uv run python "${CLAUDE_PLUGIN_ROOT}/scripts/tasks.py" board` (no `uv`? use `python3` -- the script is stdlib-only) (add `list --all` if more detail helps).

If the board reports a handoff on file and this session hasn't read it yet, read it first
(`tasks.py handoff --show`) — it is the previous planner's briefing.

Summarize for the user: what's sitting in **review** awaiting verification, what's **blocked** and on what (free-text blockers are usually questions for the planner/user), what's **in progress** and by whom, and what's ready to dispatch next.

If it fails because no `.agent-tasks/` folder exists, say so and offer to run `init`.
