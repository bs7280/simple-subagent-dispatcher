#!/usr/bin/env python3
"""agent-tasks dispatcher -- spawn, watch, wait on, and resume headless Claude Code
workers against tasks in the .agent-tasks/ queue.

Each worker is a top-level `claude -p` process with its own minted --session-id.
Claude Code persists the transcript to disk incrementally
(~/.claude/projects/<encoded-cwd>/<session-id>.jsonl) regardless of the
process's fate, so:
  - "watching" a worker means tailing its real transcript (located by globbing
    the session id -- the cwd->dirname encoding is not stable across Claude Code
    versions, the session id is), and
  - a dead worker loses nothing: `resume` continues the same session,
    uncommitted working-tree edits and all.

Workers run in the repo checkout by default. Pass --worktree (or set
"worktree": true in .agent-tasks/config.json) to give each worker an isolated
git worktree -- that's the project's judgment call, not a requirement.

The dispatcher exports AGENT_TASKS_DIR to every worker so the queue CLI always
resolves to the ONE shared queue -- important in worktree mode, where a
committed .agent-tasks/ would otherwise appear as a second stale copy inside
the worktree.

Runtime state (worker registry, spawn logs) lives in .agent-tasks/runtime/,
which self-gitignores: machine-local, never committed.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import procs  # noqa: E402  -- cross-platform process shim, same directory
import tasks  # noqa: E402  -- the queue CLI, same directory

RUNTIME = "runtime"
WORKERS_FILE = "workers.json"

load_config = tasks.load_config  # one config file, one loader, one defaults dict


def runner_str(cfg):
    """The configured interpreter invocation as prompt-ready text."""
    return " ".join(cfg["runner"])


def repo_of(root):
    """The repo the queue folder sits in."""
    return os.path.dirname(root)


def ensure_runtime(root):
    rt = os.path.join(root, RUNTIME)
    os.makedirs(os.path.join(rt, "logs"), exist_ok=True)
    gi = os.path.join(rt, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write("*\n")  # machine-local state: never committed
    return rt


def load_workers(root):
    path = os.path.join(root, RUNTIME, WORKERS_FILE)
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_workers(root, workers):
    rt = ensure_runtime(root)
    fd, tmp = tempfile.mkstemp(dir=rt, prefix=".workers-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(workers, f, indent=2)
        f.write("\n")
    os.replace(tmp, os.path.join(rt, WORKERS_FILE))


pid_alive = procs.is_alive  # all liveness checks go through the shim


def worker_state(w):
    return "running" if pid_alive(w["pid"]) else "exited"


def resolve_worker(root, workers, raw):
    """Accept a worker id, a unique prefix of one, or a task id (latest worker)."""
    if raw in workers:
        return raw
    prefixed = [wid for wid in workers if wid.startswith(raw.lower())]
    if len(prefixed) == 1:
        return prefixed[0]
    if len(prefixed) > 1:
        tasks.die(f"ambiguous worker '{raw}': {', '.join(sorted(prefixed))}")
    # task-id form: TASK-042, task-042, or bare 42
    up, tid = raw.upper(), None
    index = tasks.load_index(root)
    if up in index["tasks"]:
        tid = up
    elif raw.isdigit():
        for t in index["tasks"]:
            m = re.match(r"^[A-Z]+-0*(\d+)$", t)
            if m and int(m.group(1)) == int(raw):
                tid = t
                break
    if tid:
        cands = sorted((wid for wid, w in workers.items() if w["task"] == tid),
                       key=lambda wid: workers[wid]["started"])
        if cands:
            return cands[-1]
    tasks.die(f"no worker matching '{raw}' (see `list`)")


def cli_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks.py")


def find_transcript(session_id):
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


# ---------------------------------------------------------------- prompts

WORKER_PROMPT = """You are an unattended task worker executing exactly one task from a file-based queue.

Queue CLI (run via Bash): {run} "{cli}"
The queue lives at {root} -- AGENT_TASKS_DIR is set in your environment, so the
CLI resolves to that one shared queue from anywhere, including git worktrees.
Your task: {tid}
Your agent/assignee name: {agent}

Do this, in order:
1. Claim it: {run} "{cli}" claim {tid} --assignee {agent}
   If the claim fails, print why and stop immediately -- never --force it.
2. Read the whole task note: {run} "{cli}" show {tid}
   The note is your entire spec. If it is too vague to act on, run
   {run} "{cli}" block {tid} "question for planner: <what you need>" --agent {agent}
   and stop -- an unattended wrong guess costs more than a paused task.
3. Do the work, staying strictly inside the note's scope.
   - Your claim is a lease, not a lock. During long steps, extend it:
     {run} "{cli}" heartbeat {tid} --assignee {agent}
     (at least once per half lease). If a heartbeat fails because the task is
     assigned to someone else, your expired claim was stolen: stop immediately.
   - Log milestones as you go: {run} "{cli}" log {tid} "<update>" --agent {agent}
   - Longer findings go in the note's Notes section (edit the file directly;
     keep Work log as the last section).
   - Out-of-scope discoveries (adjacent bugs, refactor itches): create a new
     task ("found while working {tid}: ...") instead of expanding yours.
   - If stuck (missing credential, broken dependency, need a human): block the
     task with the reason, log where you left off in enough detail that anyone
     could resume, and stop cleanly.
4. Finish: self-review your changes and actually run the acceptance checks, then
   {run} "{cli}" log {tid} "done: <what changed>; verified: <how>; reviewer should check: <what>" --agent {agent}
   {run} "{cli}" status {tid} review --agent {agent}
   Never mark the task done -- closing is the reviewer's call, not yours.

Hard rules:
- NEVER launch a long-running command in the background and end your turn
  "waiting" for it. Headless sessions are not re-invoked when background work
  finishes -- that is death, not patience. Run long commands in the foreground.
- Never touch DB migrations, permissions/financial data, force pushes, or
  deploys unless the task note explicitly says to.
- If the agent-tasks plugin's task-worker skill is available, it restates
  these rules; follow it.{extra}"""

CONTINUATION_PROMPT = """You are resuming an interrupted unattended task-worker session for {tid}.
First re-read your task ({run} "{cli}" show {tid}) and your own Work log
entries, and check the working directory state (git status, git diff) to see
what you had already done. Then continue exactly where you left off and finish
per your original instructions: log milestones, never launch a background
command and end your turn waiting for it, and finish with a completion summary
and status "review" (never "done")."""


def build_prompt(root, tid, agent, extra, cfg):
    extra = f"\n\nAdditional instructions from the dispatcher:\n{extra}" if extra else ""
    return WORKER_PROMPT.format(cli=cli_path(), root=root, tid=tid, agent=agent,
                                run=runner_str(cfg), extra=extra)


# ---------------------------------------------------------------- spawn

def spawn(root, workdir, cmd, log_path, agent):
    # Strip the parent session's CLAUDE_* env (CLAUDECODE, session id, child-
    # session marker, messaging socket, ...): a spawned worker that inherits it
    # is treated as a nested child session and loses the ability to self-approve
    # actions -- every write gets denied. Workers must be top-level sessions.
    # CLAUDE_CONFIG_DIR is kept: it points at auth/config, not session state.
    env = {k: v for k, v in os.environ.items()
           if k == "CLAUDE_CONFIG_DIR"
           or not (k == "CLAUDECODE" or k.startswith("CLAUDE_"))}
    env["AGENT_TASKS_DIR"] = root       # one shared queue, even from worktrees
    env["AGENT_TASKS_AGENT"] = agent
    with open(log_path, "ab") as logf:
        logf.write((f"\n=== {tasks.now()} spawn: " + " ".join(cmd[:-1])
                    + " <prompt>\n").encode())
        logf.flush()
        return procs.spawn(cmd, workdir, env, logf)


def claude_cmd(cfg, args, base):
    model = getattr(args, "model", None) or cfg["model"]
    pm = getattr(args, "permission_mode", None) or cfg["permission_mode"]
    bin_arg = getattr(args, "claude_bin", None) or cfg["claude_bin"]
    bin_argv = list(bin_arg) if isinstance(bin_arg, list) else [bin_arg]
    cmd = [*bin_argv, "-p", "--permission-mode", pm] + base
    # The worker must always be able to run the queue CLI unattended (claim,
    # log, block, finish) -- pre-approve it under both quoting styles a worker
    # might type. Project-specific rules (build/test/git commands the worker
    # needs) come from config "allowed_tools".
    cli = cli_path()
    run = runner_str(cfg)
    rules = [f'Bash({run} "{cli}":*)', f"Bash({run} {cli}:*)"]
    rules += list(cfg["allowed_tools"])
    cmd += ["--allowedTools", " ".join(rules)]
    if model:
        cmd += ["--model", model]
    cmd += list(cfg["extra_args"])
    return cmd, model, pm


def cmd_start(args):
    root = tasks.find_dir()
    cfg = load_config(root)
    repo = repo_of(root)
    index = tasks.load_index(root)
    tid = tasks.resolve_id(index, args.task)
    task = index["tasks"][tid]
    if task["status"] != "open" and not args.force:
        tasks.die(f"{tid} is {task['status']}, not open -- use --force to dispatch anyway")
    unresolved = tasks.unresolved_blockers(index, task)
    if unresolved and not args.force:
        tasks.die(f"{tid} is blocked by: {', '.join(unresolved)} -- use --force to dispatch anyway")

    session_id = str(uuid.uuid4())
    worker_id = f"{tid.lower()}-{session_id[:8]}"
    agent = args.agent_name or worker_id

    use_worktree = cfg["worktree"] if args.worktree is None else args.worktree
    workdir, branch = repo, None
    if use_worktree:
        wt_root = cfg["worktree_root"] or f"{repo.rstrip(os.sep)}-worktrees"
        workdir = os.path.join(wt_root, worker_id)
        branch = args.branch or f"agent-tasks/{worker_id}"
        os.makedirs(wt_root, exist_ok=True)
        res = subprocess.run(["git", "-C", repo, "worktree", "add", "-b", branch, workdir],
                             capture_output=True, text=True)
        if res.returncode != 0:
            tasks.die(f"git worktree add failed:\n{res.stderr.strip()}")
        boot = os.path.join(repo, cfg["bootstrap"])
        if os.path.isfile(boot):
            bres = subprocess.run([*cfg["runner"], boot], cwd=workdir,
                                  capture_output=True, text=True)
            tail = (bres.stderr or bres.stdout).strip()[-2000:]
            if bres.returncode != 0:
                print(f"warning: bootstrap exited {bres.returncode} (continuing):\n{tail}",
                      file=sys.stderr)
            else:
                print(f"bootstrap ok: {cfg['bootstrap']}")

    prompt = build_prompt(root, tid, agent, args.prompt_extra, cfg)
    cmd, model, pm = claude_cmd(cfg, args, ["--session-id", session_id])
    cmd.append(prompt)

    rt = ensure_runtime(root)
    log_path = os.path.join(rt, "logs", f"{worker_id}.out")
    proc = spawn(root, workdir, cmd, log_path, agent)

    entry = {"task": tid, "session_id": session_id, "pid": proc.pid,
             "cwd": workdir, "worktree_branch": branch, "model": model,
             "permission_mode": pm, "agent": agent, "started": tasks.now(),
             "resumes": 0}
    with tasks.Lock(root):
        workers = load_workers(root)
        workers[worker_id] = entry
        save_workers(root, workers)
        tasks.append_log(root, tid, "dispatcher",
                         f"dispatched worker {worker_id} "
                         f"(session {session_id}, pid {proc.pid}, cwd {workdir})")
    print(f"started {worker_id}")
    print(f"  task: {tid}   pid: {proc.pid}   model: {model or '(default)'}   "
          f"permission-mode: {pm}")
    print(f"  cwd:  {workdir}" + (f"   (worktree branch {branch})" if branch else ""))
    print(f"  log:  {log_path}")
    print(f"  next: `watch {worker_id} --follow` to observe, `wait {worker_id}` to block")


def needs_resume(index, workers, wid):
    w = workers[wid]
    if worker_state(w) == "running":
        return False
    if index["tasks"].get(w["task"], {}).get("status") != "in_progress":
        return False
    # not stale if a newer/other worker is already running this task
    return not any(o["task"] == w["task"] and worker_state(o) == "running"
                   for owid, o in workers.items() if owid != wid)


def cmd_list(args):
    root = tasks.find_dir()
    workers = load_workers(root)
    index = tasks.load_index(root)
    if args.json:
        out = []
        for wid in sorted(workers, key=lambda k: workers[k]["started"]):
            w = dict(workers[wid])
            w["id"] = wid
            w["state"] = worker_state(workers[wid])
            w["needs_resume"] = needs_resume(index, workers, wid)
            w["task_status"] = index["tasks"].get(w["task"], {}).get("status")
            out.append(w)
        print(json.dumps(out, indent=2))
        return
    if not workers:
        print("no workers dispatched yet")
        return
    for wid in sorted(workers, key=lambda k: workers[k]["started"]):
        w = workers[wid]
        state = worker_state(w)
        tstat = index["tasks"].get(w["task"], {}).get("status", "?")
        line = (f"{wid:<24} {state:<8} pid {w['pid']:<7} {w['task']:<10} "
                f"task:{tstat:<12} started {w['started']}")
        if needs_resume(index, workers, wid):
            line += "  [NEEDS-RESUME]"
        print(line)


# ---------------------------------------------------------------- watch

def _trunc(s, n=160):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "..."


def parse_line(line):
    """Compact, human-scannable events out of one transcript jsonl line."""
    try:
        obj = json.loads(line)
    except ValueError:
        return []
    ts = (obj.get("timestamp") or "")[11:19]
    msg = obj.get("message") or {}
    out = []
    if obj.get("type") == "assistant":
        for blk in msg.get("content") or []:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "text" and blk.get("text", "").strip():
                out.append(f"{ts} assistant  {_trunc(blk['text'])}")
            elif blk.get("type") == "tool_use":
                inp = blk.get("input") or {}
                what = (inp.get("description") or inp.get("command")
                        or inp.get("file_path") or inp.get("prompt") or "")
                out.append(f"{ts} tool       {blk.get('name', '?')}: {_trunc(what, 120)}")
    elif obj.get("type") == "user":
        content = msg.get("content")
        if isinstance(content, list):
            for blk in content:
                if (isinstance(blk, dict) and blk.get("type") == "tool_result"
                        and blk.get("is_error")):
                    out.append(f"{ts} tool-ERROR {_trunc(blk.get('content'), 200)}")
    return out


def cmd_watch(args):
    root = tasks.find_dir()
    workers = load_workers(root)
    wid = resolve_worker(root, workers, args.worker)
    w = workers[wid]
    path = find_transcript(w["session_id"])
    if not path:
        tasks.die(f"no transcript yet for session {w['session_id']} -- the worker may "
                  f"not have started; check its spawn log: "
                  f"{os.path.join(root, RUNTIME, 'logs', wid + '.out')}")
    print(f"transcript: {path}  (worker {wid}, {worker_state(w)})")
    with open(path) as f:
        events = [e for line in f for e in parse_line(line)]
        if not args.from_start:
            events = events[-args.tail:]
        for e in events:
            print(e)
        if not args.follow:
            return
        buf = ""
        while True:
            chunk = f.readline()
            if chunk:
                buf += chunk
                if buf.endswith("\n"):
                    for e in parse_line(buf):
                        print(e, flush=True)
                    buf = ""
                continue
            if not pid_alive(w["pid"]):
                print(f"[worker {wid} exited]")
                return
            time.sleep(0.5)


def cmd_wait(args):
    root = tasks.find_dir()
    workers = load_workers(root)
    wid = resolve_worker(root, workers, args.worker)
    w = workers[wid]
    deadline = time.time() + args.timeout if args.timeout else None
    while pid_alive(w["pid"]):
        if deadline and time.time() > deadline:
            print(f"timeout: {wid} still running (pid {w['pid']})")
            sys.exit(2)
        time.sleep(1)
    status = tasks.load_index(root)["tasks"].get(w["task"], {}).get("status", "?")
    print(f"{wid} exited; {w['task']} status: {status}")
    if status == "in_progress":
        print(f"worker died mid-task -- resume with: dispatch.py resume {wid}")
        sys.exit(3)


def cmd_resume(args):
    root = tasks.find_dir()
    cfg = load_config(root)
    workers = load_workers(root)
    wid = resolve_worker(root, workers, args.worker)
    w = workers[wid]
    if pid_alive(w["pid"]):
        tasks.die(f"{wid} is still running (pid {w['pid']}) -- stop it first if you "
                  f"really want to restart")
    if not os.path.isdir(w["cwd"]):
        tasks.die(f"worker cwd is gone: {w['cwd']}")
    prompt = args.prompt or CONTINUATION_PROMPT.format(
        cli=cli_path(), tid=w["task"], run=runner_str(cfg))
    cmd, model, pm = claude_cmd(cfg, args, ["--resume", w["session_id"]])
    cmd.append(prompt)
    log_path = os.path.join(ensure_runtime(root), "logs", f"{wid}.out")
    proc = spawn(root, w["cwd"], cmd, log_path, w["agent"])
    with tasks.Lock(root):
        workers = load_workers(root)
        workers[wid].update(pid=proc.pid, resumes=workers[wid].get("resumes", 0) + 1,
                            resumed=tasks.now())
        save_workers(root, workers)
        tasks.append_log(root, w["task"], "dispatcher",
                         f"resumed worker {wid} (pid {proc.pid})")
    print(f"resumed {wid} (pid {proc.pid}, session {w['session_id']})")


def cmd_stop(args):
    root = tasks.find_dir()
    workers = load_workers(root)
    wid = resolve_worker(root, workers, args.worker)
    w = workers[wid]
    if not pid_alive(w["pid"]):
        print(f"{wid} is not running")
        return
    procs.terminate_tree(w["pid"])
    with tasks.Lock(root):
        tasks.append_log(root, w["task"], "dispatcher", f"stopped worker {wid}")
    print(f"stopped {wid} (pid {w['pid']}); its session survives -- "
          f"`resume {wid}` continues it")


def cmd_prompt(args):
    root = tasks.find_dir()
    cfg = load_config(root)
    index = tasks.load_index(root)
    tid = tasks.resolve_id(index, args.task)
    print(build_prompt(root, tid, args.agent_name or "<worker-name>",
                       args.prompt_extra, cfg))


# ---------------------------------------------------------------- cli

def main():
    parser = argparse.ArgumentParser(
        prog="dispatch", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="spawn a headless worker for a task")
    p.add_argument("task")
    p.add_argument("--agent-name", help="assignee/work-log name (default: worker id)")
    p.add_argument("--model", help="override config/CLI-default model")
    p.add_argument("--permission-mode", help="override config (default: auto)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--worktree", dest="worktree", action="store_true", default=None,
                   help="isolate the worker in a fresh git worktree")
    g.add_argument("--in-place", dest="worktree", action="store_false",
                   help="run in the repo checkout (the default unless config says otherwise)")
    p.add_argument("--branch", help="worktree branch name (default: agent-tasks/<worker-id>)")
    p.add_argument("--prompt-extra", help="extra instructions appended to the worker prompt")
    p.add_argument("--claude-bin", help="override the claude binary (mostly for tests)")
    p.add_argument("--force", action="store_true",
                   help="dispatch even if the task is not open / is blocked")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("list", help="list workers; flags [NEEDS-RESUME]")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("watch", help="show a worker's transcript events")
    p.add_argument("worker", help="worker id, unique prefix, or task id")
    p.add_argument("--follow", action="store_true", help="keep tailing live")
    p.add_argument("--tail", type=int, default=20, help="events to show (default 20)")
    p.add_argument("--from-start", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("wait", help="block until a worker exits "
                                    "(exit 3 = died mid-task, 2 = timeout)")
    p.add_argument("worker")
    p.add_argument("--timeout", type=float, help="seconds")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("resume", help="continue a dead worker's session")
    p.add_argument("worker")
    p.add_argument("--prompt", help="override the default continuation prompt")
    p.add_argument("--model")
    p.add_argument("--permission-mode")
    p.add_argument("--claude-bin")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("stop", help="SIGTERM a running worker (session survives)")
    p.add_argument("worker")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("prompt", help="print the worker prompt for a task (no spawn)")
    p.add_argument("task")
    p.add_argument("--agent-name")
    p.add_argument("--prompt-extra")
    p.set_defaults(func=cmd_prompt)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
