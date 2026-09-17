#!/usr/bin/env python3
"""End-to-end smoke test for scripts/tasks.py.

Stdlib only, cross-platform (no shell). Run: uv run python tests/smoke.py
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

SCRIPTS_DIR = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))

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
        if f"blocked <- {blocker}" not in row:
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
    if "resources held:" not in board or f"db <- {r1}" not in board:
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


def test_utf8_discipline(tmp):
    """Windows runs cp1252 by default: sources must be pure ASCII, and every
    text-mode open() must pin encoding="utf-8" (enforced by AST lint so a new
    call can't slip in unpinned)."""
    for fname in ("tasks.py", "dispatch.py", "procs.py"):
        path = os.path.join(SCRIPTS_DIR, fname)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if not src.isascii():
            bad = sorted({c for c in src if not c.isascii()})
            fail(f"{fname} contains non-ASCII characters: {bad}")
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "open":
                pass  # builtin open
            elif (isinstance(fn, ast.Attribute) and fn.attr == "fdopen"):
                pass  # os.fdopen
            else:
                continue  # os.open etc. are raw-fd, no encoding concept
            mode = "r"
            args = node.args
            if len(args) >= 2 and isinstance(args[1], ast.Constant):
                mode = args[1].value
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            if isinstance(mode, str) and "b" in mode:
                continue
            if not any(kw.arg == "encoding" for kw in node.keywords):
                fail(f"{fname}:{node.lineno}: text-mode open() without encoding=")

    # stdio survives a cp1252 console (the redirected-output case)
    q = Queue(tmp)
    env = {**os.environ, "PYTHONIOENCODING": "cp1252:strict"}
    def run_cp1252(*args):
        res = subprocess.run([PY, CLI, *args], cwd=tmp, env=env,
                             capture_output=True, text=True)
        if res.returncode not in (0, 1):
            fail(f"cp1252 stdio crash: tasks {' '.join(args)}\n{res.stderr}")
        return res
    run_cp1252("init")
    run_cp1252("create", "First", "--priority", "high")
    run_cp1252("create", "Second", "--blocked-by", "TASK-001")
    run_cp1252("claim", "TASK-001", "--assignee", "w")
    run_cp1252("status", "TASK-001", "review", "--agent", "w")  # prints old -> new
    run_cp1252("list")   # prints blocked <- markers
    run_cp1252("board")
    run_cp1252("show", "TASK-002")

    # init is re-runnable: recreate whatever went missing, overwrite nothing
    readme = os.path.join(tmp, ".agent-tasks", "README.md")
    os.remove(readme)
    out = q.out("init")
    if "README.md" not in out or not os.path.isfile(readme):
        fail(f"init should recreate a missing README.md: {out}")
    index_before = q.note_text("TASK-001")  # notes untouched by re-init
    q.run("init")
    if q.note_text("TASK-001") != index_before:
        fail("re-init must never touch existing files")
    if len(q.js("list", "--all", "--json")) != 2:
        fail("re-init must not reset the index")


def test_note_append(tmp):
    q = Queue(tmp)
    q.run("init")
    t = q.out("create", "Rich notes").split()[1]

    # two concurrent appends both land intact, serialized by the queue lock
    blocks = ["first finding:\nline a\nline b", "second finding:\nline c"]
    procs = []
    for i, block in enumerate(blocks):
        p = subprocess.Popen([PY, CLI, "note", t, "--append", "--agent", f"n{i}"],
                             cwd=tmp, stdin=subprocess.PIPE, text=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        procs.append((p, block))
    errs = [p.communicate(input=block)[1] for p, block in procs]
    if any(p.returncode != 0 for p, _ in procs):
        fail(f"concurrent note --append failed: {errs}")
    note = q.note_text(t)
    for block in blocks:
        if block.replace("\\n", "\n") not in note:
            pass
    if "line a\nline b" not in note or "line c" not in note:
        fail(f"appended blocks not intact:\n{note}")
    if note.count("status: open") != 1:
        fail("frontmatter must be untouched")
    if not (note.index("line c") < note.index("## Work log")):
        fail("appends must land in Notes, before the Work log")
    if note.count("notes appended") != 2:
        fail("each append should be recorded in the work log")

    # --file variant + stamping
    blob = os.path.join(tmp, "blob.md")
    with open(blob, "w", encoding="utf-8") as f:
        f.write("from a file")
    q.run("note", t, "--append", "--file", blob, "--agent", "filer")
    note = q.note_text(t)
    if "from a file" not in note or "**[filer @" not in note:
        fail("--file append missing or unstamped")

    # empty input dies
    p = subprocess.Popen([PY, CLI, "note", t, "--append", "--agent", "x"],
                         cwd=tmp, stdin=subprocess.PIPE, text=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.communicate(input="   ")
    if p.returncode == 0:
        fail("empty append should fail")


def test_config_overlay(tmp):
    q = Queue(tmp)
    out = q.out("init")
    if "config.local.json" not in out:
        fail("init should gitignore the local overlay")
    gi = os.path.join(tmp, ".agent-tasks", ".gitignore")
    with open(gi, encoding="utf-8") as f:
        if f.read().count("config.local.json") != 1:
            fail("gitignore entry missing")
    q.run("init")  # idempotent: no duplicate line
    with open(gi, encoding="utf-8") as f:
        if f.read().count("config.local.json") != 1:
            fail("re-init duplicated the gitignore entry")

    # local overlay merges key-by-key over project config
    with open(os.path.join(tmp, ".agent-tasks", "config.json"), "w",
              encoding="utf-8") as f:
        json.dump({"lease_minutes": 90, "model_tiers": ["haiku", "opus"]}, f)
    with open(os.path.join(tmp, ".agent-tasks", "config.local.json"), "w",
              encoding="utf-8") as f:
        json.dump({"lease_minutes": 0.02}, f)  # local wins this key only
    t = q.out("create", "Overlay probe").split()[1]
    q.run("claim", t, "--assignee", "a")
    time.sleep(2.5)
    if not q.js("show", t, "--json")["lease_expired"]:
        fail("local overlay lease_minutes should win over project config")
    # untouched key still comes from project config
    res = q.run("claim", t, "--tier", "sonnet", "--assignee", "b", check=False)
    if res.returncode == 0 or "['haiku', 'opus']" not in res.stderr:
        fail(f"project-config model_tiers should survive the overlay: {res.stderr}")



def test_journal(tmp):
    """Every mutation lands in the journal, and `since` answers "what changed
    after this cursor" without re-reading the board or any note."""
    q = Queue(tmp)
    q.run("init")
    t1 = q.out("create", "Journal one", "--agent", "planner").split()[1]
    mark = q.js("since", "--json")["cursor"]
    t2 = q.out("create", "Journal two", "--agent", "planner").split()[1]
    q.run("claim", t1, "--assignee", "worker-a")
    q.run("log", t1, "poking at middleware.ts", "--agent", "worker-a")
    q.run("status", t1, "review", "--agent", "worker-a")

    after = q.js("since", "--cursor", str(mark), "--json")
    kinds = [e["kind"] for e in after["events"]]
    if kinds != ["create", "claim", "log", "status"]:
        fail(f"journal kinds after cursor: {kinds}")
    if [e["seq"] for e in after["events"]] != sorted(e["seq"] for e in after["events"]):
        fail("journal is not ordered by seq")
    if after["cursor"] <= mark:
        fail("cursor did not advance")
    status = after["events"][-1]
    if status["from"] != "in_progress" or status["to"] != "review":
        fail(f"status event lost its transition: {status}")

    # actionable = decisions only; narration and bookkeeping stay out
    act = q.js("since", "--cursor", str(mark), "--actionable", "--json")["events"]
    if [e["kind"] for e in act] != ["create", "status"]:
        fail(f"--actionable let narration through: {[e['kind'] for e in act]}")
    if any(e["task"] != t2 for e in q.js("since", "--task", t2, "--json")["events"]):
        fail("--task filter leaked other tasks")
    if "cursor:" not in q.out("board"):
        fail("board should print the cursor a waker can resume from")


def _await(tmp, *args, agent="planner"):
    """Start a watcher; the caller drives the queue and then joins it."""
    return subprocess.Popen(
        [PY, CLI, "await", "--agent", agent, "--poll", "0.2",
         "--debounce", "0.4", "--json", *args],
        cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_await_wakes_only_on_decisions(tmp):
    q = Queue(tmp)
    q.run("init")
    t1 = q.out("create", "Await one", "--agent", "planner").split()[1]
    t2 = q.out("create", "Await two", "--agent", "planner").split()[1]
    q.run("claim", t1, "--assignee", "worker-a")
    q.run("claim", t2, "--assignee", "worker-b")

    p = _await(tmp, "--timeout", "30s")
    time.sleep(1.2)
    # narration from a worker, and the planner's own bookkeeping: neither is a
    # decision, so neither may wake the watcher
    q.run("log", t1, "still reading middleware.ts", "--agent", "worker-a")
    q.run("create", "Planner's own follow-up", "--agent", "planner")
    q.run("heartbeat", t2, "--assignee", "worker-b")  # leases are not news
    time.sleep(1.2)
    if p.poll() is not None:
        fail(f"watcher woke on narration/self-writes: {p.stdout.read()}")

    # two finishes in quick succession: one wake carrying both
    q.run("status", t1, "review", "--agent", "worker-a")
    time.sleep(0.2)
    q.run("status", t2, "review", "--agent", "worker-b")
    out, err = p.communicate(timeout=30)
    if p.returncode != 0:
        fail(f"watcher should exit 0 on a wake, got {p.returncode}: {out}{err}")
    wake = json.loads(out[out.index("{"):])
    if wake["wake"] != "2 review":
        fail(f"debounce should coalesce the burst into one wake: {wake['wake']}")
    if {h["task"] for h in wake["hits"]} != {t1, t2}:
        fail(f"wake lost a task: {wake['hits']}")
    # the digest must hand back a cursor that still replays the delta it woke
    # for -- not one already advanced past it
    replay = q.js("since", "--cursor", str(wake["from_cursor"]), "--actionable",
                  "--json")["events"]
    if {e["task"] for e in replay if e.get("to") == "review"} != {t1, t2}:
        fail(f"from_cursor does not replay the wake: {replay}")


    # the wake is counted where a human (or the next planner) can see it
    sup = q.js("supervisor", "--json")
    if sup["wakes"] != 1 or sup["last_wake"]["reason"] != "2 review":
        fail(f"wake not recorded on the lease: {sup}")

    # a drain trigger is a standing condition, not an event: it fires once
    q.run("done", t1, "--agent", "planner")
    q.run("done", t2, "--agent", "planner")
    res = q.run("await", "--agent", "planner", "--for", "drain", "--poll", "0.2",
                "--debounce", "0.6", "--timeout", "10s", "--json", check=False)
    drain = json.loads(res.stdout[res.stdout.index("{"):])
    if res.returncode != 0 or drain["wake"] != "1 drain":
        fail(f"drain should fire exactly once: rc={res.returncode} {res.stdout}")


def test_await_quiet_and_blocked(tmp):
    q = Queue(tmp)
    q.run("init")
    t = q.out("create", "Quiet probe", "--agent", "planner").split()[1]
    q.run("claim", t, "--assignee", "worker-a")

    res = q.run("await", "--agent", "planner", "--poll", "0.2",
                "--timeout", "1s", check=False)
    if res.returncode != 2:
        fail(f"a quiet watch must exit 2, got {res.returncode}: {res.stdout}")
    if "handoff" not in res.stdout:
        fail("a quiet timeout should point at the handoff, not at more waiting")

    # a worker's question (free-text blocker) is a decision for the planner
    p = _await(tmp, "--timeout", "30s")
    time.sleep(1.0)
    q.run("block", t, "need the staging API key", "--agent", "worker-a")
    out, err = p.communicate(timeout=30)
    if p.returncode != 0:
        fail(f"blocked should wake the planner: rc={p.returncode} {out}{err}")
    wake = json.loads(out[out.index("{"):])
    if wake["wake"] != "1 blocked" or "staging API key" not in str(wake["hits"]):
        fail(f"blocked wake lost the question: {wake}")


def test_supervisor_lease(tmp):
    q = Queue(tmp)
    q.run("init")
    if "none" not in q.out("supervisor"):
        fail("a fresh queue has no supervisor")

    q.run("supervisor", "claim", "--agent", "planner-a")
    busy = q.run("supervisor", "claim", "--agent", "planner-b", check=False)
    if busy.returncode != 4 or "BUSY" not in busy.stdout:
        fail(f"a second planner must not quietly share the lease: {busy.stdout}")
    if q.run("supervisor", "claim", "--agent", "planner-b", "--takeover",
             check=False).returncode != 0:
        fail("--takeover should supersede a fresh lease")

    # yesterday's watcher, still running, when a new session starts today
    p = _await(tmp, "--timeout", "30s", agent="planner-b")
    time.sleep(1.0)
    q.run("supervisor", "claim", "--agent", "planner-b")  # the new session
    out, err = p.communicate(timeout=30)
    if p.returncode != 4:
        fail(f"a superseded watcher must exit 4, got {p.returncode}: {out}{err}")
    if "superseded" not in out or "re-arm" not in out:
        fail(f"the superseded watcher must say so plainly: {out}")

    # retirement silences watchers too, and survives as queue state
    p = _await(tmp, "--timeout", "30s", agent="planner-b")
    time.sleep(1.0)
    q.run("supervisor", "retire", "--agent", "planner-b", "--note", "done for today")
    out, _ = p.communicate(timeout=30)
    if p.returncode != 4 or "retired" not in out:
        fail(f"a retired watcher must exit 4: rc={p.returncode} {out}")
    if "retired by planner-b" not in q.out("board"):
        fail("the board should say nobody is watching")

    # an abandoned lease is an integrity finding, not a silent squatter
    with open(os.path.join(tmp, ".agent-tasks", "config.json"), "w",
              encoding="utf-8") as f:
        json.dump({"supervisor_ttl_minutes": 0}, f)
    q.run("supervisor", "claim", "--agent", "planner-c")
    doc = q.run("doctor", check=False)
    if "supervisor" not in doc.stdout or doc.returncode != 1:
        fail(f"doctor should flag an abandoned supervisor lease: {doc.stdout}")


def test_handoff(tmp):
    q = Queue(tmp)
    q.run("init")
    if "no handoff recorded" not in q.out("handoff"):
        fail("a fresh queue should say so rather than error")

    t1 = q.out("create", "Finished work", "--agent", "planner").split()[1]
    t2 = q.out("create", "Stuck work", "--agent", "planner").split()[1]
    t3 = q.out("create", "Next up", "--priority", "high", "--agent", "planner").split()[1]
    q.run("claim", t1, "--assignee", "worker-a")
    q.run("status", t1, "review", "--agent", "worker-a")
    q.run("block", t2, "need the staging API key", "--agent", "worker-b")

    q.run("supervisor", "claim", "--agent", "planner")
    out = q.out("handoff", "--write", "--retire", "--agent", "planner",
                "--note", "worker-a's fix is unverified; check the redirect by hand")
    if "handoff written" not in out or "retired" not in out:
        fail(f"handoff --write --retire should report both: {out}")

    doc = q.js("handoff", "--json")
    if not doc["exists"]:
        fail("handoff --json should find the document it just wrote")
    text = doc["text"]
    for needle in (t1, "review", t2, "staging API key", t3,
                   "worker-a's fix is unverified", "tasks since --cursor",
                   "Pick this up"):
        if needle not in text:
            fail(f"handoff document is missing {needle!r}")
    if f"--cursor {doc['cursor']}" not in text:
        fail("the handoff must carry a cursor the next planner can resume from")

    if q.out("handoff", "--show") != text:
        fail("--show and the default must print the same document")
    if "handoff on file" not in q.out("board"):
        fail("the board should tell a fresh session a handoff is waiting")

    # retired means retired: a watcher armed afterwards exits instead of lurking
    res = q.run("await", "--agent", "planner", "--poll", "0.2", "--timeout", "5s",
                "--no-supervisor", check=False)
    if res.returncode != 2:
        fail("--no-supervisor should watch without a lease")
    p = _await(tmp, "--timeout", "10s")
    out, _ = p.communicate(timeout=20)
    if p.returncode not in (0, 2, 4):
        fail(f"unexpected watcher exit {p.returncode}: {out}")


def test_remote(tmp):
    """`remote` is an opaque handle for the project's seed/sync hooks: set at
    create or later, mirrored into the note frontmatter (display-only, like
    status), cleared with '-', and invisible to the rest of the queue."""
    q = Queue(tmp)
    q.run("init")
    t = q.out("create", "Linked", "--remote", "repos.x.llpm.tasks.TASK-012").split()[1]
    if q.js("show", t, "--json")["remote"] != "repos.x.llpm.tasks.TASK-012":
        fail("create --remote not in index")
    fm = q.note_text(t).split("---")[1]
    if "remote: repos.x.llpm.tasks.TASK-012" not in fm:
        fail("create --remote not mirrored into frontmatter")
    if q.out("remote", t).strip() != "repos.x.llpm.tasks.TASK-012":
        fail("remote get")
    if q.js("remote", t, "--json") != {"id": t, "remote": "repos.x.llpm.tasks.TASK-012"}:
        fail("remote get --json")
    q.run("remote", t, "https://issues.example/42", "--agent", "planner")
    if q.js("show", t, "--json")["remote"] != "https://issues.example/42":
        fail("remote set")
    fm = q.note_text(t).split("---")[1]
    if "remote: https://issues.example/42" not in fm or "TASK-012" in fm:
        fail(f"remote set should replace the frontmatter line: {fm}")
    ev = q.js("since", "--task", t, "--json")["events"]
    if not any(e["kind"] == "remote" and e.get("remote") == "https://issues.example/42" for e in ev):
        fail("remote change should be journaled")
    q.run("remote", t, "-")
    if "remote" in q.js("show", t, "--json") or "remote:" in q.note_text(t).split("---")[1]:
        fail("remote clear")
    if q.run("remote", t, check=False).returncode != 1:
        fail("remote get on a bare task should exit 1")
    t2 = q.out("create", "Plain").split()[1]
    if "remote" in q.js("show", t2, "--json") or "remote:" in q.note_text(t2).split("---")[1]:
        fail("a task without --remote must carry no remote field at all")
    if q.run("doctor", check=False).returncode != 0:
        fail("remote frontmatter must not trip doctor")


def main():
    for test in (test_lifecycle, test_leases, test_tiers, test_resources,
                 test_doctor, test_mutex, test_concurrent_logs,
                 test_utf8_discipline, test_note_append, test_config_overlay,
                 test_journal, test_await_wakes_only_on_decisions,
                 test_await_quiet_and_blocked, test_supervisor_lease,
                 test_handoff, test_remote):
        tmp = tempfile.mkdtemp(prefix="agent-tasks-smoke-")
        try:
            test(tmp)
            print(f"ok: {test.__name__}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
