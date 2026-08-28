---
description: Show the agent-tasks board — statuses, assignees, blockers, and what needs attention
---

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tasks.py" board` (add `list --all` if more detail helps).

Summarize for the user: what's sitting in **review** awaiting verification, what's **blocked** and on what (free-text blockers are usually questions for the planner/user), what's **in progress** and by whom, and what's ready to dispatch next.

If it fails because no `.agent-tasks/` folder exists, say so and offer to run `init`.
