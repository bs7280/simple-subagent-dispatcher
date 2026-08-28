#!/usr/bin/env bash
# Dispatcher smoke test: uses a stub `claude` binary to exercise spawn/list/
# wait/NEEDS-RESUME/resume/stop/worktree/bootstrap without real sessions.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")/.." && pwd)/scripts"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
tasks() { python3 "$SCRIPTS/tasks.py" "$@"; }
dispatch() { python3 "$SCRIPTS/dispatch.py" "$@"; }
fail() { echo "FAIL: $1" >&2; exit 1; }

git init -q repo && cd repo
git commit -q --allow-empty -m init

# stub claude: records argv + env, then lingers briefly like a real worker
cat > "$TMP/fake-claude" <<'FAKE'
#!/usr/bin/env bash
echo "FAKE-CLAUDE ARGS: $*"
echo "FAKE-CLAUDE AGENT_TASKS_DIR: ${AGENT_TASKS_DIR:-unset} AGENT: ${AGENT_TASKS_AGENT:-unset} CWD: $(pwd -P) CLAUDECODE: ${CLAUDECODE:-unset}"
sleep 3
FAKE
chmod +x "$TMP/fake-claude"

tasks init >/dev/null
printf '{"claude_bin": "%s", "model": "haiku"}\n' "$TMP/fake-claude" > .agent-tasks/config.json
T1=$(tasks create "Dispatched task" --body "do it" | awk '{print $2}')
T2=$(tasks create "Worktree task" | awk '{print $2}')

# prompt (no spawn) contains the death-mode ban and the claim step
dispatch prompt "$T1" | grep -q "NEVER launch a long-running command" || fail "prompt missing death-mode ban"
dispatch prompt "$T1" | grep -q "claim $T1" || fail "prompt missing claim step"

# start in-place (default)
WID=$(dispatch start "$T1" | awk '/^started /{print $2}')
[ -n "$WID" ] || fail "no worker id from start"
dispatch list | grep -F "$WID" | grep -q "running" || fail "worker not listed running"
grep -q "dispatched worker $WID" ".agent-tasks/tasks/$T1.md" || fail "dispatch not logged to task note"
git check-ignore -q .agent-tasks/runtime/workers.json || fail "runtime/ not self-gitignored"

# stub got the right env + args + cwd (give it a beat to write)
sleep 1
LOG=".agent-tasks/runtime/logs/$WID.out"
grep -q -- "--permission-mode acceptEdits" "$LOG" || fail "permission-mode not passed"
grep -q -- "tasks.py:\*)" "$LOG" || fail "queue CLI not allowlisted"
grep -q -- "--model haiku" "$LOG" || fail "config model not passed"
grep -q "AGENT_TASKS_DIR: $(cd .agent-tasks && pwd -P)" "$LOG" || fail "AGENT_TASKS_DIR not exported"
grep -q "CWD: $(pwd -P)" "$LOG" || fail "in-place worker not in repo checkout"
grep -q "CLAUDECODE: unset" "$LOG" || fail "parent CLAUDE_* env leaked into worker"

# simulate the worker having claimed, then dying mid-task
tasks claim "$T1" --assignee "$WID" >/dev/null
rc=0; dispatch wait "$WID" || rc=$?
[ "$rc" = "3" ] || fail "wait should exit 3 for died-mid-task, got $rc"
dispatch list | grep -F "$WID" | grep -q "NEEDS-RESUME" || fail "NEEDS-RESUME not flagged"

# resume: same session id, continuation prompt, registry updated
dispatch resume "$WID" >/dev/null
dispatch list | grep -F "$WID" | grep -q "running" || fail "resumed worker not running"
dispatch list | grep -F "$WID" | grep -q "NEEDS-RESUME" && fail "running worker still flagged" || true
sleep 1
grep -q -- "--resume" "$LOG" || fail "resume flag not passed"
SID=$(dispatch list --json | python3 -c "import json,sys; ws=json.load(sys.stdin); w=[x for x in ws if x['id']=='$WID'][0]; print(w['session_id']); assert w['resumes']==1, w")
grep -q -- "--resume $SID" "$LOG" || fail "resume used wrong session id"

# stop: process dies, wait returns immediately
dispatch stop "$WID" | grep -q "stopped $WID" || fail "stop output"
sleep 1
dispatch list | grep -F "$WID" | grep -q "exited" || fail "stopped worker still alive"

# worker id resolution by task id and by prefix
dispatch wait "$T1" >/dev/null 2>&1 || true          # task-id form resolves
dispatch wait "${WID:0:12}" >/dev/null 2>&1 || true  # prefix form resolves

# worktree mode + bootstrap hook
mkdir -p .claude
cat > .claude/task-worker-bootstrap.sh <<'BOOT'
#!/usr/bin/env bash
touch BOOTSTRAPPED
BOOT
W2=$(dispatch start "$T2" --worktree | awk '/^started /{print $2}')
WT="$TMP/repo-worktrees/$W2"
[ -d "$WT" ] || fail "worktree not created at $WT"
[ -f "$WT/BOOTSTRAPPED" ] || fail "bootstrap hook did not run in worktree"
git branch --list "agent-tasks/$W2" | grep -q "agent-tasks/$W2" || fail "worktree branch missing"
sleep 1
grep -q "CWD: $(cd "$WT" && pwd -P)" ".agent-tasks/runtime/logs/$W2.out" || fail "worktree worker not in worktree"
grep -q "AGENT_TASKS_DIR: $(cd .agent-tasks && pwd -P)" ".agent-tasks/runtime/logs/$W2.out" || fail "worktree worker not pointed at shared queue"
rc=0; dispatch wait "$W2" --timeout 15 || rc=$?
[ "$rc" = "0" ] || fail "worktree worker wait rc=$rc"

# start refuses blocked/claimed tasks without --force
T3=$(tasks create "Blocked" --blocked-by "$T2" | awk '{print $2}')
dispatch start "$T3" 2>/dev/null && fail "start should refuse blocked task" || true

echo "ALL DISPATCH SMOKE TESTS PASSED"
