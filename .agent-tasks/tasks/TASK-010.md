---
id: TASK-010
title: claude-tools TASK-018: worker prompt says check inbox/ between steps (plugin half)
status: done
created: 2026-09-18T21:30:08Z
remote: repos.claude-tools.llpm.tasks.TASK-018
---

# TASK-010 -- claude-tools TASK-018: worker prompt says check inbox/ between steps (plugin half)

## Description

Execute the PLUGIN half of the linked ticket only (the bridge-docs half in claude_tools is done by mission control). Add one paragraph to the worker prompt the dispatcher composes (scripts/dispatch.py): before each step, list inbox/ in the workspace, read anything new, acknowledge it in the outbox, act on it; a comment is not an interrupt, it arrives at a step boundary. Keep it to the plugin's existing prompt style. Bump the version to v0.9.2 the way v0.9.1 did (plugin.json + README), commit as `v0.9.2: worker prompt checks inbox/ between steps (claude_tools TASK-018)`. Never push, never tag. Do NOT run tests/smoke.py from inside this repo checkout without AGENT_TASKS_DIR pointing at a temp dir -- it writes into the live queue; run the other three suites normally.

## Acceptance criteria

- worker prompt contains the inbox paragraph (assert it in tests/dispatch_smoke.py the way existing prompt assertions do)
- dispatch_smoke, procs_smoke, activity_smoke green
- outbox ends with a ## Summary line and STATUS: review

## Notes

_(worker scratch space -- findings, decisions, open questions)_

## Work log

- 2026-09-18T21:30:08Z [mission-control] created
- 2026-09-18T21:30:08Z [task-010-48342c1e] claimed
- 2026-09-18T21:30:09Z [dispatcher] dispatched worker task-010-48342c1e (session 48342c1e-8da3-4141-ab2b-4b586dfa0ecf, pid 16760, model sonnet, cwd /Users/benshaughnessy/code/simple-subagent-orchestrator-worktrees/task-010-48342c1e)
- 2026-09-18T21:34:58Z [dispatcher] outbox of task-010-48342c1e folded:
    - 2026-09-18T21:30:32Z Starting TASK-010: add inbox/ check paragraph to worker prompt in scripts/dispatch.py, assert it in tests/dispatch_smoke.py, bump version to v0.9.2, run the three safe smoke suites, commit.
    - 2026-09-18T21:35:00Z Added inbox/ check paragraph to WORKER_PROMPT in scripts/dispatch.py (after the workspace layout, before the queue-read-only note) -- lists inbox/, reads/acknowledges anything new, notes it is not an interrupt.
    - 2026-09-18T21:37:00Z Bumped .claude-plugin/plugin.json to 0.9.2; added a README bullet under 'Linking a remote tracker: seed + sync hooks' documenting the inbox/ reverse-channel convention; added a dispatch_smoke.py assertion for the new prompt paragraph.
    - 2026-09-18T21:40:00Z Ran the three required smoke suites -- all green: dispatch_smoke.py (incl. the new inbox assertion), procs_smoke.py, activity_smoke.py. Did not run tests/smoke.py per the note's warning. Full output in notes/test-run.md.
    - 2026-09-18T21:42:00Z Checked inbox/: found inbox/0000-ticket-updated.md -- the linked ticket's status flipped to in-progress, but Goal/Scope/Acceptance criteria are unchanged from seed/ticket.md and the task note. No new action needed; noting the ticket's acceptance criteria mention pushing to GitHub and updating bridge docs, but the task note explicitly scopes this worker to the plugin half only, no push/tag -- followed the task note's narrower instructions.
    
    ## Summary
    Added an inbox/ check paragraph to WORKER_PROMPT in scripts/dispatch.py, asserted it in tests/dispatch_smoke.py, documented the convention in README, and bumped the plugin to v0.9.2 (commit 66b90a9); all three required smoke suites pass.
    
    STATUS: review
- 2026-09-18T21:34:58Z [task-010-48342c1e] status: in_progress -> review (outbox sentinel)
- 2026-09-18T21:38:24Z [mission-control] status: review -> done
