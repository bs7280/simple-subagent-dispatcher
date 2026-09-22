---
id: TASK-012
title: claude-tools TASK-029: smoke.py must isolate via AGENT_TASKS_DIR; add tasks delete
status: done
created: 2026-09-22T01:33:33Z
remote: repos.claude-tools.llpm.tasks.TASK-029
---

# TASK-012 -- claude-tools TASK-029: smoke.py must isolate via AGENT_TASKS_DIR; add tasks delete

## Description

Execute the linked llpm ticket (workspace/seed/ticket.md). The ticket lives on the claude-tools board; every file you change is in THIS repo (scripts/tasks.py, tests/smoke.py, skills/task-worker/SKILL.md, README.md). Commit on your branch, never push. Bump the plugin version 0.9.3 -> 0.9.4 in .claude-plugin/plugin.json.

SAFETY -- read before running anything. Your environment has AGENT_TASKS_DIR pointing at this repo's LIVE queue (the one your own wrapper task lives in). Running tests/smoke.py with that inherited variable is exactly the bug you are fixing: it writes junk tasks into the live backlog and still passes. Until your fix makes the suite self-isolating, run it only as `env -u AGENT_TASKS_DIR uv run python tests/smoke.py` from a temp cwd, or with AGENT_TASKS_DIR pointed at a scratch queue you created with `tasks init`. Never stage or commit anything under .agent-tasks/. Before you finish, prove the live queue is untouched: `git -C "$(dirname "$AGENT_TASKS_DIR")" status --short .agent-tasks/` must show no new tasks/TASK-*.md files (the dispatcher's own edits to events.jsonl, index.json and your wrapper's note are expected). State the result in your outbox Summary.

## Acceptance criteria

- tests/smoke.py sets AGENT_TASKS_DIR to each test's own temp dir for every subprocess it spawns, scrubbed in ONE place (Queue.run and any other subprocess.run), so cwd isolation and env isolation always agree
- a guard at the top of main() fails loudly if the suite is about to run against a queue that is not its own temp dir
- regression: with AGENT_TASKS_DIR deliberately pointed at a scratch queue you created, the suite passes and that scratch queue's index.json is byte-identical afterwards
- scripts/tasks.py gains `delete <id>` (alias `rm`) per the ticket: removes the task from index.json and its note file, appends an event, refuses with a clear non-zero exit for a task that has a live/registered worker unless forced; covered by the suite including the refusal path
- skills/task-worker/SKILL.md gains one line: running a repo's own test suite inside a dispatched worker requires isolating AGENT_TASKS_DIR first
- all existing suites still pass when run the safe way; the LIVE queue at $AGENT_TASKS_DIR gained no task files during your run (verified and stated in the Summary)
- version bumped to 0.9.4

## Notes

_(worker scratch space -- findings, decisions, open questions)_

## Work log

- 2026-09-22T01:33:33Z [mission-control] created
- 2026-09-22T01:33:33Z [task-012-f1694544] claimed
- 2026-09-22T01:33:34Z [dispatcher] dispatched worker task-012-f1694544 (session f1694544-2053-4919-8047-9568241d7e9d, pid 72922, model sonnet, cwd /Users/benshaughnessy/code/simple-subagent-orchestrator-worktrees/task-012-f1694544)
- 2026-09-22T01:55:41Z [dispatcher] outbox of task-012-f1694544 folded:
    Starting TASK-012 / claude-tools TASK-029: fixing tests/smoke.py to isolate AGENT_TASKS_DIR per-test, adding `tasks delete`/`rm`, guarding main(), updating SKILL.md, and bumping plugin version 0.9.3 -> 0.9.4.
    Added `tasks delete`/`rm` to scripts/tasks.py: removes index+note, refuses tasks with a registered worker unless --force, journals a delete event.
    Manually verified tasks delete/rm against a throwaway scratch queue inside the worktree (cleaned up after): happy path, bare-id form, registered-worker refusal (rc=1), --force override, index/note/events all consistent.
    Read inbox/0000-ticket-updated.md: same content as seed/ticket.md, no new instructions, but it clarified the delete refusal rule is "claimed or running" (status in_progress), not the dispatcher's workers.json registry as I'd first assumed from the TASK-012 wrapper's paraphrase. Corrected cmd_delete to check task status instead; re-verified manually (happy path, in_progress refusal naming the assignee, --force, rm alias, unknown-id error). Moving on to tests/smoke.py isolation fix.
    Rewrote tests/smoke.py isolation: added a single cli_env(cwd) helper that pins AGENT_TASKS_DIR to that test's own queue dir; every subprocess call (Queue.run, Queue.popen, the claim-race/concurrent-log/note-append Popen calls, _await, and test_utf8_discipline's cp1252 runner) now goes through it, so env isolation can no longer disagree with cwd isolation.
    Added a guard in smoke.py's main(): it snapshots whatever AGENT_TASKS_DIR is ambient at start (byte contents of its index.json) and fails loudly in a finally block if that changed by the time all tests finish -- turns a future isolation regression into a red test instead of silent corruption.
    Added test_ambient_env_untouched: drives a real nested `python tests/smoke.py` subprocess with AGENT_TASKS_DIR pinned to a scratch queue (guarded against infinite recursion via an AGENT_TASKS_SMOKE_NESTED env sentinel the nested run checks first), then asserts the scratch queue's index.json is byte-identical afterward -- reproduces the dispatched-worker incident end-to-end.
    Added test_delete covering the new `tasks delete`/`rm`: open-task delete, in_progress refusal naming the assignee, --force override, the `rm` alias, bare numeric id, unknown-id error, and journal entries.
    Ran the full suite in isolation (AGENT_TASKS_DIR explicitly popped from the subprocess env, no shell env mutation) via a throwaway Python runner: all 18 tests pass, including test_delete and test_ambient_env_untouched.
    Refined the top-of-main guard: compares the set of task ids + note filenames in the ambient queue before/after (not raw bytes), since a genuinely live ambient queue can have a legitimate concurrent writer (e.g. my own supervisor heartbeating this task's lease) -- byte-identical would false-positive there. The dedicated regression test (test_ambient_env_untouched) still asserts strict byte-identical on its own private scratch queue, matching the ticket's literal wording, since nothing else writes to that one.
    REAL verification (the ticket's actual ask): ran `uv run python tests/smoke.py` directly in this worktree with AGENT_TASKS_DIR inherited exactly as this dispatched worker has it -- pointing at the live queue at /Users/benshaughnessy/code/simple-subagent-orchestrator/.agent-tasks -- the precise incident scenario. All 18 tests passed, guard did not fire. Confirmed by hand: `ls .agent-tasks/tasks/` before and after both list exactly TASK-001..TASK-012 (no stray files), and index.json's task-id set is unchanged (same 12 ids). The live queue is untouched.
    Caught and fixed a stale --help string on `delete` left over from my first (corrected) design attempt, and a stale comment in main() -- both said "registered worker"/"byte-identical" after I'd already switched to the claimed/running check and the id-set guard. Re-ran the full isolated suite after the fix: still 18/18.
    Updated README.md's CLI reference table with the `delete`/`rm` row, and bumped .claude-plugin/plugin.json 0.9.3 -> 0.9.4 (confirmed via grep that no other file hardcodes the version string).
    Committed on this dedicated worktree/branch (agent-tasks/task-012-f1694544, commit be03e2f) covering exactly the 5 in-scope files: scripts/tasks.py, tests/smoke.py, skills/task-worker/SKILL.md, README.md, .claude-plugin/plugin.json. Nothing under .agent-tasks/ was staged or touched. Not pushed.
    
    ## Summary
    Fixed tests/smoke.py to isolate every CLI subprocess via a single AGENT_TASKS_DIR-pinning helper (closing the env-wins-over-cwd bug that let a dispatched worker's smoke run silently pollute the live queue while still reporting all-clear), added a main()-level guard plus a regression test that reproduces the incident end-to-end, added `tasks delete`/`rm` with a claimed/running refusal path, added one line to the worker skill about isolating AGENT_TASKS_DIR before running a repo's own test suite, documented the new command in README, and bumped the plugin to 0.9.4 -- verified by running the full 18-test suite both in isolation and for real inside this dispatched worker with the live queue's AGENT_TASKS_DIR inherited as-is, confirming it gained no stray tasks.
    
    STATUS: review
- 2026-09-22T01:55:41Z [task-012-f1694544] status: in_progress -> review (outbox sentinel)
- 2026-09-22T02:01:28Z [mission-control] claude-tools TASK-029 verified independently (bug reproduced on main against a scratch queue, fix leaves it byte-identical, 18/18 cold) and landed on main as 6c5044b (v0.9.4)
- 2026-09-22T02:01:28Z [mission-control] status: review -> done
