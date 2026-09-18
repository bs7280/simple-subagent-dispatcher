---
id: TASK-009
title: claude-tools TASK-017: dispatch resume re-claims; fold accepts review from open
status: done
created: 2026-09-18T21:10:02Z
remote: repos.claude-tools.llpm.tasks.TASK-017
---

# TASK-009 -- claude-tools TASK-017: dispatch resume re-claims; fold accepts review from open

## Description

Execute the linked ticket (claude-tools board) in this repo, the agent-tasks plugin itself. Implement fix options a + b at minimum, c if it is a small clean addition to `tasks await`. The first acceptance criterion is an automated test that stubs the claude process the way the existing dispatch tests do. Follow the release convention visible in `git log -3 --stat` (subjects `v0.9.0: ...` with the version bump); commit as `v0.9.1: ... (claude_tools TASK-017)`. Never push, never tag.

## Acceptance criteria

- repro test: blocked -> unblock -> resume -> review ends review, work log shows it, await returns
- resume of a task open but assigned elsewhere fails loudly; existing fold tests pass
- outbox ends with a ## Summary line and STATUS: review

## Notes

_(worker scratch space -- findings, decisions, open questions)_

## Work log

- 2026-09-18T21:10:02Z [mission-control] created
- 2026-09-18T21:10:02Z [task-009-946a16cc] claimed
- 2026-09-18T21:10:03Z [dispatcher] dispatched worker task-009-946a16cc (session 946a16cc-6c9f-40b1-9cca-00c1d6782ad1, pid 65093, model sonnet, cwd /Users/benshaughnessy/code/simple-subagent-orchestrator-worktrees/task-009-946a16cc)
- 2026-09-18T21:25:56Z [dispatcher] outbox of task-009-946a16cc folded:
    Starting TASK-009 / TASK-017: fix dispatch resume so it re-claims an `open` task assigned to the resuming worker, and fold_outbox accepts review/blocked sentinels from `open` when the assignee matches. Will add a repro test (stub claude) covering blocked -> unblock -> resume -> review -> await.
    Read scripts/dispatch.py (cmd_resume, fold_outbox, cmd_wait) and scripts/tasks.py (apply_claim, cmd_unblock, classify_event, tasks await) plus tests/dispatch_smoke.py to find insertion points and test conventions.
    Plan: (a) cmd_resume re-claims an open task under lock (assignee match -> in_progress + fresh lease; assignee mismatch -> die loudly); (b) fold_outbox accepts review/blocked sentinels from status "open" (not just in_progress) when assignee matches, logging the from-status honestly; (c) tag the genuinely-foreign ignored branch with a new event kind "ignored" and map it to the "needs-resume" await trigger (small, reuses existing trigger vocabulary). Version bump to 0.9.1 per release convention.
    
    INCIDENT (unrelated to my code changes, flagging for the planner -- I have NOT touched the live queue myself): running `uv run python tests/smoke.py` in my worker shell wrote 3 real entries into the LIVE queue at /Users/benshaughnessy/code/simple-subagent-orchestrator/.agent-tasks -- TASK-010 "First task", TASK-011 "Second task", TASK-012 "Third task" (index.json + tasks/TASK-01{0,1,2}.md + events.jsonl seq 4-6), before the test failed on a numbering assertion ("expected TASK-001, got TASK-010") and stopped. Root cause: my session has AGENT_TASKS_AGENT/AGENT_TASKS_DIR/etc. exported (pointing at the real queue, since I'm a dispatched worker), and tests/smoke.py's `Queue.run()` (tests/smoke.py:34-39) calls `subprocess.run([PY, CLI, *args], cwd=self.cwd, ...)` WITHOUT an `env=` override, so it inherits my shell's env -- `tasks.find_dir()` prefers $AGENT_TASKS_DIR over `cwd`, so every command in the test landed on the real queue instead of its own tempdir. tests/dispatch_smoke.py's sibling `sh()` helper does this correctly (`env.pop("AGENT_TASKS_DIR", None)`); tests/smoke.py never adopted that pattern. This is a pre-existing test-isolation bug, out of TASK-017's scope -- not fixing it here, just flagging it.
    I did NOT touch index.json or any tasks/*.md myself (queue state is explicitly read-only to me). Whoever reviews this needs to either delete TASK-010/011/012 (index entries + note files) via the tasks CLI from a session with a *clean* env, or judge them harmless enough to leave; either way tests/smoke.py needs an env fix (env.pop the AGENT_TASKS_* vars, like dispatch_smoke.py does) before anyone runs it again from a live worker shell.
    Next: re-running tests/smoke.py with the AGENT_TASKS_* vars explicitly unset for that one invocation (my own shell env, not the test's code) so I can still verify my tasks.py change (classify_event) didn't break it, without touching the real queue again.
    Re-ran all four suites (tests/dispatch_smoke.py, tests/smoke.py, tests/procs_smoke.py, tests/activity_smoke.py) via a subprocess with AGENT_TASKS_DIR/AGENT_TASKS_AGENT/AGENT_TASKS_OUTBOX/AGENT_TASKS_WORKSPACE stripped so the real queue could not be touched again -- all four green, no further live-queue changes. Full evidence + a bug I found and fixed in my own test (an `await --agent planner-repro` watcher that leaked the queue's single supervisor slot) are in notes/verification.md.
    Implemented and committed all three fix options from the ticket: (a) `dispatch.py cmd_resume` re-claims an `open` task to `in_progress` under the lock when the resuming worker still owns it (fresh lease, logged), and dies loudly if it's open but assigned to someone else; (b) `fold_outbox` accepts review/blocked sentinels from `open` as well as `in_progress` when the assignee matches, logging the from-status honestly; (c) the genuinely-foreign ignored branch now logs `kind="ignored"` with structured data, and `tasks.py classify_event` rides it into the existing `needs-resume` await trigger. Bumped plugin.json to 0.9.1, updated README's `resume` row and the await wake-reasons list, and committed as `0cebf42` following the repo's release convention. Never pushed, never tagged.
    
    ## Summary
    `dispatch resume` now re-claims an `open` task it still owns before resuming (and refuses loudly if it doesn't), `fold_outbox` accepts review/blocked sentinels from `open` too, and a dropped/foreign sentinel now wakes `tasks await` via `needs-resume` -- verified by a new repro test plus 3 more scenarios in `tests/dispatch_smoke.py`, with all 4 existing test suites still green; separately flagged (not fixed) a live-queue-writing env-isolation bug in `tests/smoke.py` that I hit while verifying, including the 3 stray tasks (TASK-010/011/012) it created in the real `.agent-tasks` queue that still need a reviewer decision.
    
    STATUS: review
- 2026-09-18T21:25:56Z [task-009-946a16cc] status: in_progress -> review (outbox sentinel)
- 2026-09-18T21:29:28Z [mission-control] status: review -> done
