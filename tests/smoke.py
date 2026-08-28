#!/usr/bin/env python3
"""End-to-end smoke test for scripts/tasks.py.

Stdlib only, cross-platform (no shell). Run: uv run python tests/smoke.py
"""
import json
import os
import re
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


def test_resources(tmp):
    q = Queue(tmp)
    q.run("init")
    with open(os.path.join(tmp, ".agent-tasks", "config.json"), "w") as f:
        json.dump({"lease_minutes": 0.02}, f)  # 1.2s leases for the expiry case

    r1 = q.out("create", "Migration A", "--resources", "db").split()[1]
    r2 = q.out("create", "Migration B", "--resources", "db,browser").split()[1]
    r3 = q.out("create", "E2E run", "--resources", "browser").split()[1]
    r4 = q.out("create", "Plain task").split()[1]

    q.run("claim", r1, "--assignee", "a")
    res = q.run("claim", r2, "--assignee", "b", check=False)
    if res.returncode == 0:
        fail("shared resource tag must refuse second live claim")
    if f"'db' is held by {r1}" not in res.stderr or "(a," not in res.stderr:
        fail(f"refusal should name the holder: {res.stderr}")

    # next skips the conflicted task, takes the free ones
    if q.out("next").split()[0] != r3:
        fail("next should skip the db-conflicted task")
    q.run("claim", r3, "--assignee", "c")
    if q.out("next").split()[0] != r4:
        fail("next should now skip both held tags")

    board = q.out("board")
    if "resources held:" not in board or f"db ← {r1}" not in board:
        fail(f"board should show held resources:\n{board}")
    if q.js("board", "--json")["resources_held"] != {"db": r1, "browser": r3}:
        fail("board json resources_held")

    # releasing one tag isn't enough; releasing both frees the task
    q.run("status", r1, "review", "--agent", "a")
    res = q.run("claim", r2, "--assignee", "b", check=False)
    if res.returncode == 0 or f"'browser' is held by {r3}" not in res.stderr:
        fail("second held tag should still refuse")
    q.run("status", r3, "review", "--agent", "c")
    q.run("claim", r2, "--assignee", "b")

    # an expired lease releases its resources
    r5 = q.out("create", "GPU one", "--resources", "gpu").split()[1]
    r6 = q.out("create", "GPU two", "--resources", "gpu").split()[1]
    q.run("status", r2, "review", "--agent", "b")
    q.run("claim", r5, "--assignee", "a")
    if q.run("claim", r6, "--assignee", "b", check=False).returncode == 0:
        fail("live gpu hold should refuse")
    time.sleep(2.5)
    q.run("claim", r6, "--assignee", "b")  # r5's lease expired -> gpu free


def test_doctor(tmp):
    q = Queue(tmp)
    q.run("init")
    d1 = q.out("create", "Healthy").split()[1]
    if "doctor: clean" not in q.out("doctor"):
        fail("fresh queue should be clean")

    # drift: hand-edit a note's frontmatter status
    d2 = q.out("create", "Drifter").split()[1]
    note = os.path.join(tmp, ".agent-tasks", "tasks", f"{d2}.md")
    with open(note) as f:
        text = f.read()
    with open(note, "w") as f:
        f.write(text.replace("status: open", "status: done", 1))
    res = q.run("doctor", check=False)
    if res.returncode == 0 or "status drift" not in res.stdout or d2 not in res.stdout:
        fail(f"doctor should flag drift nonzero: {res.stdout}")
    res = q.run("doctor", "--fix", check=False)
    if res.returncode == 0:
        fail("doctor --fix still exits nonzero when findings were found")
    with open(note) as f:
        if "status: open" not in f.read():
            fail("--fix should rewrite frontmatter from the index")

    # orphan claim: expired lease, no live worker
    with open(os.path.join(tmp, ".agent-tasks", "config.json"), "w") as f:
        json.dump({"lease_minutes": 0.02}, f)
    q.run("claim", d1, "--assignee", "ghost")
    time.sleep(2.5)
    res = q.run("doctor", check=False)
    if "orphan claim" not in res.stdout or "ghost" not in res.stdout:
        fail(f"doctor should flag orphan claim: {res.stdout}")

    # stray note + missing note
    with open(os.path.join(tmp, ".agent-tasks", "tasks", "TASK-999.md"), "w") as f:
        f.write("stray")
    d3 = q.out("create", "Noteless").split()[1]
    os.remove(os.path.join(tmp, ".agent-tasks", "tasks", f"{d3}.md"))
    out = q.run("doctor", check=False).stdout
    if "TASK-999.md: note file has no index entry" not in out:
        fail(f"stray note not flagged: {out}")
    if f"{d3}: index entry has no note file" not in out:
        fail(f"missing note not flagged: {out}")


def test_mutex(tmp):
    q = Queue(tmp)
    q.run("init")

    if "locked build (a)" not in q.out("lock", "build", "--agent", "a"):
        fail("lock acquire output")
    res = q.run("lock", "build", "--agent", "b", check=False)
    if res.returncode != 4:
        fail(f"second lock should exit 4 BUSY, got {res.returncode}")
    if "BUSY" not in res.stdout or "held by a" not in res.stdout:
        fail(f"BUSY should name the holder: {res.stdout}")
    if q.run("unlock", "build", "--agent", "b", check=False).returncode == 0:
        fail("non-holder unlock should fail")
    q.run("unlock", "build", "--agent", "a")
    q.run("lock", "build", "--agent", "b")
    q.run("unlock", "build", "--agent", "b")
    if "not locked" not in q.out("unlock", "build", "--agent", "b"):
        fail("unlock of unheld mutex should be a friendly no-op")

    # stale steal
    with open(os.path.join(tmp, ".agent-tasks", "config.json"), "w") as f:
        json.dump({"mutex_stale_minutes": 0.02}, f)  # 1.2s
    q.run("lock", "gpu", "--agent", "a")
    res = q.run("lock", "gpu", "--agent", "b", check=False)
    if res.returncode != 4:
        fail("fresh lock should still be BUSY")
    time.sleep(2.5)
    out = q.out("lock", "gpu", "--agent", "b")
    if "stole stale lock from a" not in out:
        fail(f"stale steal not reported: {out}")
    q.run("unlock", "gpu", "--agent", "b")

    if q.run("lock", "../evil", "--agent", "a", check=False).returncode == 0:
        fail("path-traversal mutex name should be rejected")
    # mutexes are machine-local: they live under self-gitignored runtime/
    if not os.path.isfile(os.path.join(tmp, ".agent-tasks", "runtime", ".gitignore")):
        fail("runtime self-gitignore missing")


def test_concurrent_logs(tmp):
    q = Queue(tmp)
    q.run("init")
    t = q.out("create", "Log target").split()[1]
    procs = [subprocess.Popen([PY, CLI, "log", t, f"entry-{i}", "--agent", f"w{i}"],
                              cwd=tmp, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
             for i in range(10)]
    if any(p.wait() != 0 for p in procs):
        fail("concurrent log invocation failed")
    lines = [l for l in q.note_text(t).splitlines() if "entry-" in l]
    if len(lines) != 10:
        fail(f"expected 10 log entries, got {len(lines)}")
    for line in lines:
        if not re.match(r"^- \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \[w\d\] entry-\d$", line):
            fail(f"torn/interleaved log line: {line!r}")


def main():
    for test in (test_lifecycle, test_leases, test_tiers, test_resources,
                 test_doctor, test_mutex, test_concurrent_logs):
        tmp = tempfile.mkdtemp(prefix="agent-tasks-smoke-")
        try:
            test(tmp)
            print(f"ok: {test.__name__}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
