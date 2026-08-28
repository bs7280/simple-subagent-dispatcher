#!/usr/bin/env python3
"""End-to-end smoke test for scripts/tasks.py.

Stdlib only, cross-platform (no shell). Run: uv run python tests/smoke.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.abspath(os.path.join(HERE, os.pardir, "scripts", "tasks.py"))
PY = sys.executable


def fail(msg):
    raise SystemExit(f"FAIL: {msg}")


class Queue:
    """Runs the tasks CLI against one temp directory."""

    def __init__(self, tmp):
        self.cwd = tmp

    def run(self, *args, check=True):
        res = subprocess.run([PY, CLI, *args], cwd=self.cwd,
                             capture_output=True, text=True)
        if check and res.returncode != 0:
            fail(f"tasks {' '.join(args)} -> rc={res.returncode}\n{res.stderr}")
        return res

    def out(self, *args):
        return self.run(*args).stdout

    def js(self, *args):
        return json.loads(self.out(*args))

    def note_text(self, tid):
        with open(os.path.join(self.cwd, ".agent-tasks", "tasks", f"{tid}.md")) as f:
            return f.read()


def test_lifecycle(tmp):
    q = Queue(tmp)
    if "initialized" not in q.out("init"):
        fail("init")
    if "already initialized" not in q.out("init"):
        fail("re-init should be a no-op")

    t1 = q.out("create", "First task", "--body", "Do the thing",
               "--priority", "high", "--tags", "auth,backend").split()[1]
    t2 = q.out("create", "Second task", "--blocked-by", t1).split()[1]
    t3 = q.out("create", "Third task", "--blocked-by", "waiting on API key",
               "--priority", "low").split()[1]
    if t1 != "TASK-001":
        fail(f"expected TASK-001, got {t1}")
    if not os.path.isfile(os.path.join(tmp, ".agent-tasks", "tasks", f"{t1}.md")):
        fail("note file missing")

    listing = q.out("list")
    for tid, blocker in ((t2, t1), (t3, "waiting on API key")):
        row = next(l for l in listing.splitlines() if l.startswith(tid))
        if f"blocked ← {blocker}" not in row:
            fail(f"{tid} not shown blocked on {blocker}")

    if q.out("next").split()[0] != t1:
        fail(f"next should pick {t1}")

    # claim race: exactly one of two concurrent claims wins
    procs = [subprocess.Popen([PY, CLI, "claim", t1, "--assignee", who],
                              cwd=tmp, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
             for who in ("racer-a", "racer-b")]
    wins = sum(p.wait() == 0 for p in procs)
    if wins != 1:
        fail(f"claim race: expected exactly 1 winner, got {wins}")

    t = q.js("show", t1, "--json")
    if t["status"] != "in_progress" or t["assignee"] not in ("racer-a", "racer-b"):
        fail(f"claim state: {t}")
    if not t.get("lease_until") or not t.get("claimed_at"):
        fail("claim did not stamp lease_until/claimed_at")
    note = q.note_text(t1)
    if "status: in_progress" not in note:
        fail("frontmatter not synced")
    if "claimed" not in note:
        fail("work log missing claim entry")

    res = q.run("claim", t1, "--assignee", "thief", check=False)
    if res.returncode == 0:
        fail("re-claim of an active lease should fail")
    if "lease until" not in res.stderr:
        fail(f"active-lease refusal should mention the lease: {res.stderr}")

    q.run("log", t1, "root cause found", "--agent", "racer-a")
    if "root cause found" not in q.note_text(t1):
        fail("log entry missing")
    q.run("status", t1, "review", "--agent", "racer-a")
    if q.js("show", t1, "--json").get("lease_until"):
        fail("lease should be dropped on leaving in_progress")
    q.run("done", t1, "--summary", "verified by smoke test", "--agent", "planner")
    if "verified by smoke test" not in q.note_text(t1):
        fail("done summary missing")

    # task-id blocker auto-resolves; free-text one does not
    if q.out("next").split()[0] != t2:
        fail(f"{t2} should be unblocked after {t1} done")
    if q.run("claim", t3, "--assignee", "w", check=False).returncode == 0:
        fail(f"{t3} should still be blocked by free text")
    q.run("unblock", t3, "waiting on API key")
    q.run("claim", t3, "--assignee", "w")

    nx = q.out("next", "--claim", "--assignee", "worker-x")
    if f"claimed {t2} (worker-x)" not in nx:
        fail(f"next --claim output: {nx}")

    # loose id forms + note path
    q.run("show", "2")
    q.run("show", "task-002")
    note_path = q.out("note", "3").strip()
    expect = os.path.join(os.path.realpath(tmp), ".agent-tasks", "tasks", f"{t3}.md")
    if os.path.realpath(note_path) != expect:
        fail(f"note path: {note_path} != {expect}")

    if "done: 1, cancelled: 0" not in q.out("board"):
        fail("board counts")
    if q.js("board", "--json")["counts"]["in_progress"] != 2:
        fail("board json")

    for tid in (t2, t3):
        q.run("done", tid, "--agent", "planner")
    if q.run("next", check=False).returncode != 1:
        fail("next should exit 1 when nothing ready")

    ts = q.js("list", "--all", "--json")
    if len(ts) != 3 or not all("unresolved_blockers" in t for t in ts):
        fail("list json")

    if os.path.exists(os.path.join(tmp, ".agent-tasks", ".lock")):
        fail("lock file left behind")


def test_leases(tmp):
    q = Queue(tmp)
    q.run("init")
    # 0.02 min = 1.2s lease
    with open(os.path.join(tmp, ".agent-tasks", "config.json"), "w") as f:
        json.dump({"lease_minutes": 0.02}, f)

    l1 = q.out("create", "Leased task").split()[1]
    q.run("claim", l1, "--assignee", "agent-a")
    first_lease = q.js("show", l1, "--json")["lease_until"]

    # active lease: refuse claim, refuse foreign heartbeat, allow own heartbeat
    if q.run("claim", l1, "--assignee", "agent-b", check=False).returncode == 0:
        fail("active lease should refuse claim")
    res = q.run("heartbeat", l1, "--assignee", "agent-b", check=False)
    if res.returncode == 0 or "assigned to agent-a" not in res.stderr:
        fail(f"foreign heartbeat should fail naming the assignee: {res.stderr}")
    time.sleep(1.1)
    q.run("heartbeat", l1, "--assignee", "agent-a")
    if q.js("show", l1, "--json")["lease_until"] < first_lease:
        fail("heartbeat did not extend the lease")

    time.sleep(2.5)  # let the lease expire
    t = q.js("show", l1, "--json")
    if not t["lease_expired"]:
        fail("lease should read expired")
    row = next(l for l in q.out("list").splitlines() if l.startswith(l1))
    if "[lease expired]" not in row:
        fail("list should flag expired lease")
    board = q.out("board")
    if "expired lease (claimable):" not in board or l1 not in board:
        fail(f"board should show expired bucket:\n{board}")
    if q.js("board", "--json")["expired_lease"] != [l1]:
        fail("board json expired bucket")

    # steal via next --claim, with the steal logged
    nx = q.out("next", "--claim", "--assignee", "agent-b")
    if f"claimed {l1} (agent-b)" not in nx:
        fail(f"next --claim should steal the expired lease: {nx}")
    note = q.note_text(l1)
    if "stole expired claim (was agent-a" not in note:
        fail(f"steal not logged:\n{note}")

    # direct-claim steal path
    l2 = q.out("create", "Second leased").split()[1]
    q.run("claim", l2, "--assignee", "agent-a")
    time.sleep(2.5)
    q.run("claim", l2, "--assignee", "agent-b")
    if q.js("show", l2, "--json")["assignee"] != "agent-b":
        fail("direct claim should steal expired lease")

    # heartbeat on a non-in_progress task fails
    q.run("status", l2, "review", "--agent", "agent-b")
    if q.run("heartbeat", l2, "--assignee", "agent-b", check=False).returncode == 0:
        fail("heartbeat on non-in_progress should fail")


def test_tiers(tmp):
    q = Queue(tmp)
    q.run("init")
    a = q.out("create", "Opus job", "--model", "opus").split()[1]
    b = q.out("create", "Haiku job", "--model", "haiku").split()[1]
    c = q.out("create", "Any-model job").split()[1]
    d = q.out("create", "Unknown-model job", "--model", "fable").split()[1]

    # haiku tier: skips opus and unknown (with a why), picks the haiku task
    res = q.run("next", "--tier", "haiku")
    if res.stdout.split()[0] != b:
        fail(f"--tier haiku should pick {b}: {res.stdout}")
    if "excluded from --tier haiku" not in res.stderr or d not in res.stderr:
        fail(f"unknown-model exclusion not explained: {res.stderr}")

    q.run("next", "--claim", "--tier", "haiku", "--assignee", "h")
    if q.js("show", b, "--json")["assignee"] != "h":
        fail("next --claim --tier claimed the wrong task")
    q.run("next", "--claim", "--tier", "haiku", "--assignee", "h")  # -> c (unset model)
    if q.js("show", c, "--json")["assignee"] != "h":
        fail("model-unset task should be claimable at any tier")
    if q.run("next", "--tier", "haiku", check=False).returncode != 1:
        fail("haiku tier must never see the opus task")

    # direct claim honors --tier; no tier means no gate
    res = q.run("claim", a, "--tier", "haiku", "--assignee", "h", check=False)
    if res.returncode == 0 or "outside your tier" not in res.stderr:
        fail(f"claim --tier should refuse opus task: {res.stderr}")
    q.run("claim", a, "--tier", "opus", "--assignee", "o")
    if q.run("claim", d, "--tier", "opus", "--assignee", "o", check=False).returncode == 0:
        fail("unknown model must be excluded from tier claims")
    q.run("claim", d, "--assignee", "o")  # tierless claim still fine
    if q.run("claim", c, "--tier", "turbo9000", "--assignee", "x", check=False).returncode == 0:
        fail("bogus tier name should be rejected")


def main():
    for test in (test_lifecycle, test_leases, test_tiers):
        tmp = tempfile.mkdtemp(prefix="agent-tasks-smoke-")
        try:
            test(tmp)
            print(f"ok: {test.__name__}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
