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
import shutil
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
    os.makedirs(os.path.join(rt, "outbox"), exist_ok=True)
    os.makedirs(os.path.join(rt, "prompts"), exist_ok=True)
    gi = os.path.join(rt, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w", encoding="utf-8") as f:
            f.write("*\n")  # machine-local state: never committed
    return rt


def load_workers(root):
    path = os.path.join(root, RUNTIME, WORKERS_FILE)
    if not os.path.isfile(path):
        return {}
    with open(procs.long_path(path), encoding="utf-8") as f:
        return json.load(f)


def save_workers(root, workers):
    rt = ensure_runtime(root)
    fd, tmp = tempfile.mkstemp(dir=rt, prefix=".workers-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(workers, f, indent=2)
        f.write("\n")
    os.replace(tmp, procs.long_path(os.path.join(rt, WORKERS_FILE)))


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


def outbox_path(root, wid):
    return os.path.join(root, RUNTIME, "outbox", f"{wid}.md")


def cli_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks.py")


def find_transcript(session_id):
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


# ---------------------------------------------------------------- prompts

WORKER_PROMPT = """You are an unattended task worker executing exactly one task.

Your task: {tid}. The full spec is the task note at:
  {note}
Read it first -- it is your entire spec. The task is already claimed for you
(assignee: {agent}), and your supervisor keeps the claim alive while you run.
You do not need to run any queue commands.

YOUR OUTBOX -- the one file you report through:
  {outbox}
Write progress notes, findings, and decisions there as you go, as plain
markdown, with your ordinary file tools. It is yours alone -- nobody else
writes to it -- and it is folded into the task note for you afterwards.

Queue state is read-only to you: never edit the task note, index.json, or
anything else under {root}. Your outbox is the sanctioned place to write.

When you finish, the LAST line of your outbox must be exactly one of:
  STATUS: review
  STATUS: blocked: <what you need>
Use review only after actually running the note's acceptance checks; record
in the outbox what changed, how you verified it, and what a reviewer should
double-check. Use blocked when you cannot proceed (missing credential, broken
dependency, spec too vague to act on) -- say what you need and stop cleanly.
Those are the only two endings; done is the reviewer's call, never yours.

Rules:
- Stay strictly inside the note's scope. Found an adjacent bug or a refactor
  itch? Describe it in your outbox for the planner instead of fixing it.
- NEVER launch a long-running command in the background and end your turn
  "waiting" for it. Headless sessions are not re-invoked when background work
  finishes -- run long commands in the foreground.
- Never touch DB migrations, permissions/financial data, force pushes, or
  deploys unless the task note explicitly says to.
- Only if the note tells you to commit and you share the checkout with other
  workers (no dedicated worktree): wrap the stage->commit span in
  {run} "{cli}" lock commit --agent {agent}  ...  {run} "{cli}" unlock commit --agent {agent}
  and stage only the files you changed, by name -- never git add -A / -u /
  commit -a.{extra}"""

CONTINUATION_PROMPT = """You are resuming an interrupted unattended task-worker session for {tid}.
Re-read the task note ({note}) -- any earlier outbox content of yours was
already folded into its Work log -- and your outbox ({outbox}). Check the
working tree state (git status, git diff), then continue exactly where you
left off, per your original instructions: progress goes in your outbox, and
its LAST line must end up as the STATUS sentinel (STATUS: review, or
STATUS: blocked: <what you need>). The task remains claimed for you; your
supervisor keeps the claim alive."""


def build_prompt(root, tid, agent, extra, cfg, outbox):
    extra = f"\n\nAdditional instructions from the dispatcher:\n{extra}" if extra else ""
    return WORKER_PROMPT.format(cli=cli_path(), root=root, tid=tid, agent=agent,
                                note=tasks.note_path(root, tid), outbox=outbox,
                                run=runner_str(cfg), extra=extra)


SENTINEL = re.compile(r"^\s*status\s*:\s*(review|blocked)\b[:\s]*(.*?)\s*$", re.I)


def parse_sentinel(text):
    """Tiny, forgiving grammar: one token, LAST occurrence wins,
    case-insensitive; everything else in the outbox is prose."""
    last = None
    for line in text.splitlines():
        m = SENTINEL.match(line)
        if m:
            last = (m.group(1).lower(), m.group(2).strip())
    return last


def fold_outbox(root, wid, w):
    """Fold a finished worker's outbox into the canonical task note under the
    queue lock, apply its sentinel through the same primitives the CLI uses,
    and archive the outbox in the same locked span (idempotence: a crashed
    fold can never double-append). Returns a summary string, or None if there
    was nothing to fold."""
    outbox = w.get("outbox") or outbox_path(root, wid)
    if not os.path.isfile(outbox):
        return None
    with tasks.Lock(root):
        if not os.path.isfile(outbox):
            return None  # another observer folded it while we waited
        with open(procs.long_path(outbox), encoding="utf-8") as f:
            text = f.read()
        tid = w["task"]
        body = text.strip()
        if body and os.path.isfile(tasks.note_path(root, tid)):
            tasks.append_log(root, tid, "dispatcher", f"outbox of {wid} folded:")
            with open(procs.long_path(tasks.note_path(root, tid)), "a",
                      encoding="utf-8") as f:
                for line in body.splitlines():
                    f.write(f"    {line}\n")
        applied = "no sentinel"
        sentinel = parse_sentinel(text)
        index = tasks.load_index(root)
        task = index["tasks"].get(tid)
        if sentinel and task:
            kind, reason = sentinel
            if task.get("assignee") != w["agent"] or task["status"] != "in_progress":
                applied = (f"sentinel '{kind}' ignored: task is now "
                           f"{task['status']} (assignee {task.get('assignee')})")
                tasks.append_log(root, tid, "dispatcher", applied)
            elif kind == "review":
                task["status"] = "review"
                task.pop("lease_until", None)
                task["updated"] = tasks.now()
                tasks.save_index(root, index)
                tasks.set_note_status(root, tid, "review")
                tasks.append_log(root, tid, w["agent"],
                                 "status: in_progress -> review (outbox sentinel)")
                applied = "review"
            else:
                reason = reason or f"worker {wid} reported blocked without a reason"
                if reason not in task["blockers"]:
                    task["blockers"].append(reason)
                task["status"] = "open"
                task.pop("lease_until", None)
                task["updated"] = tasks.now()
                tasks.save_index(root, index)
                tasks.set_note_status(root, tid, "open")
                tasks.append_log(root, tid, w["agent"],
                                 f"blocked (outbox sentinel): {reason} -- back to open")
                applied = f"blocked: {reason}"
        os.replace(procs.long_path(outbox),
                   procs.long_path(outbox[:-3] + ".folded.md"))
    return applied


def maybe_fold(root, workers, wid):
    """Fold an exited worker's outbox, if it has one waiting."""
    w = workers[wid]
    if worker_state(w) == "running":
        return None
    return fold_outbox(root, wid, w)


def report_doctor(root):
    """Auto-doctor on worker exit: drift/orphan detection happens in the
    loop, not when a human remembers. Reported only -- callers keep their
    own exit-code contracts."""
    findings, _ = tasks.doctor_findings(root)
    for finding in findings:
        print(f"doctor: {finding}")


def auto_heartbeat(root, w, cfg):
    """The dispatcher keeps a verifiably-alive worker's lease fresh, so
    dispatched workers carry no heartbeat duty (cheap models forget it;
    supervisors don't). Worker-side heartbeat remains the backstop for
    externally-run agents."""
    with tasks.Lock(root):
        index = tasks.load_index(root)
        task = index["tasks"].get(w["task"])
        if (task and task["status"] == "in_progress"
                and task.get("assignee") == w["agent"]):
            task["lease_until"] = tasks.compute_lease(cfg)
            task["updated"] = tasks.now()
            tasks.save_index(root, index)


def heartbeat_interval(cfg):
    """Half the lease, clamped to [1s, 60s]."""
    return max(1.0, min(60.0, float(cfg["lease_minutes"]) * 30.0))


# ---------------------------------------------------------------- spawn

def spawn(root, workdir, cmd, log_path, agent, outbox=None, stdin=None):
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
    if outbox:
        env["AGENT_TASKS_OUTBOX"] = outbox
    with open(procs.long_path(log_path), "ab") as logf:
        logf.write((f"\n=== {tasks.now()} spawn: " + " ".join(cmd[:-1])
                    + " <prompt>\n").encode())
        logf.flush()
        return procs.spawn(cmd, workdir, env, logf, stdin=stdin)


def resolve_claude_bin(bin_argv):
    """which()-resolve argv[0] (on Windows the claude CLI is an npm shim trio
    claude / claude.cmd / claude.ps1, and Popen does no PATHEXT resolution --
    a bare "claude" dies with WinError 2). If the result is a .cmd/.bat shim,
    unwrap to a sibling .exe when one exists, since cmd.exe mangles multi-line
    argv (a prompt truncates at its first newline). Returns (argv, is_batch)
    -- is_batch True means the binary is STILL a batch file and the prompt
    must travel via stdin, not argv."""
    resolved = shutil.which(bin_argv[0])
    if resolved is None:
        tasks.die(f"claude binary '{bin_argv[0]}' not found (searched PATH via "
                  f"shutil.which, PATHEXT honored on Windows) -- set "
                  f"claude_bin in .agent-tasks/config.json or pass --claude-bin")
    if resolved.lower().endswith((".cmd", ".bat")):
        exe = os.path.splitext(resolved)[0] + ".exe"
        if os.path.isfile(exe):
            return [exe, *bin_argv[1:]], False
        return [resolved, *bin_argv[1:]], True
    return [resolved, *bin_argv[1:]], False


STDIN_POINTER = ("Your full instructions are the piped stdin content. "
                 "Follow them exactly.")


def write_prompt_file(root, name, prompt):
    """Durable prompt: written for every spawn (audit + resume), and the
    stdin source when the binary is a batch file."""
    path = os.path.join(ensure_runtime(root), "prompts", name)
    with open(procs.long_path(path), "w", encoding="utf-8") as f:
        f.write(prompt)
    return path


SHELL_FAMILIES = ("Bash", "PowerShell")


def expand_shell_rules(rules, cfg):
    """Permission rules are matched per TOOL FAMILY: a Bash(...) rule does
    not cover the PowerShell tool, and Windows sessions frequently reach for
    PowerShell -- headless, an unmatched family means denial ("approval that
    never arrives"). Add the other shell's twin for every shell rule, so
    users don't need to know this permission-system quirk. Opt out with
    config "expand_shell_rules": false."""
    if not cfg.get("expand_shell_rules", True):
        return list(rules)
    out = []
    for rule in rules:
        out.append(rule)
        for fam in SHELL_FAMILIES:
            if rule.startswith(fam + "("):
                for twin in SHELL_FAMILIES:
                    if twin != fam:
                        twin_rule = twin + rule[len(fam):]
                        if twin_rule not in rules and twin_rule not in out:
                            out.append(twin_rule)
                break
    return out


def claude_cmd(cfg, args, base):
    model = getattr(args, "model", None) or cfg["model"]
    pm = getattr(args, "permission_mode", None) or cfg["permission_mode"]
    # ladder: --claude-bin > AGENT_TASKS_CLAUDE_BIN > config.local.json >
    # config.json > auto-resolution (which() + exe-unwrap). Config never
    # replaces resolution: a configured NAME still resolves and unwraps.
    bin_arg = (getattr(args, "claude_bin", None)
               or os.environ.get("AGENT_TASKS_CLAUDE_BIN")
               or cfg["claude_bin"])
    bin_argv, is_batch = resolve_claude_bin(
        list(bin_arg) if isinstance(bin_arg, list) else [bin_arg])
    via = cfg.get("prompt_via", "auto")
    use_stdin = {"argv": False, "stdin": True}.get(via, is_batch)
    cmd = [*bin_argv, "-p", "--permission-mode", pm] + base
    # The worker must always be able to run the queue CLI unattended (claim,
    # log, block, finish) -- pre-approve it under both quoting styles a worker
    # might type. Project-specific rules (build/test/git commands the worker
    # needs) come from config "allowed_tools".
    cli = cli_path()
    run = runner_str(cfg)
    # both shell families, both quoting styles: four rules for the queue CLI
    rules = []
    for fam in SHELL_FAMILIES:
        rules += [f'{fam}({run} "{cli}":*)', f"{fam}({run} {cli}:*)"]
    rules += expand_shell_rules(list(cfg["allowed_tools"]), cfg)
    cmd += ["--allowedTools", " ".join(rules)]
    # Queue state is read-only to dispatched workers: deny file-tool writes
    # on the index and the task notes. Only Edit(path) rules are matched by
    # the harness's file permission checks -- Write(path)/NotebookEdit(path)
    # forms are ignored with a warning -- and Edit rules cover ALL
    # file-editing tools, so two Edit rules are the whole fence. Reads stay
    # open; the outbox dir under runtime/ is deliberately NOT covered (it is
    # the sanctioned write surface). Forward slashes so rules match on
    # Windows too.
    qroot = cfg["_root"].replace(os.sep, "/")
    deny = [f"Edit({qroot}/index.json)", f"Edit({qroot}/tasks/**)"]
    cmd += ["--disallowedTools", " ".join(deny)]
    # the queue folder is an additional working directory so the worker's
    # ordinary file tools can write its outbox even from a worktree
    cmd += ["--add-dir", cfg["_root"]]
    if model:
        cmd += ["--model", model]
    cmd += list(cfg["extra_args"])
    return cmd, model, pm, use_stdin


def _revert_preclaim(root, tid, agent):
    """Give the task back if we claimed it but never got a worker running."""
    with tasks.Lock(root):
        index = tasks.load_index(root)
        task = index["tasks"][tid]
        if task.get("assignee") == agent and task["status"] == "in_progress":
            task["status"] = "open"
            task["assignee"] = None
            task.pop("lease_until", None)
            task["updated"] = tasks.now()
            tasks.save_index(root, index)
            tasks.set_note_status(root, tid, "open")
            tasks.append_log(root, tid, "dispatcher",
                             "spawn failed -- reverted pre-claim to open")


def cmd_start(args):
    root = tasks.find_dir()
    cfg = load_config(root)
    repo = repo_of(root)
    index = tasks.load_index(root)
    tid = tasks.resolve_id(index, args.task)

    session_id = str(uuid.uuid4())
    worker_id = f"{tid.lower()}-{session_id[:8]}"
    agent = args.agent_name or worker_id

    # Pre-claim atomically BEFORE spawning: of two concurrent dispatches of
    # the same task, the loser exits right here without burning a session.
    # The worker's prompt tells it to verify the assignment and heartbeat
    # instead of claiming.
    with tasks.Lock(root):
        index = tasks.load_index(root)
        err = tasks.apply_claim(root, index, tid, agent, cfg, force=args.force)
        if err:
            tasks.die(err)
        task = index["tasks"][tid]

    use_worktree = cfg["worktree"] if args.worktree is None else args.worktree
    workdir, branch = repo, None
    try:
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

        if not args.model and task.get("model"):
            args.model = task["model"]  # task > config > claude CLI default
        rt = ensure_runtime(root)
        outbox = outbox_path(root, worker_id)
        with open(procs.long_path(outbox), "w", encoding="utf-8") as f:
            f.write("")  # exists from birth; exactly one writer: the worker
        prompt = build_prompt(root, tid, agent, args.prompt_extra, cfg, outbox)
        prompt_file = write_prompt_file(root, f"{worker_id}.txt", prompt)
        cfg["_root"] = root
        cmd, model, pm, use_stdin = claude_cmd(cfg, args,
                                               ["--session-id", session_id])
        log_path = os.path.join(rt, "logs", f"{worker_id}.out")
        if use_stdin:
            # batch shims (cmd.exe) truncate multi-line argv at the first
            # newline -- the prompt travels on stdin, argv carries one line
            cmd.append(STDIN_POINTER)
            with open(procs.long_path(prompt_file), "rb") as pf:
                proc = spawn(root, workdir, cmd, log_path, agent,
                             outbox=outbox, stdin=pf)
        else:
            cmd.append(prompt)
            proc = spawn(root, workdir, cmd, log_path, agent, outbox=outbox)
    except (Exception, SystemExit):
        _revert_preclaim(root, tid, agent)
        raise

    entry = {"task": tid, "session_id": session_id, "pid": proc.pid,
             "cwd": workdir, "worktree_branch": branch, "model": model,
             "permission_mode": pm, "agent": agent, "started": tasks.now(),
             "outbox": outbox, "resumes": 0}
    with tasks.Lock(root):
        workers = load_workers(root)
        workers[worker_id] = entry
        save_workers(root, workers)
        tasks.append_log(root, tid, "dispatcher",
                         f"dispatched worker {worker_id} "
                         f"(session {session_id}, pid {proc.pid}, "
                         f"model {model or 'default'}, cwd {workdir})")
    print(f"started {worker_id}")
    print(f"  task: {tid}   pid: {proc.pid}   model: {model or '(default)'}   "
          f"permission-mode: {pm}")
    print(f"  cwd:  {workdir}" + (f"   (worktree branch {branch})" if branch else ""))
    print(f"  outbox: {outbox}")
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
    for wid in list(workers):
        maybe_fold(root, workers, wid)
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
        if state == "running" and w.get("outbox"):
            print(f"{'':<24} outbox: {w['outbox']}")


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
    """One merged timeline from both evidence streams: the session transcript
    (tool activity) prefixed [session], and the spawn log (permission
    warnings, CLI-level errors, stub/stderr output) prefixed [spawn]. Deny-
    rule warnings appear beside the tool calls they explain -- previously a
    watcher had to read two files to see that."""
    root = tasks.find_dir()
    workers = load_workers(root)
    wid = resolve_worker(root, workers, args.worker)
    w = workers[wid]
    cfg = load_config(root)
    maybe_fold(root, workers, wid)

    def raw(line):
        return [line.rstrip("\n")] if line.strip() else []

    sources = []  # spawn first: its warnings precede session activity
    spath = os.path.join(root, RUNTIME, "logs", f"{wid}.out")
    if os.path.isfile(spath):
        sources.append(("spawn", spath, raw))
    tpath = find_transcript(w["session_id"])
    if tpath:
        sources.append(("session", tpath, parse_line))
    if not sources:
        tasks.die(f"nothing to watch: no transcript for session "
                  f"{w['session_id']} and no spawn log at {spath}")
    if not tpath:
        print(f"(no transcript found for session {w['session_id']} -- "
              f"showing the spawn log only)")
    print(f"watching {wid} ({worker_state(w)}): "
          + "  ".join(f"[{name}] {path}" for name, path, _ in sources))

    handles = []
    try:
        for name, path, parse in sources:
            f = open(procs.long_path(path), encoding="utf-8", errors="replace")
            events = [f"[{name}] {e}" for line in f for e in parse(line)]
            if not args.from_start:
                events = events[-args.tail:]
            for e in events:
                print(e)
            handles.append([name, f, parse, ""])
        if not args.follow:
            return

        hb_every, last_hb = heartbeat_interval(cfg), 0.0
        probe_at = time.time() + 2.0
        while True:
            got = False
            for h in handles:
                name, f, parse = h[0], h[1], h[2]
                chunk = f.readline()
                while chunk:
                    got = True
                    h[3] += chunk
                    if h[3].endswith("\n"):
                        for e in parse(h[3]):
                            print(f"[{name}] {e}", flush=True)
                        h[3] = ""
                    chunk = f.readline()
            if got:
                continue
            if tpath is None and time.time() >= probe_at:
                # the transcript may appear moments after the session starts
                probe_at = time.time() + 2.0
                tpath = find_transcript(w["session_id"])
                if tpath:
                    f = open(procs.long_path(tpath), encoding="utf-8",
                             errors="replace")
                    handles.append(["session", f, parse_line, ""])
                    print(f"[session] transcript appeared: {tpath}", flush=True)
            if not pid_alive(w["pid"]):
                folded = maybe_fold(root, workers, wid)
                print(f"[worker {wid} exited"
                      + (f"; outbox folded: {folded}]" if folded else "]"))
                report_doctor(root)
                return
            if time.time() - last_hb >= hb_every:
                auto_heartbeat(root, w, cfg)
                last_hb = time.time()
            time.sleep(0.5)
    finally:
        for h in handles:
            h[1].close()


def cmd_wait(args):
    root = tasks.find_dir()
    cfg = load_config(root)
    workers = load_workers(root)
    wid = resolve_worker(root, workers, args.worker)
    w = workers[wid]
    deadline = time.time() + args.timeout if args.timeout else None
    hb_every, last_hb = heartbeat_interval(cfg), 0.0
    while pid_alive(w["pid"]):
        if deadline and time.time() > deadline:
            print(f"timeout: {wid} still running (pid {w['pid']})")
            sys.exit(2)
        if time.time() - last_hb >= hb_every:
            auto_heartbeat(root, w, cfg)
            last_hb = time.time()
        time.sleep(1)
    folded = maybe_fold(root, workers, wid)
    if folded:
        print(f"outbox folded: {folded}")
    report_doctor(root)
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
    if not args.model and w.get("model"):
        args.model = w["model"]  # resume with the model the worker started on
    outbox = w.get("outbox") or outbox_path(root, wid)
    ensure_runtime(root)
    if not os.path.isfile(outbox):
        with open(procs.long_path(outbox), "w", encoding="utf-8") as f:
            f.write("")  # earlier content was folded into the note on exit
    prompt = args.prompt or CONTINUATION_PROMPT.format(
        tid=w["task"], note=tasks.note_path(root, w["task"]), outbox=outbox)
    n = w.get("resumes", 0) + 1
    prompt_file = write_prompt_file(root, f"{wid}.resume{n}.txt", prompt)
    cfg["_root"] = root
    cmd, model, pm, use_stdin = claude_cmd(cfg, args, ["--resume", w["session_id"]])
    log_path = os.path.join(ensure_runtime(root), "logs", f"{wid}.out")
    if use_stdin:
        cmd.append(STDIN_POINTER)
        with open(procs.long_path(prompt_file), "rb") as pf:
            proc = spawn(root, w["cwd"], cmd, log_path, w["agent"],
                         outbox=outbox, stdin=pf)
    else:
        cmd.append(prompt)
        proc = spawn(root, w["cwd"], cmd, log_path, w["agent"], outbox=outbox)
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
                       args.prompt_extra, cfg,
                       outbox_path(root, "<worker-id>")))


# ---------------------------------------------------------------- cli

def main():
    parser = argparse.ArgumentParser(
        prog="dispatch", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", aliases=["run"],
                       help="pre-claim a task, then spawn a headless worker for it")
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
