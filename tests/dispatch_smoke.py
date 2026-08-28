#!/usr/bin/env python3
"""Dispatcher smoke test: a stub claude (a Python script) exercises spawn/
list/wait/NEEDS-RESUME/resume/stop/worktree/bootstrap without real sessions.

Stdlib only, cross-platform. Run: uv run python tests/dispatch_smoke.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
TASKS = os.path.join(SCRIPTS, "tasks.py")
DISPATCH = os.path.join(SCRIPTS, "dispatch.py")
PY = sys.executable

FAKE_CLAUDE = """\
import os, sys, time
print("FAKE-CLAUDE ARGS:", " ".join(sys.argv[1:]))
print("FAKE-CLAUDE AGENT_TASKS_DIR:", os.environ.get("AGENT_TASKS_DIR", "unset"))
print("FAKE-CLAUDE AGENT:", os.environ.get("AGENT_TASKS_AGENT", "unset"))
print("FAKE-CLAUDE CWD:", os.path.realpath(os.getcwd()))
print("FAKE-CLAUDE CLAUDECODE:", os.environ.get("CLAUDECODE", "unset"))
sys.stdout.flush()
time.sleep(3)
"""


def fail(msg):
    raise SystemExit(f"FAIL: {msg}")


def run_all(tmp):
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    # CLAUDECODE set on purpose: the dispatcher must strip it from workers
    env = {**os.environ, "CLAUDECODE": "1"}
    env.pop("AGENT_TASKS_DIR", None)

    def sh(cmd, check=True, cwd=repo):
        res = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
        if check and res.returncode != 0:
            fail(f"{' '.join(map(str, cmd))} -> rc={res.returncode}\n{res.stderr}")
        return res

    def tasksc(*a, **kw):
        return sh([PY, TASKS, *a], **kw)

    def disp(*a, **kw):
        return sh([PY, DISPATCH, *a], **kw)

    def started_id(res):
        for line in res.stdout.splitlines():
            if line.startswith("started "):
                return line.split()[1]
        fail(f"no worker id in start output:\n{res.stdout}")

    def list_row(wid):
        return next(l for l in disp("list").stdout.splitlines() if wid in l)

    def log_text(wid):
        path = os.path.join(repo, ".agent-tasks", "runtime", "logs", f"{wid}.out")
        with open(path) as f:
            return f.read()

    sh(["git", "init", "-q"])
    sh(["git", "commit", "-q", "--allow-empty", "-m", "init"])
    tasksc("init")
    queue_root = os.path.realpath(os.path.join(repo, ".agent-tasks"))

    t1 = tasksc("create", "Dispatched task", "--body", "do it").stdout.split()[1]
    t2 = tasksc("create", "Worktree task").stdout.split()[1]

    # composed prompt: default uv runner, death-mode ban, claim step
    out = disp("prompt", t1).stdout
    if "uv run python" not in out:
        fail("default runner missing from composed prompt")
    if "NEVER launch a long-running command" not in out:
        fail("prompt missing death-mode ban")
    if f"claim {t1}" not in out:
        fail("prompt missing claim step")

    # stub claude; runner pinned to this interpreter so the test needs no uv
    stub = os.path.join(tmp, "fake_claude.py")
    with open(stub, "w") as f:
        f.write(FAKE_CLAUDE)
    with open(os.path.join(repo, ".agent-tasks", "config.json"), "w") as f:
        json.dump({"claude_bin": [PY, stub], "model": "haiku", "runner": [PY]}, f)

    # ---- start in-place (default) ----
    wid = started_id(disp("start", t1))
    if "running" not in list_row(wid):
        fail("worker not listed running")
    with open(os.path.join(repo, ".agent-tasks", "tasks", f"{t1}.md")) as f:
        if f"dispatched worker {wid}" not in f.read():
            fail("dispatch not logged to task note")
    if sh(["git", "check-ignore", "-q", ".agent-tasks/runtime/workers.json"],
          check=False).returncode != 0:
        fail("runtime/ not self-gitignored")

    time.sleep(1)
    log = log_text(wid)
    for needle, msg in [
        ("--permission-mode acceptEdits", "permission-mode not passed"),
        ("--model haiku", "config model not passed"),
        ("tasks.py:*)", "queue CLI not allowlisted"),
        ("FAKE-CLAUDE AGENT_TASKS_DIR: " + queue_root, "AGENT_TASKS_DIR not exported"),
        ("FAKE-CLAUDE CWD: " + os.path.realpath(repo), "in-place worker not in repo checkout"),
        ("FAKE-CLAUDE CLAUDECODE: unset", "parent CLAUDE_* env leaked into worker"),
    ]:
        if needle not in log:
            fail(f"{msg}\n--- log:\n{log}")

    # ---- worker dies mid-task -> NEEDS-RESUME -> resume -> stop ----
    tasksc("claim", t1, "--assignee", wid)
    res = disp("wait", wid, check=False)
    if res.returncode != 3:
        fail(f"wait should exit 3 for died-mid-task, got {res.returncode}")
    if "NEEDS-RESUME" not in list_row(wid):
        fail("NEEDS-RESUME not flagged")

    disp("resume", wid)
    row = list_row(wid)
    if "running" not in row:
        fail("resumed worker not running")
    if "NEEDS-RESUME" in row:
        fail("running worker still flagged NEEDS-RESUME")
    time.sleep(1)
    w = next(x for x in json.loads(disp("list", "--json").stdout) if x["id"] == wid)
    if w["resumes"] != 1:
        fail(f"resume count: {w['resumes']}")
    if f"--resume {w['session_id']}" not in log_text(wid):
        fail("resume used wrong session id")

    if f"stopped {wid}" not in disp("stop", wid).stdout:
        fail("stop output")
    time.sleep(1)
    if "exited" not in list_row(wid):
        fail("stopped worker still alive")

    # worker resolution by task id and by unique prefix
    disp("wait", t1, check=False)
    disp("wait", wid[:12], check=False)

    # ---- task-pinned model beats config model ----
    tm = tasksc("create", "Sonnet-pinned task", "--model", "sonnet").stdout.split()[1]
    wm = started_id(disp("start", tm))
    time.sleep(1)
    if "--model sonnet" not in log_text(wm):
        fail("task-pinned model not in spawned command line")
    if "model sonnet" not in open(os.path.join(
            repo, ".agent-tasks", "tasks", f"{tm}.md")).read():
        fail("dispatched model not logged to task note")
    disp("wait", wm, check=False)

    # ---- worktree mode + python bootstrap hook ----
    os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
    with open(os.path.join(repo, ".claude", "task-worker-bootstrap.py"), "w") as f:
        f.write("open('BOOTSTRAPPED', 'w').close()\n")
    w2 = started_id(disp("start", t2, "--worktree"))
    wt = os.path.join(f"{repo}-worktrees", w2)
    if not os.path.isdir(wt):
        fail(f"worktree not created at {wt}")
    if not os.path.isfile(os.path.join(wt, "BOOTSTRAPPED")):
        fail("python bootstrap hook did not run in worktree")
    if f"agent-tasks/{w2}" not in sh(["git", "branch", "--list",
                                      f"agent-tasks/{w2}"]).stdout:
        fail("worktree branch missing")
    time.sleep(1)
    log2 = log_text(w2)
    if "FAKE-CLAUDE CWD: " + os.path.realpath(wt) not in log2:
        fail("worktree worker not in worktree")
    if "FAKE-CLAUDE AGENT_TASKS_DIR: " + queue_root not in log2:
        fail("worktree worker not pointed at shared queue")
    res = disp("wait", w2, "--timeout", "15", check=False)
    if res.returncode != 0:
        fail(f"worktree worker wait rc={res.returncode}")

    # ---- start refuses blocked tasks without --force ----
    t3 = tasksc("create", "Blocked", "--blocked-by", t2).stdout.split()[1]
    if disp("start", t3, check=False).returncode == 0:
        fail("start should refuse a blocked task")

    print("ALL DISPATCH SMOKE TESTS PASSED")


def main():
    tmp = tempfile.mkdtemp(prefix="agent-tasks-dispatch-")
    try:
        run_all(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
