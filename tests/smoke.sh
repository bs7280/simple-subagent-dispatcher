#!/usr/bin/env bash
# End-to-end smoke test for scripts/tasks.py. Runs in a throwaway temp dir.
set -euo pipefail

CLI="$(cd "$(dirname "$0")/.." && pwd)/scripts/tasks.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
tasks() { python3 "$CLI" "$@"; }
fail() { echo "FAIL: $1" >&2; exit 1; }

# init + create
tasks init | grep -q "initialized" || fail "init"
tasks init | grep -q "already initialized" || fail "re-init should be a no-op"
T1=$(tasks create "First task" --body "Do the thing" --priority high --tags auth,backend | awk '{print $2}')
T2=$(tasks create "Second task" --blocked-by "$T1" | awk '{print $2}')
T3=$(tasks create "Third task" --blocked-by "waiting on API key" --priority low | awk '{print $2}')
[ "$T1" = "TASK-001" ] || fail "expected TASK-001, got $T1"
[ -f ".agent-tasks/tasks/$T1.md" ] || fail "note file missing"

# list: T2/T3 flagged blocked
tasks list | grep -F "$T2" | grep -q "blocked ← $T1" || fail "T2 not shown blocked"
tasks list | grep -F "$T3" | grep -q "blocked ← waiting on API key" || fail "T3 not shown blocked"

# next skips blocked tasks, picks T1
[ "$(tasks next | head -1 | awk '{print $1}')" = "$T1" ] || fail "next should pick $T1"

# claim race: exactly one of two concurrent claims wins
: > wins.txt
( tasks claim "$T1" --assignee racer-a >/dev/null 2>&1 && echo a >> wins.txt ) &
( tasks claim "$T1" --assignee racer-b >/dev/null 2>&1 && echo b >> wins.txt ) &
wait
[ "$(wc -l < wins.txt | tr -d ' ')" = "1" ] || fail "claim race: expected exactly 1 winner, got $(cat wins.txt)"

# claimed task is in_progress, note frontmatter synced, work log written
tasks show "$T1" --json | python3 -c "import json,sys; t=json.load(sys.stdin); assert t['status']=='in_progress' and t['assignee'] in ('racer-a','racer-b'), t" || fail "claim state"
grep -q "^status: in_progress" ".agent-tasks/tasks/$T1.md" || fail "frontmatter not synced"
grep -q "claimed" ".agent-tasks/tasks/$T1.md" || fail "work log missing claim entry"

# claiming a non-open task fails without --force
tasks claim "$T1" --assignee thief 2>/dev/null && fail "re-claim should fail" || true

# log + worker finishes to review, reviewer closes
tasks log "$T1" "root cause found" --agent racer-a >/dev/null
grep -q "root cause found" ".agent-tasks/tasks/$T1.md" || fail "log entry missing"
tasks status "$T1" review --agent racer-a >/dev/null
tasks done "$T1" --summary "verified by smoke test" --agent planner >/dev/null
grep -q "verified by smoke test" ".agent-tasks/tasks/$T1.md" || fail "done summary missing"

# task-id blocker auto-resolves; free-text one does not
[ "$(tasks next | head -1 | awk '{print $1}')" = "$T2" ] || fail "T2 should be unblocked after T1 done"
tasks claim "$T3" --assignee w 2>/dev/null && fail "T3 still blocked by free text" || true
tasks unblock "$T3" "waiting on API key" >/dev/null
tasks claim "$T3" --assignee w >/dev/null || fail "T3 claim after unblock"

# next --claim, loose id forms, note path, board
NX=$(tasks next --claim --assignee worker-x) || fail "next --claim"
echo "$NX" | grep -q "claimed $T2 (worker-x)" || fail "next --claim output: $NX"
tasks show 2 >/dev/null || fail "bare-number id"
tasks show task-002 >/dev/null || fail "lowercase id"
[ "$(tasks note 3)" = "$(cd .agent-tasks/tasks && pwd -P)/$T3.md" ] || fail "note path"
tasks board | grep -q "done: 1, cancelled: 0" || fail "board counts"
tasks board --json | python3 -c "import json,sys; b=json.load(sys.stdin); assert b['counts']['in_progress']==2, b" || fail "board json"

# queue drained -> next exits 1
tasks next >/dev/null 2>&1 && fail "next should exit 1 when nothing ready" || true

# list --json parses and carries unresolved_blockers
tasks list --all --json | python3 -c "import json,sys; ts=json.load(sys.stdin); assert len(ts)==3 and all('unresolved_blockers' in t for t in ts)" || fail "list json"

# no stale lock left behind
[ ! -f .agent-tasks/.lock ] || fail "lock file left behind"

echo "ALL SMOKE TESTS PASSED"
