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
mode = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "plain"
print("FAKE-CLAUDE ARGS:", " ".join(sys.argv[1:]))
print("FAKE-CLAUDE AGENT_TASKS_DIR:", os.environ.get("AGENT_TASKS_DIR", "unset"))
print("FAKE-CLAUDE AGENT:", os.environ.get("AGENT_TASKS_AGENT", "unset"))
print("FAKE-CLAUDE CWD:", os.path.realpath(os.getcwd()))
print("FAKE-CLAUDE CLAUDECODE:", os.environ.get("CLAUDECODE", "unset"))
data = sys.stdin.read()
prompt = data if data.strip() else (sys.argv[-1] if len(sys.argv) > 1 else "")
print("FAKE-CLAUDE PROMPT-VIA:", "stdin" if data.strip() else "argv")
lines = [l for l in prompt.splitlines() if l.strip()]
print("FAKE-CLAUDE LAST-LINE:", lines[-1] if lines else "(empty)")
sys.stdout.flush()
ob = os.environ.get("AGENT_TASKS_OUTBOX")

def write(text):
    with open(ob, "a", encoding="utf-8") as f:
        f.write(text)

if mode == "review":
    write("did the thing\\nverified: checks pass\\nSTATUS: review\\n")
    time.sleep(0.2)
elif mode == "blocked":
    write("half done\\nstatus: blocked: need API key\\n")
    time.sleep(0.2)
elif mode == "sleepy":
    time.sleep(5)
    write("slow but done\\nSTATUS: review\\n")
else:
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
    if "already claimed for you" not in out:
        fail("prompt should say the task is pre-claimed")
    if "STATUS: review" not in out or "STATUS: blocked:" not in out:
        fail("prompt missing the sentinel grammar")
    if os.path.join("runtime", "outbox") not in out:
        fail("prompt should name the outbox file")

    # stub claude; runner pinned to this interpreter so the test needs no uv
    stub = os.path.join(tmp, "fake_claude.py")
    with open(stub, "w", encoding="utf-8") as f:
        f.write(FAKE_CLAUDE)

    def set_cfg(mode=None, **kw):
        bin_argv = [PY, stub] + ([mode] if mode else [])
        cfg = {"claude_bin": bin_argv, "model": "haiku", "runner": [PY]}
        cfg.update(kw)
        with open(os.path.join(repo, ".agent-tasks", "config.json"), "w",
                  encoding="utf-8") as f:
            json.dump(cfg, f)

    set_cfg()

    # ---- start in-place (default) ----
    wid = started_id(disp("start", t1))
    t = json.loads(tasksc("show", t1, "--json").stdout)
    if t["status"] != "in_progress" or t["assignee"] != wid:
        fail(f"start should pre-claim for the minted worker: {t}")
    if "running" not in list_row(wid):
        fail("worker not listed running")
    ob1 = os.path.join(repo, ".agent-tasks", "runtime", "outbox", f"{wid}.md")
    if not os.path.isfile(ob1):
        fail("outbox not created at spawn")
    with open(os.path.join(repo, ".agent-tasks", "tasks", f"{t1}.md")) as f:
        if f"dispatched worker {wid}" not in f.read():
            fail("dispatch not logged to task note")
    if sh(["git", "check-ignore", "-q", ".agent-tasks/runtime/workers.json"],
          check=False).returncode != 0:
        fail("runtime/ not self-gitignored")

    time.sleep(1)
    log = log_text(wid)
    spawn_line = next(l for l in log.splitlines() if "spawn:" in l)
    if "Edit(" not in spawn_line or "tasks/**)" not in spawn_line \
            or "index.json)" not in spawn_line:
        fail(f"deny rules should cover index.json and tasks/** via Edit(): {spawn_line}")
    if "Write(" in spawn_line or "NotebookEdit(" in spawn_line:
        fail("only Edit() deny rules enforce; Write/NotebookEdit forms are "
             f"dead weight the harness warns about: {spawn_line}")
    if "outbox" in spawn_line:
        fail("the outbox must NOT be covered by the deny rules")
    for needle, msg in [
        ("--permission-mode acceptEdits", "permission-mode not passed"),
        ("--model haiku", "config model not passed"),
        ("tasks.py:*)", "queue CLI not allowlisted"),
        ('PowerShell(' + PY, "queue CLI not allowlisted for the PowerShell family"),
        ("--add-dir", "queue folder not added as a working directory"),
        ("--disallowedTools", "queue-write deny rules not passed"),
        ("FAKE-CLAUDE AGENT_TASKS_DIR: " + queue_root, "AGENT_TASKS_DIR not exported"),
        ("FAKE-CLAUDE CWD: " + os.path.realpath(repo), "in-place worker not in repo checkout"),
        ("FAKE-CLAUDE CLAUDECODE: unset", "parent CLAUDE_* env leaked into worker"),
    ]:
        if needle not in log:
            fail(f"{msg}\n--- log:\n{log}")

    # ---- worker dies mid-task (task stays pre-claimed) -> NEEDS-RESUME ----
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
    if not os.path.isfile(ob1):
        fail("resume should recreate the outbox (folded away on exit)")
    if not os.path.isfile(os.path.join(repo, ".agent-tasks", "runtime",
                                       "prompts", f"{wid}.resume1.txt")):
        fail("resume should write a durable continuation-prompt file")

    if f"stopped {wid}" not in disp("stop", wid).stdout:
        fail("stop output")
    time.sleep(1)
    if "exited" not in list_row(wid):
        fail("stopped worker still alive")

    # watch: stub sessions write no real transcript -> spawn-only timeline
    res = disp("watch", wid, "--from-start")
    if "showing the spawn log only" not in res.stdout:
        fail(f"watch should say the transcript is missing: {res.stdout}")
    if "[spawn] FAKE-CLAUDE ARGS:" not in res.stdout:
        fail(f"spawn-log lines should carry the [spawn] prefix: {res.stdout}")

    # worker resolution by task id and by unique prefix
    disp("wait", t1, check=False)
    disp("wait", wid[:12], check=False)

    # ---- multi-line prompt integrity (the cmd.exe truncation bug) ----
    # A marker on line 1 false-passes -- that is exactly how the bug hid.
    marker = "XYZZY-4242"
    extra = "\n".join([f"filler instruction line {i}" for i in range(1, 37)]
                      + [f"Your reply must contain the marker word {marker}."])
    for via in ("argv", "stdin"):
        set_cfg(prompt_via=via)
        tp = tasksc("create", f"Prompt integrity {via}").stdout.split()[1]
        wp = started_id(disp("start", tp, "--prompt-extra", extra))
        disp("wait", wp, check=False)
        log = log_text(wp)
        if f"FAKE-CLAUDE PROMPT-VIA: {via}" not in log:
            fail(f"prompt should travel via {via}:\n{log}")
        last = [l for l in log.splitlines() if "FAKE-CLAUDE LAST-LINE:" in l][-1]
        if marker not in last:
            fail(f"prompt truncated before its last line ({via}): {last}")
        pfile = os.path.join(repo, ".agent-tasks", "runtime", "prompts",
                             f"{wp}.txt")
        if not os.path.isfile(pfile):
            fail(f"durable prompt file missing ({via})")
        with open(pfile, encoding="utf-8") as f:
            ptext = f.read()
        if marker not in ptext or len(ptext.splitlines()) < 38:
            fail(f"durable prompt file incomplete ({via})")
    set_cfg()

    # ---- auto-doctor on exit: hand-edited note surfaces in wait ----
    td = tasksc("create", "Doctor bait").stdout.split()[1]
    wd = started_id(disp("start", td))
    bait = os.path.join(repo, ".agent-tasks", "tasks", f"{td}.md")
    with open(bait, encoding="utf-8") as f:
        text = f.read()
    with open(bait, "w", encoding="utf-8") as f:  # simulate a rogue hand-edit
        f.write(text.replace("status: in_progress", "status: done", 1))
    res = disp("wait", wd, check=False)
    if res.returncode != 3:
        fail(f"doctor reporting must not change wait's exit code: {res.returncode}")
    if "doctor:" not in res.stdout or "status drift" not in res.stdout:
        fail(f"wait should surface doctor findings: {res.stdout}")
    tasksc("doctor", "--fix", check=False)  # heal the bait for later tests
    tasksc("status", td, "cancelled")

    # ---- outbox sentinel: review (folded via `list`) ----
    set_cfg("review")
    tr = tasksc("create", "Sentinel review task").stdout.split()[1]
    wr = started_id(disp("start", tr))
    time.sleep(1.5)  # stub writes sentinel and exits almost immediately
    disp("list")     # any observation of an exited worker folds its outbox
    t = json.loads(tasksc("show", tr, "--json").stdout)
    if t["status"] != "review":
        fail(f"review sentinel should move the task to review: {t}")
    note_file = os.path.join(repo, ".agent-tasks", "tasks", f"{tr}.md")
    with open(note_file, encoding="utf-8") as f:
        note = f.read()
    if "did the thing" not in note or f"outbox of {wr} folded:" not in note:
        fail(f"outbox content not folded into note:\n{note}")
    if "-> review (outbox sentinel)" not in note:
        fail("sentinel transition not logged")
    obr = os.path.join(repo, ".agent-tasks", "runtime", "outbox", f"{wr}.md")
    if os.path.isfile(obr) or not os.path.isfile(obr[:-3] + ".folded.md"):
        fail("outbox should be archived to .folded.md in the fold")
    disp("list")  # idempotence: a second observation must not double-append
    disp("wait", wr, check=False)
    with open(note_file, encoding="utf-8") as f:
        if f.read().count("did the thing") != 1:
            fail("fold is not idempotent")

    # ---- outbox sentinel: blocked (folded via `wait`) ----
    set_cfg("blocked")
    tbk = tasksc("create", "Sentinel blocked task").stdout.split()[1]
    wb = started_id(disp("start", tbk))
    res = disp("wait", wb, check=False)
    if res.returncode != 0:
        fail(f"blocked sentinel wait rc={res.returncode}, want 0 (not mid-task)")
    if "outbox folded: blocked: need API key" not in res.stdout:
        fail(f"wait should report the fold: {res.stdout}")
    t = json.loads(tasksc("show", tbk, "--json").stdout)
    if t["status"] != "open" or "need API key" not in t["blockers"]:
        fail(f"blocked sentinel should reopen with the blocker recorded: {t}")

    # ---- auto-heartbeat: supervisor keeps a live worker's lease fresh ----
    set_cfg("sleepy", lease_minutes=0.02)  # 1.2s lease vs a 5s worker
    th = tasksc("create", "Slow heartbeat task").stdout.split()[1]
    wh = started_id(disp("start", th))
    waiter = subprocess.Popen([PY, DISPATCH, "wait", wh], cwd=repo, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
    time.sleep(3)  # without auto-heartbeat the lease is long expired by now
    steal = sh([PY, TASKS, "claim", th, "--assignee", "rival"], check=False)
    if steal.returncode == 0:
        fail("auto-heartbeat failed: a rival stole a live worker's lease")
    waiter.communicate(timeout=30)
    if waiter.returncode != 0:
        fail(f"sleepy worker wait rc={waiter.returncode}")
    t = json.loads(tasksc("show", th, "--json").stdout)
    if t["status"] != "review":
        fail(f"sleepy worker should end in review: {t}")
    set_cfg()  # back to plain defaults

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
    if res.returncode != 3:  # stub never finishes the task, so it stays claimed
        fail(f"worktree worker wait rc={res.returncode}, want 3")

    # ---- double-dispatch: exactly one spawns; loser exits before spawning ----
    tc = tasksc("create", "Contested task").stdout.split()[1]
    racers = [subprocess.Popen([PY, DISPATCH, "run", tc], cwd=repo, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True) for _ in range(2)]
    outs = [r.communicate() for r in racers]
    winners = [i for i, r in enumerate(racers) if r.returncode == 0]
    if len(winners) != 1:
        fail(f"double-dispatch: want exactly 1 winner, got {len(winners)}\n{outs}")
    loser_err = outs[1 - winners[0]][1]
    if "in_progress" not in loser_err:
        fail(f"loser should die on the pre-claim, explaining why: {loser_err}")
    contested = [x for x in json.loads(disp("list", "--json").stdout)
                 if x["task"] == tc]
    if len(contested) != 1:
        fail(f"double-dispatch spawned {len(contested)} workers")
    disp("wait", tc, check=False)

    # ---- shell-family rule expansion (Bash <-> PowerShell twins) ----
    set_cfg(allowed_tools=["Bash(pnpm test:*)"])
    tsf = tasksc("create", "Shell family task").stdout.split()[1]
    wsf = started_id(disp("start", tsf))
    time.sleep(1)
    line = next(l for l in log_text(wsf).splitlines() if "spawn:" in l)
    if "PowerShell(pnpm test:*)" not in line or "Bash(pnpm test:*)" not in line:
        fail(f"config Bash rule should gain its PowerShell twin: {line}")
    if line.count("PowerShell(") < 3:  # 2 CLI quoting styles + the twin
        fail(f"queue CLI should carry both families: {line}")
    disp("wait", wsf, check=False)

    set_cfg(allowed_tools=["Bash(pnpm test:*)"], expand_shell_rules=False)
    tsf2 = tasksc("create", "Shell family opt-out").stdout.split()[1]
    wsf2 = started_id(disp("start", tsf2))
    time.sleep(1)
    line = next(l for l in log_text(wsf2).splitlines() if "spawn:" in l)
    if "PowerShell(pnpm test:*)" in line:
        fail("expand_shell_rules: false must suppress the twin for config rules")
    if "PowerShell(" not in line:
        fail("the queue CLI itself always gets both families")
    disp("wait", wsf2, check=False)
    set_cfg()

    # ---- claude_bin ladder: flag > env > local overlay > project config ----
    local_cfg = os.path.join(repo, ".agent-tasks", "config.local.json")
    set_cfg()  # rewrite project config...
    with open(os.path.join(repo, ".agent-tasks", "config.json"), "r+",
              encoding="utf-8") as f:
        cfg_now = json.load(f)
        cfg_now["claude_bin"] = "project-config-bogus-binary"
        f.seek(0); f.truncate(); json.dump(cfg_now, f)
    with open(local_cfg, "w", encoding="utf-8") as f:
        json.dump({"claude_bin": [PY, stub], "model": "sonnet"}, f)
    tl = tasksc("create", "Ladder task").stdout.split()[1]
    wl = started_id(disp("start", tl))  # local overlay beats bogus project bin
    time.sleep(1)
    if "--model sonnet" not in log_text(wl):
        fail("generic local-overlay key (model) should apply")
    disp("wait", wl, check=False)

    tl2 = tasksc("create", "Ladder env task").stdout.split()[1]
    env_bak = dict(env)
    env["AGENT_TASKS_CLAUDE_BIN"] = "env-var-bogus-binary"
    res = disp("start", tl2, check=False)
    if res.returncode == 0 or "env-var-bogus-binary" not in res.stderr:
        fail(f"env var should outrank the local overlay: {res.stderr}")
    res = disp("start", tl2, "--claude-bin", "flag-bogus-binary", check=False)
    if res.returncode == 0 or "flag-bogus-binary" not in res.stderr:
        fail(f"--claude-bin flag should outrank the env var: {res.stderr}")
    env.clear(); env.update(env_bak)
    os.remove(local_cfg)
    set_cfg()

    # ---- unresolvable claude binary: clear error, pre-claim reverted ----
    tb = tasksc("create", "Binary-less task").stdout.split()[1]
    res = disp("start", tb, "--claude-bin", "definitely-not-a-real-binary-xyz",
               check=False)
    if res.returncode == 0:
        fail("unresolvable claude binary should fail the start")
    if "not found" not in res.stderr or "claude_bin" not in res.stderr:
        fail(f"unresolved-binary error should name the fix: {res.stderr}")
    t = json.loads(tasksc("show", tb, "--json").stdout)
    if t["status"] != "open" or t["assignee"]:
        fail(f"failed spawn must revert the pre-claim: {t}")
    with open(os.path.join(repo, ".agent-tasks", "tasks", f"{tb}.md"),
              encoding="utf-8") as f:
        if "reverted pre-claim" not in f.read():
            fail("revert should be logged in the work log")

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
