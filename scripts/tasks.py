#!/usr/bin/env python3
"""agent-tasks -- a file-based task queue for coordinating planner and worker agents.

State lives in a dedicated folder (default: .agent-tasks/) at the root of a project:

    .agent-tasks/
      index.json         metadata for every task (source of truth for status/blockers)
      tasks/TASK-001.md  one markdown note per task (description, notes, work log)

Stdlib only, no dependencies. Safe for concurrent agents: every write goes
through a lock file and lands via atomic rename.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

DIR_ENV = "AGENT_TASKS_DIR"
AGENT_ENV = "AGENT_TASKS_AGENT"
DIR_NAME = ".agent-tasks"
INDEX = "index.json"
EVENTS = "events.jsonl"
HANDOFF = "handoff.md"
SUPERVISOR = "supervisor.json"   # in runtime/: machine-local, never committed
TASKS_SUBDIR = "tasks"
LOCK = ".lock"
LOCK_TIMEOUT = 5.0   # seconds to wait for the lock
LOCK_STALE = 30.0    # a lock older than this is presumed dead and stolen

STATUSES = ["open", "in_progress", "review", "done", "cancelled"]
TERMINAL = {"done", "cancelled"}
PRIORITIES = ["high", "normal", "low"]


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def default_agent(explicit):
    return explicit or os.environ.get(AGENT_ENV) or "agent"


CONFIG_DEFAULTS = {
    # -- queue --
    "runner": None,  # interpreter argv used everywhere a command is composed
                     # (prompts, skills, allowlists, bootstrap). None = detect:
                     # uv if installed, else the best python3 on PATH
    "lease_minutes": 90,  # claim lease length; an expired lease is stealable
    "model_tiers": ["haiku", "sonnet", "opus"],  # ordered cheap -> capable,
                                                 # for next/claim --tier
    "mutex_stale_minutes": 30,  # named mutex (lock/unlock) stale-steal timeout
    # -- supervision (see `await`): a supervising session pays its whole
    # context on every wake, so these govern how rarely it is woken --
    "supervisor_ttl_minutes": 20,   # lease not refreshed this long = abandoned
    "await_poll_seconds": 5,        # how often `await` looks (cheap: stat + tail)
    "await_debounce_seconds": 20,   # after the first hit, keep collecting this
                                    # long so a burst of finishes is ONE wake
    "await_timeout_minutes": 240,   # quiet for this long -> exit 2 ("hand off?")
    # -- dispatcher --
    "worktree": False,          # isolate each worker in a git worktree?
    "worktree_root": None,      # default: sibling "<repo>-worktrees/"
    "model": None,              # default: the claude CLI's own default model
    "permission_mode": "acceptEdits",
    "allowed_tools": [],        # extra permission rules, e.g. ["Bash(pnpm test:*)"]
    "expand_shell_rules": True,  # auto-add the Bash<->PowerShell twin of each
                                 # shell rule (families are matched separately)
    "bootstrap": ".claude/task-worker-bootstrap.py",  # run via runner in fresh worktrees
    "claude_bin": "claude",     # string or argv list (e.g. ["cmd", "/c", "claude"])
    "prompt_via": "auto",       # "argv" | "stdin" | "auto" (stdin iff the
                                # resolved claude binary is a .cmd/.bat shim)
    "extra_args": [],           # extra claude CLI args, e.g. ["--verbose"]
    # -- remote tracker bridge (see README "Linking a remote tracker") --
    # Both hooks are argv lists run with cwd = the repo and a JSON payload on
    # stdin describing the task, the worker, its outbox and workspace. The
    # queue never learns what the remote is: `remote` on a task is an opaque
    # string only the hooks interpret (a vault stem, a Jira key, an issue URL).
    "seed_hook": None,          # before spawn: materialize remote context into
                                # the worker's workspace/seed/ (failure aborts
                                # the dispatch and reverts the pre-claim)
    "prices": {},               # per-model USD/MTok overrides for the dispatcher's
                                # cost estimate: {"<model id>": {input, output,
                                # cache_read, cache_write_5m, cache_write_1h}}
    "sync_hook": None,          # at spawn, every sync_interval_seconds while
                                # supervised (wait/watch), and at fold with the
                                # outcome: push outbox + workspace onward
                                # (best-effort: failures warn, never block)
    "sync_interval_seconds": 120,
    "hook_timeout_seconds": 120,
}


def detect_runner():
    """Interpreter argv when config doesn't pin one. uv is preferred, but the
    scripts are stdlib-only, so any Python >= 3.8 on PATH works."""
    if shutil.which("uv"):
        return ["uv", "run", "python"]
    if os.name == "nt":
        candidates = (["py", "-3"], ["python"], ["python3"])
    else:
        candidates = (["python3"], ["python"])
    for cand in candidates:
        if shutil.which(cand[0]):
            return cand
    return [sys.executable or "python3"]


def load_config(root):
    """Defaults <- config.json (project, committed) <- config.local.json
    (machine, gitignored). The local overlay merges key-by-key and may
    override ANY config key -- machine facts (claude_bin, runner paths)
    don't belong in shared project config."""
    cfg = dict(CONFIG_DEFAULTS)
    for name in ("config.json", "config.local.json"):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    user = json.load(f)
            except ValueError as e:
                die(f"bad {path}: {e}")
            for key in cfg:
                if key in user:
                    cfg[key] = user[key]
    if not cfg["runner"]:
        cfg["runner"] = detect_runner()
    return cfg


def compute_lease(cfg):
    delta = timedelta(minutes=float(cfg["lease_minutes"]))
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def lease_expired(task):
    """A claim is a lease, not a lock: expired means legitimately stealable."""
    return (task.get("status") == "in_progress"
            and bool(task.get("lease_until"))
            and task["lease_until"] < now())


# ---------------------------------------------------------------- storage

def find_dir(require=True):
    """Locate the queue folder: $AGENT_TASKS_DIR, else walk up from cwd."""
    env = os.environ.get(DIR_ENV)
    if env:
        path = os.path.abspath(env)
        if not os.path.isdir(path) and require:
            die(f"{DIR_ENV}={env} does not exist (run `init` first)")
        return path
    cur = os.getcwd()
    while True:
        cand = os.path.join(cur, DIR_NAME)
        if os.path.isdir(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    if require:
        die(f"no {DIR_NAME}/ found in {os.getcwd()} or any parent "
            f"(run `init` first, or set {DIR_ENV})")
    return None


class Lock:
    """Exclusive lock over the queue folder via O_EXCL lock file. Reentrant
    within one process, so helpers like append_log can insist on the lock
    whether or not the caller already holds it."""

    _depth = {}  # lock path -> this process's reentrancy depth

    def __init__(self, root):
        self.path = os.path.join(root, LOCK)

    def __enter__(self):
        if Lock._depth.get(self.path, 0):
            Lock._depth[self.path] += 1
            return self
        deadline = time.time() + LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                Lock._depth[self.path] = 1
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > LOCK_STALE:
                        os.unlink(self.path)  # stale lock from a dead process
                        continue
                except OSError:
                    continue  # lock vanished between checks; retry
                if time.time() > deadline:
                    die(f"could not acquire {self.path} after {LOCK_TIMEOUT}s")
                time.sleep(0.05)

    def __exit__(self, *exc):
        depth = Lock._depth.get(self.path, 1) - 1
        if depth > 0:
            Lock._depth[self.path] = depth
            return
        Lock._depth.pop(self.path, None)
        try:
            os.unlink(self.path)
        except OSError:
            pass


def load_index(root):
    with open(os.path.join(root, INDEX), encoding="utf-8") as f:
        return json.load(f)


def save_index(root, index):
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".index-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
        f.write("\n")
    os.replace(tmp, os.path.join(root, INDEX))


def get_task(index, tid):
    task = index["tasks"].get(tid)
    if task is None:
        die(f"unknown task: {tid}")
    return task


def resolve_id(index, raw):
    """Accept TASK-012, task-012, or bare 12."""
    tasks = index["tasks"]
    if raw in tasks:
        return raw
    up = raw.upper()
    if up in tasks:
        return up
    if raw.isdigit():
        n = int(raw)
        for tid in tasks:
            m = re.match(r"^[A-Z]+-0*(\d+)$", tid)
            if m and int(m.group(1)) == n:
                return tid
    die(f"unknown task: {raw}")


def new_id(index):
    mx = 0
    for tid in index["tasks"]:
        m = re.match(r"^TASK-(\d+)$", tid)
        if m:
            mx = max(mx, int(m.group(1)))
    return f"TASK-{mx + 1:03d}"


def unresolved_blockers(index, task):
    """A blocker naming a task id resolves when that task is done/cancelled.
    Free-text blockers stay until removed with `unblock`."""
    out = []
    for b in task.get("blockers", []):
        dep = index["tasks"].get(b)
        if dep is None or dep["status"] not in TERMINAL:
            out.append(b)
    return out


def held_resources(index, exclude=None):
    """Resource tags currently held: each in_progress task with a live
    (non-expired) lease holds all of its tags."""
    held = {}
    for tid, t in index["tasks"].items():
        if tid == exclude or t["status"] != "in_progress" or lease_expired(t):
            continue
        for tag in t.get("resources", []):
            held.setdefault(tag, tid)
    return held


def tier_allows(cfg, tier, task_model, tid=None):
    """May a --tier worker take a task with this model? Unset model: yes.
    Unknown model names are allowed on tasks but excluded from tier selection
    (we can't rank what we can't find in model_tiers)."""
    if not task_model:
        return True
    tiers = cfg["model_tiers"]
    if task_model not in tiers:
        if tid:
            print(f"note: {tid} excluded from --tier {tier}: model "
                  f"'{task_model}' not in model_tiers {tiers}", file=sys.stderr)
        return False
    return tiers.index(task_model) <= tiers.index(tier)


def check_tier(cfg, tier):
    if tier not in cfg["model_tiers"]:
        die(f"unknown tier '{tier}' (model_tiers: {cfg['model_tiers']})")


def apply_claim(root, index, tid, assignee, cfg, force=False):
    """Claim a task under an already-held Lock. Returns an error message, or
    None on success. Open tasks and expired-lease tasks are claimable; a steal
    of an expired lease is recorded in the work log."""
    task = index["tasks"][tid]
    stolen = None
    if task["status"] == "open":
        pass
    elif lease_expired(task):
        stolen = (task.get("assignee"), task.get("lease_until"))
    elif not force:
        msg = f"{tid} is {task['status']}, not open"
        if task.get("assignee"):
            msg += f" (assignee: {task['assignee']}"
            if task.get("lease_until"):
                msg += f", lease until {task['lease_until']}"
            msg += ")"
        return msg + " -- use --force to take it anyway"
    unresolved = unresolved_blockers(index, task)
    if unresolved and not force:
        return f"{tid} is blocked by: {', '.join(unresolved)} -- use --force to override"
    held = held_resources(index, exclude=tid)
    conflicts = [tag for tag in task.get("resources", []) if tag in held]
    if conflicts and not force:
        tag = conflicts[0]
        holder = index["tasks"][held[tag]]
        return (f"resource '{tag}' is held by {held[tag]} "
                f"({holder.get('assignee')}, in_progress, lease until "
                f"{holder.get('lease_until')}) -- wait for it or --force")
    task["status"] = "in_progress"
    task["assignee"] = assignee
    task["claimed_at"] = now()
    task["lease_until"] = compute_lease(cfg)
    task["updated"] = now()
    save_index(root, index)
    set_note_status(root, tid, "in_progress")
    if stolen:
        append_log(root, tid, assignee,
                   f"stole expired claim (was {stolen[0]}, lease expired {stolen[1]})",
                   kind="steal")
    append_log(root, tid, assignee, "claimed", kind="claim")
    return None


# ---------------------------------------------------------------- notes

def note_path(root, tid):
    return os.path.join(root, TASKS_SUBDIR, f"{tid}.md")


NOTE_TEMPLATE = """---
id: {tid}
title: {title}
status: open
created: {ts}{remote_line}
---

# {tid} -- {title}

## Description

{body}

## Acceptance criteria

{criteria}

## Notes

_(worker scratch space -- findings, decisions, open questions)_

## Work log

"""


def write_note(root, tid, title, body, criteria, ts, remote=None):
    text = NOTE_TEMPLATE.format(
        tid=tid, title=title, ts=ts,
        remote_line=f"\nremote: {remote}" if remote else "",
        body=body or "_(no description yet -- planner should fill this in)_",
        criteria=criteria or "_(none specified)_",
    )
    with open(note_path(root, tid), "w", encoding="utf-8") as f:
        f.write(text)


def set_note_remote(root, tid, remote):
    """Mirror the index's `remote` into the note frontmatter (display-only,
    like `status:` -- the index is the source of truth)."""
    path = note_path(root, tid)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return
    fm = [ln for ln in m.group(1).split("\n") if not ln.startswith("remote:")]
    if remote:
        fm.append(f"remote: {remote}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n" + "\n".join(fm) + "\n---\n" + text[m.end():])


def set_note_status(root, tid, status):
    path = note_path(root, tid)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return
    new, n = re.subn(r"^status: .*$", f"status: {status}", text, count=1, flags=re.M)
    if n:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)


def append_log(root, tid, agent, msg, kind="log", data=None):
    """Append to the Work log AND the event journal. The Work log must stay
    the note's last section. Serialized under the queue lock (reentrant if the
    caller holds it), so concurrent appends can't interleave.

    Every mutation already funnels through here, so hanging the journal off it
    is what keeps the two from disagreeing about what happened."""
    with Lock(root):
        with open(note_path(root, tid), "a", encoding="utf-8") as f:
            f.write(f"- {now()} [{agent}] {msg}\n")
        record_event(root, tid, kind, agent, msg, data)


# ---------------------------------------------------------------- journal

# One append-only line per mutation, so a reader can ask "what changed since
# I last looked?" without re-reading the board and every note. That question
# is the whole cost model of a long-lived planner: its context is re-sent on
# every wake, so the delta has to be cheap and the wakes have to be rare.

def events_path(root):
    return os.path.join(root, EVENTS)


def last_seq(root):
    """Highest sequence number in the journal, read from its tail -- the file
    is append-only, so the last line wins. No full parse, no index write."""
    path = events_path(root)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0
    for line in reversed(tail):
        if not line.strip():
            continue
        try:
            return int(json.loads(line).get("seq", 0))
        except (ValueError, TypeError):
            continue
    return 0


def record_event(root, tid, kind, agent, msg, data=None):
    """Append one journal line. Callers hold the queue lock (every mutation
    does), so sequence numbers need no second lock."""
    rec = {"seq": last_seq(root) + 1, "ts": now(), "task": tid,
           "kind": kind, "agent": agent, "msg": msg}
    if data:
        rec.update(data)
    try:
        with open(events_path(root), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError as e:
        print(f"warning: could not record event: {e}", file=sys.stderr)
    return rec


def read_events(root, after=0, limit=None, task=None, kinds=None):
    """Journal lines with seq > after, oldest first."""
    out = []
    try:
        with open(events_path(root), encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("seq", 0) <= after:
                    continue
                if task and rec.get("task") != task:
                    continue
                if kinds and rec.get("kind") not in kinds:
                    continue
                out.append(rec)
    except OSError:
        return []
    return out[-limit:] if limit else out


def classify_event(rec):
    """Which supervision trigger an event represents, or None for narration.
    Workers narrate constantly and decide rarely; only the decisions are worth
    waking a planner for."""
    kind = rec.get("kind")
    if kind == "status":
        to = rec.get("to")
        if to == "review":
            return "review"
        if to in ("done", "cancelled"):
            return "closed"
        return None
    if kind == "block":
        return "blocked"
    if kind == "create":
        return "create"
    if kind == "ignored":
        # a worker's sentinel was dropped on the floor (task reassigned or
        # otherwise moved out from under it) -- exactly the case a resume
        # decision is needed for, so it rides the existing needs-resume
        # trigger instead of inventing a new one nobody has opted into.
        return "needs-resume"
    return None


# ---------------------------------------------------------------- output

def fmt_row(tid, task, unresolved):
    assignee = task.get("assignee") or "-"
    line = (f"{tid:<10} {task['status']:<12} {task.get('priority', 'normal'):<7} "
            f"{assignee:<14} {task['title']}")
    if unresolved:
        line += f"  [blocked <- {', '.join(unresolved)}]"
    if task.get("model"):
        line += f"  [model {task['model']}]"
    if task.get("resources"):
        line += f"  [resources: {', '.join(task['resources'])}]"
    if lease_expired(task):
        line += "  [lease expired]"
    return line


def task_json(index, tid):
    task = dict(index["tasks"][tid])
    task["id"] = tid
    task["unresolved_blockers"] = unresolved_blockers(index, index["tasks"][tid])
    task["lease_expired"] = lease_expired(index["tasks"][tid])
    return task


# ---------------------------------------------------------------- commands

FOLDER_README = """# agent-tasks queue

Machine-managed task queue shared by planner and worker agents
(https://github.com/{gh}/simple-subagent-dispatcher).

- `index.json` -- source of truth for task **metadata**: status, assignee,
  blockers, priority, tags. Change these via the `tasks.py` CLI only, never by
  hand-editing this file (the CLI serializes concurrent writers).
- `tasks/TASK-NNN.md` -- one note per task. The note body is free-form and
  agents are meant to edit it directly (description, notes, findings) -- that is
  the point of the system. Keep **Work log** as the last section; the CLI
  appends entries to the end of the file.

- `config.json` -- optional per-project dispatcher defaults (this is where a
  project records its own judgment calls). All keys optional:
  `worktree` (false), `worktree_root` (sibling `<repo>-worktrees/`),
  `runner` (interpreter argv composed into worker prompts, allowlists, and
  the bootstrap invocation; unset = auto-detect -- uv if installed, else the
  best python3 on PATH),
  `lease_minutes` (90 -- claim lease length; expired claims are stealable),
  `model_tiers` (["haiku","sonnet","opus"] -- ordering behind `--tier`),
  `mutex_stale_minutes` (30 -- named-mutex stale-steal timeout),
  `supervisor_ttl_minutes` (20 -- an unrefreshed supervisor lease is abandoned),
  `await_poll_seconds` (5), `await_debounce_seconds` (20 -- a burst of worker
  finishes becomes ONE planner wake), `await_timeout_minutes` (240 -- quiet for
  this long and `await` exits 2, suggesting a handoff),
  `model` (claude CLI default), `permission_mode` ("acceptEdits"),
  `allowed_tools` ([] -- extra permission rules for what your workers may run;
  Bash(...)/PowerShell(...) entries get their other-shell twin added
  automatically unless `expand_shell_rules` is false),
  `bootstrap` (".claude/task-worker-bootstrap.py" -- a Python script),
  `claude_bin` ("claude" -- string or argv list), `extra_args` ([]),
  `seed_hook` / `sync_hook` (null -- argv lists bridging a remote tracker:
  seed runs before a worker spawns and fills its workspace `seed/`; sync
  pushes the worker's outbox + workspace onward at spawn, every
  `sync_interval_seconds` (120) while supervised (`dispatch wait`/`watch`
  on one worker, `tasks await` on all of them), and at fold with the
  outcome; both get a JSON payload on stdin; `hook_timeout_seconds` 120).
  A task's `remote` (`create --remote`, `tasks remote ID [REF]`) is an
  opaque string only those hooks interpret.
  `prices` ({{}} -- per-model USD-per-million-token overrides for the
  dispatcher's estimated cost, shipped in the sync payload's `activity`
  block and shown by `dispatch status`: {{"<model id>": {{"input", "output",
  "cache_read", "cache_write_5m", "cache_write_1h"}}}}; unknown models get a
  null cost and raw usage).
- `config.local.json` -- optional machine-local overlay, merged key-by-key
  over `config.json` (gitignored by init). Any key may be overridden; put
  machine facts here (claude_bin path, runner), project policy in
  config.json.
- `events.jsonl` -- append-only journal: one line per mutation (seq, ts,
  task, kind, agent, msg). Read a delta with `tasks since --cursor N` instead
  of re-reading the board and every note; `tasks await` watches it.
- `handoff.md` -- the current planner handoff: queue state, in-flight workers,
  what needs a decision, and the last planner's intent. Written by
  `tasks handoff --write`; read by the next session with `tasks handoff --show`.
  Overwritten each time (the folder's git history keeps the old ones).
- `runtime/` -- machine-local dispatcher state (worker registry, spawn logs,
  `supervisor.json` -- which session is watching the queue); self-gitignored,
  never committed.

Statuses: open -> in_progress -> review -> done (or cancelled).
A blocker that names a task id auto-resolves when that task is done/cancelled;
free-text blockers stay until removed with `unblock`.

This folder belongs in the repo: commit it and task state travels with the
project (with history for free). Projects that prefer not to can gitignore it.
"""


def cmd_init(args):
    """Create the queue folder. Re-runnable: creates whatever is missing and
    never overwrites what exists, so a half-made folder (crashed init, partial
    checkout) heals instead of passing a naive existence check."""
    root = os.path.abspath(os.environ.get(DIR_ENV) or os.path.join(os.getcwd(), DIR_NAME))
    created = []
    os.makedirs(os.path.join(root, TASKS_SUBDIR), exist_ok=True)
    if not os.path.exists(os.path.join(root, INDEX)):
        save_index(root, {"version": 1, "tasks": {}})
        created.append(INDEX)
    readme = os.path.join(root, "README.md")
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8") as f:
            f.write(FOLDER_README.format(gh=args.github or "bs7280"))
        created.append("README.md")
    gi = os.path.join(root, ".gitignore")
    gi_text = ""
    if os.path.exists(gi):
        with open(gi, encoding="utf-8") as f:
            gi_text = f.read()
    if "config.local.json" not in gi_text:
        with open(gi, "a", encoding="utf-8") as f:
            if gi_text and not gi_text.endswith("\n"):
                f.write("\n")
            f.write("config.local.json\n")
        created.append(".gitignore(config.local.json)")
    if created:
        print(f"initialized {root} ({', '.join(created)})")
    else:
        print(f"already initialized: {root}")


def cmd_create(args):
    root = find_dir()
    ts = now()
    agent = default_agent(args.agent)
    with Lock(root):
        index = load_index(root)
        tid = new_id(index)
        entry = {
            "title": args.title,
            "status": "open",
            "priority": args.priority,
            "assignee": None,
            "blockers": [b.strip() for b in (args.blocked_by or "").split(",") if b.strip()],
            "tags": [t.strip() for t in (args.tags or "").split(",") if t.strip()],
            "created": ts,
            "updated": ts,
            "note": f"{TASKS_SUBDIR}/{tid}.md",
        }
        if args.model:
            entry["model"] = args.model
        resources = [r.strip() for r in (args.resources or "").split(",") if r.strip()]
        if resources:
            entry["resources"] = resources
        if args.remote:
            entry["remote"] = args.remote
        index["tasks"][tid] = entry
        save_index(root, index)
        write_note(root, tid, args.title, args.body, args.criteria, ts,
                   remote=args.remote)
        append_log(root, tid, agent, "created",
                   kind="create", data={"title": args.title,
                                        "priority": args.priority,
                                        **({"remote": args.remote} if args.remote else {})})
    print(f"created {tid}  {os.path.relpath(note_path(root, tid))}")


def cmd_remote(args):
    """Get or set a task's remote reference: the opaque handle the project's
    seed/sync hooks use to bridge an outside tracker (a vault stem, an issue
    URL, a Jira key). The queue itself never interprets it."""
    root = find_dir()
    if args.ref is None:
        index = load_index(root)
        tid = resolve_id(index, args.id)
        remote = index["tasks"][tid].get("remote")
        if args.json:
            print(json.dumps({"id": tid, "remote": remote}))
        elif remote:
            print(remote)
        else:
            print(f"{tid} has no remote", file=sys.stderr)
            sys.exit(1)
        return
    with Lock(root):
        index = load_index(root)
        tid = resolve_id(index, args.id)
        task = index["tasks"][tid]
        ref = args.ref.strip()
        if ref in ("", "-", "none"):
            task.pop("remote", None)
            ref = None
        else:
            task["remote"] = ref
        task["updated"] = now()
        save_index(root, index)
        set_note_remote(root, tid, ref)
        append_log(root, tid, default_agent(args.agent),
                   f"remote: {ref or '(cleared)'}", kind="remote",
                   data={"remote": ref})
    print(f"{tid} remote: {ref or '(cleared)'}")


def cmd_list(args):
    root = find_dir()
    index = load_index(root)
    statuses = ([s.strip() for s in args.status.split(",")] if args.status
                else None)
    rows = []
    for tid in sorted(index["tasks"]):
        task = index["tasks"][tid]
        if statuses is not None:
            if task["status"] not in statuses:
                continue
        elif not args.all and task["status"] in TERMINAL:
            continue
        if args.assignee and task.get("assignee") != args.assignee:
            continue
        rows.append(tid)
    if args.json:
        print(json.dumps([task_json(index, tid) for tid in rows], indent=2))
        return
    if not rows:
        print("no matching tasks")
        return
    order = {s: i for i, s in enumerate(STATUSES)}
    prio = {p: i for i, p in enumerate(PRIORITIES)}
    rows.sort(key=lambda t: (order[index["tasks"][t]["status"]],
                             prio.get(index["tasks"][t].get("priority", "normal"), 1), t))
    for tid in rows:
        task = index["tasks"][tid]
        print(fmt_row(tid, task, unresolved_blockers(index, task)))


def cmd_show(args):
    root = find_dir()
    index = load_index(root)
    tid = resolve_id(index, args.id)
    task = index["tasks"][tid]
    path = note_path(root, tid)
    if args.json:
        out = task_json(index, tid)
        out["note_path"] = path
        print(json.dumps(out, indent=2))
        return
    print(fmt_row(tid, task, unresolved_blockers(index, task)))
    if task.get("lease_until"):
        print(f"lease: until {task['lease_until']}"
              + ("  (EXPIRED -- claimable)" if lease_expired(task) else ""))
    if task.get("tags"):
        print(f"tags: {', '.join(task['tags'])}")
    print(f"note: {path}")
    print("-" * 60)
    try:
        with open(path, encoding="utf-8") as f:
            sys.stdout.write(f.read())
    except OSError:
        print("(note file missing)")


def _ready_tasks(index):
    """Claimable tasks (open, or in_progress with an expired lease) with no
    unresolved blockers, best-first."""
    prio = {p: i for i, p in enumerate(PRIORITIES)}
    held = held_resources(index)
    ready = [tid for tid, t in index["tasks"].items()
             if (t["status"] == "open" or lease_expired(t))
             and not unresolved_blockers(index, t)
             and not any(tag in held for tag in t.get("resources", []))]
    ready.sort(key=lambda t: (prio.get(index["tasks"][t].get("priority", "normal"), 1), t))
    return ready


def cmd_next(args):
    root = find_dir()
    cfg = load_config(root)
    if args.tier:
        check_tier(cfg, args.tier)

    def pick(index):
        ready = _ready_tasks(index)
        if args.tier:
            ready = [t for t in ready
                     if tier_allows(cfg, args.tier,
                                    index["tasks"][t].get("model"), t)]
        if not ready:
            print("no ready tasks")
            sys.exit(1)
        return ready[0]

    if args.claim:
        assignee = args.assignee or os.environ.get(AGENT_ENV)
        if not assignee:
            die(f"--claim needs --assignee or ${AGENT_ENV}")
        with Lock(root):
            index = load_index(root)
            tid = pick(index)
            err = apply_claim(root, index, tid, assignee, cfg)
            if err:
                die(err)
            task = index["tasks"][tid]
    else:
        index = load_index(root)
        tid = pick(index)
        task = index["tasks"][tid]
    if args.json:
        print(json.dumps({**task_json(index, tid), "note_path": note_path(root, tid)},
                         indent=2))
    else:
        print(f"{tid}  {task['title']}")
        print(f"note: {note_path(root, tid)}")
        if args.claim:
            print(f"claimed {tid} ({task['assignee']})")


def cmd_claim(args):
    root = find_dir()
    assignee = args.assignee or os.environ.get(AGENT_ENV)
    if not assignee:
        die(f"provide --assignee or set ${AGENT_ENV}")
    cfg = load_config(root)
    with Lock(root):
        index = load_index(root)
        tid = resolve_id(index, args.id)
        if args.tier:
            check_tier(cfg, args.tier)
            model = index["tasks"][tid].get("model")
            if not tier_allows(cfg, args.tier, model, tid):
                die(f"{tid} needs model '{model}', outside your tier "
                    f"'{args.tier}' (model_tiers: {cfg['model_tiers']})")
        err = apply_claim(root, index, tid, assignee, cfg, force=args.force)
        if err:
            die(err)
    print(f"claimed {tid} ({assignee})")


def _set_status(root, raw_id, new_status, agent, summary=None):
    with Lock(root):
        index = load_index(root)
        tid = resolve_id(index, raw_id)
        task = index["tasks"][tid]
        old = task["status"]
        task["status"] = new_status
        if new_status != "in_progress":
            task.pop("lease_until", None)
        task["updated"] = now()
        save_index(root, index)
        set_note_status(root, tid, new_status)
        if summary:
            append_log(root, tid, agent, summary)
        append_log(root, tid, agent, f"status: {old} -> {new_status}",
                   kind="status", data={"from": old, "to": new_status})
    print(f"{tid} status: {old} -> {new_status}")


def cmd_status(args):
    _set_status(find_dir(), args.id, args.new_status, default_agent(args.agent))


def cmd_done(args):
    _set_status(find_dir(), args.id, "done", default_agent(args.agent), args.summary)


def cmd_block(args):
    root = find_dir()
    agent = default_agent(args.agent)
    with Lock(root):
        index = load_index(root)
        tid = resolve_id(index, args.id)
        task = index["tasks"][tid]
        added = [b for b in args.blockers if b not in task["blockers"]]
        task["blockers"].extend(added)
        task["updated"] = now()
        save_index(root, index)
        if added:
            append_log(root, tid, agent, f"blocked on: {', '.join(added)}",
                       kind="block", data={"blockers": added})
    print(f"{tid} blockers: {task['blockers']}")


def cmd_unblock(args):
    root = find_dir()
    agent = default_agent(args.agent)
    with Lock(root):
        index = load_index(root)
        tid = resolve_id(index, args.id)
        task = index["tasks"][tid]
        removed = [b for b in args.blockers if b in task["blockers"]]
        task["blockers"] = [b for b in task["blockers"] if b not in removed]
        task["updated"] = now()
        save_index(root, index)
        if removed:
            append_log(root, tid, agent, f"unblocked: {', '.join(removed)}",
                       kind="unblock", data={"blockers": removed})
    for missing in set(args.blockers) - set(removed):
        print(f"warning: {missing} was not a blocker of {tid}", file=sys.stderr)
    print(f"{tid} blockers: {task['blockers']}")


def cmd_assign(args):
    root = find_dir()
    with Lock(root):
        index = load_index(root)
        tid = resolve_id(index, args.id)
        task = index["tasks"][tid]
        task["assignee"] = args.assignee
        task["updated"] = now()
        save_index(root, index)
        append_log(root, tid, default_agent(args.agent),
                   f"assigned to {args.assignee}", kind="assign",
                   data={"assignee": args.assignee})
    print(f"{tid} assignee: {args.assignee}")


def cmd_delete(args):
    """Remove a task from the index and delete its note. Refuses a claimed
    or running (in_progress) task unless --force -- the whole reason this
    exists is so scrubbing strays never again means hand-editing index.json
    and rm-ing note files by hand."""
    root = find_dir()
    agent = default_agent(args.agent)
    with Lock(root):
        index = load_index(root)
        tid = resolve_id(index, args.id)
        task = index["tasks"][tid]
        claimed = task["status"] == "in_progress"
        if claimed and not args.force:
            who = task.get("assignee") or "unknown"
            die(f"{tid} is in_progress (claimed by {who}) -- "
                f"use --force to delete it anyway")
        del index["tasks"][tid]
        save_index(root, index)
        try:
            os.remove(note_path(root, tid))
        except OSError:
            pass
        msg = "deleted"
        if claimed:
            msg += f" (forced past in_progress claim by {task.get('assignee')})"
        record_event(root, tid, "delete", agent, msg)
    print(f"deleted {tid}")


def cmd_log(args):
    root = find_dir()
    with Lock(root):
        index = load_index(root)
        tid = resolve_id(index, args.id)
        index["tasks"][tid]["updated"] = now()
        save_index(root, index)
        append_log(root, tid, default_agent(args.agent), args.message)
    print(f"logged to {tid}")


def cmd_heartbeat(args):
    root = find_dir()
    cfg = load_config(root)
    assignee = args.assignee or os.environ.get(AGENT_ENV)
    if not assignee:
        die(f"provide --assignee or set ${AGENT_ENV}")
    with Lock(root):
        index = load_index(root)
        tid = resolve_id(index, args.id)
        task = index["tasks"][tid]
        if task["status"] != "in_progress":
            die(f"{tid} is {task['status']}, not in_progress -- nothing to heartbeat")
        if task.get("assignee") != assignee and not args.force:
            die(f"{tid} is assigned to {task.get('assignee')}, not {assignee} -- "
                "your expired claim may have been stolen; stop working on it "
                "(--force extends the lease anyway)")
        task["lease_until"] = compute_lease(cfg)
        task["updated"] = now()
        save_index(root, index)
    print(f"{tid} lease extended to {task['lease_until']}")


def cmd_note(args):
    root = find_dir()
    index = load_index(root)
    tid = resolve_id(index, args.id)
    if not args.append:
        print(note_path(root, tid))
        return
    # --append: the CLI-side equivalent of the dispatched-worker outbox, for
    # humans and capable agents working the queue directly -- a stamped block
    # into ## Notes, under the queue lock, frontmatter untouched.
    agent = default_agent(args.agent)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            block = f.read()
    else:
        block = sys.stdin.read()
    block = block.strip()
    if not block:
        die("nothing to append (empty input)")
    stamped = f"**[{agent} @ {now()}]**\n\n{block}\n"
    with Lock(root):
        path = note_path(root, tid)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"^## Notes\s*$", text, re.M)
        if m:
            rest = text[m.end():]
            nxt = re.search(r"^## ", rest, re.M)
            pos = m.end() + (nxt.start() if nxt else len(rest))
            text = (text[:pos].rstrip("\n") + "\n\n" + stamped + "\n"
                    + text[pos:].lstrip("\n"))
        else:
            wl = re.search(r"^## Work log\s*$", text, re.M)
            if wl:  # keep Work log the last section
                text = (text[:wl.start()].rstrip("\n") + "\n\n## Notes\n\n"
                        + stamped + "\n" + text[wl.start():])
            else:
                text = text.rstrip("\n") + "\n\n## Notes\n\n" + stamped
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        index = load_index(root)
        index["tasks"][tid]["updated"] = now()
        save_index(root, index)
        append_log(root, tid, agent, "notes appended", kind="note")
    print(f"appended to {tid} Notes")


def _runtime_dir(root):
    """Machine-local state: self-gitignored, never committed."""
    rt = os.path.join(root, "runtime")
    os.makedirs(rt, exist_ok=True)
    gi = os.path.join(rt, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w", encoding="utf-8") as f:
            f.write("*\n")
    return rt


def _mutex_path(root, name):
    if not re.match(r"^[A-Za-z0-9._-]+$", name):
        die(f"bad mutex name '{name}' (letters, digits, dot, dash, underscore only)")
    locks = os.path.join(_runtime_dir(root), "locks")
    os.makedirs(locks, exist_ok=True)
    return os.path.join(locks, f"{name}.json")


def _write_mutex(path, agent, cfg):
    delta = timedelta(minutes=float(cfg["mutex_stale_minutes"]))
    data = {"holder": agent, "acquired": now(),
            "stale_after": (datetime.now(timezone.utc) + delta)
                           .strftime("%Y-%m-%dT%H:%M:%SZ")}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".mtx-",
                               suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def cmd_lock(args):
    """Acquire a named mutex (e.g. `lock commit` around a shared-tree commit).
    Single attempt: exit 4 = BUSY with the holder named. A stale lock (holder
    crashed; past mutex_stale_minutes) is stolen automatically."""
    root = find_dir()
    cfg = load_config(root)
    agent = default_agent(args.agent)
    path = _mutex_path(root, args.name)
    with Lock(root):
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    cur = json.load(f)
            except ValueError:
                cur = {}
            if not (cur.get("stale_after") and now() > cur["stale_after"]):
                print(f"BUSY: {args.name} held by {cur.get('holder')} "
                      f"since {cur.get('acquired')}")
                sys.exit(4)
            _write_mutex(path, agent, cfg)
            print(f"locked {args.name} ({agent}) -- stole stale lock from "
                  f"{cur.get('holder')} (acquired {cur.get('acquired')})")
            return
        _write_mutex(path, agent, cfg)
    print(f"locked {args.name} ({agent})")


def cmd_unlock(args):
    root = find_dir()
    agent = default_agent(args.agent)
    path = _mutex_path(root, args.name)
    with Lock(root):
        if not os.path.exists(path):
            print(f"{args.name} is not locked")
            return
        try:
            with open(path, encoding="utf-8") as f:
                cur = json.load(f)
        except ValueError:
            cur = {}
        if cur.get("holder") != agent and not args.force:
            die(f"{args.name} is held by {cur.get('holder')}, not {agent} -- "
                "use --force to break it")
        os.unlink(path)
    print(f"unlocked {args.name} ({agent})")


def _note_frontmatter_status(root, tid):
    """Read the display-only status line from a note. (Nothing else ever
    reads it back -- index.json is authoritative; this exists only so doctor
    can detect drift.)"""
    try:
        with open(note_path(root, tid), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None, False
    m = re.search(r"^status: (.*)$", text, re.M)
    return (m.group(1).strip() if m else None), True


def doctor_findings(root, fix=False):
    """Integrity findings: index vs notes drift, orphan claims, strays.
    Returns (findings, fixed_count). Used by cmd_doctor and by the
    dispatcher, which runs it automatically on worker exit."""
    index = load_index(root)
    findings = []
    fixed = 0

    for tid, task in sorted(index["tasks"].items()):
        note_status, exists = _note_frontmatter_status(root, tid)
        if not exists:
            findings.append(f"{tid}: index entry has no note file ({task['note']})")
            continue
        if note_status is not None and note_status != task["status"]:
            findings.append(f"{tid}: status drift -- index '{task['status']}' vs "
                            f"note frontmatter '{note_status}' (index wins)")
            if fix:
                with Lock(root):
                    set_note_status(root, tid, task["status"])
                fixed += 1

    tasks_dir = os.path.join(root, TASKS_SUBDIR)
    if os.path.isdir(tasks_dir):
        for name in sorted(os.listdir(tasks_dir)):
            if name.endswith(".md") and name[:-3] not in index["tasks"]:
                findings.append(f"{TASKS_SUBDIR}/{name}: note file has no index entry")

    # orphan claims: in_progress + assignee + expired lease + no live worker
    live_tasks = set()
    workers_path = os.path.join(root, "runtime", "workers.json")
    if os.path.isfile(workers_path):
        try:
            with open(workers_path, encoding="utf-8") as f:
                workers = json.load(f)
        except ValueError:
            workers = {}
        try:
            import procs  # same directory; optional for a standalone tasks.py
            for w in workers.values():
                pid = w.get("pid")
                if isinstance(pid, int) and pid > 0 and procs.is_alive(pid):
                    live_tasks.add(w.get("task"))
        except ImportError:
            pass
    for tid, task in sorted(index["tasks"].items()):
        if (task["status"] == "in_progress" and task.get("assignee")
                and lease_expired(task) and tid not in live_tasks):
            findings.append(f"{tid}: orphan claim -- assignee {task['assignee']}, "
                            f"lease expired {task['lease_until']}, no live worker "
                            f"(claimable; next/claim will steal it)")
    cfg = load_config(root)
    sup = load_supervisor(root)
    if sup and not sup.get("retired") and supervisor_stale(sup, cfg):
        findings.append(f"supervisor: {sup.get('agent')} (pid {sup.get('pid')}) "
                        f"still holds the lease but has not refreshed since "
                        f"{sup.get('refreshed')} -- its watcher is gone; a new "
                        f"planner can claim it, or `tasks supervisor release`")

    return findings, fixed


def _handoff_ts(root):
    """When the handoff document was written (its first line carries the
    stamp; the file mtime is the fallback)."""
    path = os.path.join(root, HANDOFF)
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", f.readline())
        if m:
            return m.group(1)
        return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return None


def cmd_doctor(args):
    root = find_dir()
    findings, fixed = doctor_findings(root, fix=args.fix)
    for finding in findings:
        print(finding)
    if args.fix and fixed:
        print(f"fixed {fixed} drifted note(s) from the index")
    if findings:
        print(f"doctor: {len(findings)} finding(s)")
        sys.exit(1)
    print("doctor: clean")



# ------------------------------------------------------------- supervision

# A supervising session (the planner) is expensive to wake: its entire context
# is re-read on every wake, whether that wake carried a decision or a
# heartbeat. Three rules follow, and this section is their implementation:
#   1. wake for decisions only -- never for narration, and never for the
#      supervisor's own writes (a planner shouldn't be woken by itself);
#   2. one supervisor at a time -- a newer session supersedes the older one,
#      which exits instead of lurking and popping on someone else's change;
#   3. a session that is done supervising says so (`handoff --write --retire`),
#      so the next one boots from a small document, not a big transcript.


def fmt_dur(secs):
    if secs < 1:
        return f"{secs:g}s"
    secs = int(secs)
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _age_seconds(ts):
    try:
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - then).total_seconds()


def fmt_age(ts):
    secs = _age_seconds(ts)
    return "?" if secs is None else f"{fmt_dur(secs)} ago"


def parse_duration(text):
    """'45' / '90s' / '20m' / '4h' -> seconds. '0' means no limit."""
    m = re.match(r"^(\d+(?:\.\d+)?)([smh]?)$", str(text).strip().lower())
    if not m:
        die(f"bad duration '{text}' (examples: 45, 90s, 20m, 4h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def supervisor_path(root):
    return os.path.join(_runtime_dir(root), SUPERVISOR)


def load_supervisor(root):
    try:
        with open(supervisor_path(root), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_supervisor(root, data):
    path = supervisor_path(root)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".sup-",
                               suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def supervisor_stale(sup, cfg):
    """An unrefreshed lease means the watcher (or its whole session) is gone."""
    if not sup:
        return True
    age = _age_seconds(sup.get("refreshed") or sup.get("claimed"))
    return age is None or age > float(cfg["supervisor_ttl_minutes"]) * 60


def supervisor_claim(root, cfg, agent, takeover=False):
    """Take the queue's single supervisor slot. The same agent name always
    wins -- that is the same planner re-arming, and the generation bump is
    what retires its previous watcher. A *different*, still-fresh supervisor
    is a conflict unless --takeover, so two sessions never sit watching one
    queue."""
    with Lock(root):
        cur = load_supervisor(root)
        if (cur and not cur.get("retired") and cur.get("agent") != agent
                and not supervisor_stale(cur, cfg) and not takeover):
            print(f"BUSY: {cur.get('agent')} is supervising this queue "
                  f"(pid {cur.get('pid')}, refreshed {fmt_age(cur.get('refreshed'))})"
                  f" -- pass --takeover to supersede it (its watcher then exits), "
                  f"or --no-supervisor to watch without holding the lease")
            sys.exit(4)
        me = {"agent": agent, "pid": os.getpid(),
              "generation": int((cur or {}).get("generation", 0)) + 1,
              "claimed": now(), "refreshed": now(), "wakes": 0,
              "last_wake": None, "retired": None}
        save_supervisor(root, me)
    return me


def supervisor_lost(root, me):
    """Why this watcher should stop, or None to keep watching."""
    cur = load_supervisor(root)
    if not cur:
        return "released -- the supervisor record is gone"
    if cur.get("retired"):
        r = cur["retired"]
        return (f"retired by {r.get('agent')} at {r.get('ts')}"
                + (f" -- {r.get('note')}" if r.get("note") else ""))
    if cur.get("generation") != me["generation"] or cur.get("pid") != me["pid"]:
        return (f"superseded by {cur.get('agent')} (pid {cur.get('pid')}, "
                f"generation {me['generation']} -> {cur.get('generation')}, "
                f"claimed {cur.get('claimed')})")
    return None


def supervisor_update(root, me, **fields):
    """Refresh/annotate the lease, but only while it is still ours."""
    with Lock(root):
        cur = load_supervisor(root)
        if (cur.get("generation") != me["generation"]
                or cur.get("pid") != me["pid"]):
            return False
        cur["refreshed"] = now()
        cur.update(fields)
        save_supervisor(root, cur)
    return True


def retire_supervisor(root, agent, note=None):
    """Mark the slot retired: live watchers exit, and nothing re-arms until a
    new planner claims it."""
    with Lock(root):
        cur = load_supervisor(root) or {"generation": 0}
        cur["retired"] = {"agent": agent, "ts": now(), "note": note}
        cur["refreshed"] = now()
        save_supervisor(root, cur)


def supervisor_line(root, cfg):
    sup = load_supervisor(root)
    if not sup:
        return None
    if sup.get("retired"):
        r = sup["retired"]
        return (f"supervisor: retired by {r.get('agent')} {fmt_age(r.get('ts'))}"
                + (f" -- {r.get('note')}" if r.get("note") else ""))
    wakes = sup.get("wakes", 0)
    line = (f"supervisor: {sup.get('agent')} (pid {sup.get('pid')}, "
            f"{'STALE' if supervisor_stale(sup, cfg) else 'fresh'}, refreshed "
            f"{fmt_age(sup.get('refreshed'))}, {wakes} wake"
            f"{'' if wakes == 1 else 's'})")
    if sup.get("last_wake"):
        lw = sup["last_wake"]
        line += f"\n  last wake: {lw.get('reason')} ({fmt_age(lw.get('ts'))})"
    return line


def dispatcher_workers(root):
    """The dispatcher's registry, when the dispatcher is in use. tasks.py only
    ever reads it."""
    try:
        with open(os.path.join(root, "runtime", "workers.json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        import procs  # same directory; optional for a standalone tasks.py
    except ImportError:
        return False
    return procs.is_alive(pid)


def dispatch_supervisor(root, cfg):
    """The dispatcher's chores for every dispatched worker -- lease
    heartbeats and sync ticks while alive, outbox fold on exit -- when
    dispatch.py sits beside this file. `await` is the planner's supervisor,
    so it must do what `dispatch wait` does for one worker, for all of them:
    otherwise a worker finishing under `await` alone is never folded, its
    clean STATUS: review reads as died-mid-task, and a remote that derives
    liveness from the sync hook sees it stall. None for a standalone
    tasks.py (nothing dispatched, nothing to supervise)."""
    try:
        import dispatch  # same directory; optional for a standalone tasks.py
    except ImportError:
        return None
    return dispatch.BatchSupervisor(root, cfg)


def fold_pending(root, wid):
    """An exited worker whose outbox has not been folded yet. Its fate is
    the fold's call -- review, blocked, or truly died -- so until a
    supervisor folds it, it is not needs-resume material."""
    w = dispatcher_workers(root).get(wid) or {}
    path = w.get("outbox") or os.path.join(root, "runtime", "outbox", f"{wid}.md")
    return os.path.isfile(path)


def worker_snapshot(root, index):
    """(live worker ids, {dead worker id: task id}) -- dead meaning it exited
    while its task was still in_progress, i.e. `dispatch resume` material."""
    live, stuck = set(), {}
    for wid, w in dispatcher_workers(root).items():
        task = index["tasks"].get(w.get("task")) or {}
        if _pid_alive(w.get("pid")):
            live.add(wid)
        elif (task.get("status") == "in_progress"
              and task.get("assignee") == w.get("agent")):
            stuck[wid] = w.get("task")
    return live, stuck


# ---------------------------------------------------------------- since

def cmd_since(args):
    """The cheap delta: what changed after a cursor. A planner that wakes (or
    boots) reads this instead of the board plus every note."""
    root = find_dir()
    cursor = args.cursor if args.cursor is not None else 0
    task = resolve_id(load_index(root), args.task) if args.task else None
    kinds = ({k.strip() for k in args.kind.split(",") if k.strip()}
             if args.kind else None)
    events = read_events(root, after=cursor, task=task, kinds=kinds)
    if args.actionable:
        events = [e for e in events if classify_event(e)]
    if args.limit:
        events = events[-args.limit:]
    head = last_seq(root)
    if args.json:
        print(json.dumps({"from": cursor, "cursor": head, "events": events},
                         indent=2))
        return
    if not events:
        print(f"no events since cursor {cursor}  (cursor: {head})")
        return
    for e in events:
        trig = classify_event(e)
        print(f"{e.get('seq', 0):>5}  {(e.get('ts') or ''):19.19}  "
              f"{e.get('task', ''):<10} {e.get('kind', ''):<8} "
              f"{(e.get('agent') or '-'):<14} {e.get('msg', '')}"
              + (f"   <- {trig}" if trig else ""))
    print(f"cursor: {head}")


# ---------------------------------------------------------------- await

AWAIT_TRIGGERS = ("review", "blocked", "create", "closed", "needs-resume",
                  "drain", "any")
DEFAULT_TRIGGERS = "review,blocked,create,needs-resume"


def _hit_line(trig, rec):
    return (f"  {rec.get('task', '-'):<10} {trig:<12} "
            f"{(rec.get('agent') or '-'):<14} {(rec.get('msg') or '')[:100]}")


def cmd_await(args):
    """Block until the queue holds a decision for the planner, print ONE
    compact digest, and exit. Everything here exists to make wakes rare and
    small: narration is ignored, the supervisor's own writes are ignored, a
    burst of finishes is debounced into a single wake, and a superseded or
    retired watcher exits instead of popping on a future change."""
    root = find_dir()
    cfg = load_config(root)
    agent = default_agent(args.agent)
    wants = set()
    for name in (args.triggers or DEFAULT_TRIGGERS).split(","):
        name = name.strip()
        if name and name not in AWAIT_TRIGGERS:
            die(f"unknown trigger '{name}' (choose from: "
                f"{', '.join(AWAIT_TRIGGERS)})")
        if name:
            wants.add(name)
    poll = float(args.poll if args.poll is not None else cfg["await_poll_seconds"])
    debounce = float(args.debounce if args.debounce is not None
                     else cfg["await_debounce_seconds"])
    timeout = (parse_duration(args.timeout) if args.timeout is not None
               else float(cfg["await_timeout_minutes"]) * 60)

    me = None if args.no_supervisor else supervisor_claim(
        root, cfg, agent, takeover=args.takeover)
    cursor = args.cursor if args.cursor is not None else last_seq(root)
    start_cursor = cursor
    started = time.time()
    deadline = started + timeout if timeout > 0 else None

    # supervisor chores for dispatched workers: heartbeat + sync while alive,
    # fold on exit. Ticked once here, after the cursor is set and before the
    # snapshot, so a worker that finished while nobody watched is folded now
    # and its review/blocked event is the first thing this wake reports --
    # not a "pre-existing" needs-resume.
    chores = dispatch_supervisor(root, cfg)
    chore_out = sys.stderr if args.json else sys.stdout

    def do_chores():
        for line in (chores.tick() if chores else ()):
            print(f"  [dispatch] {line}", file=chore_out, flush=True)

    do_chores()
    index = load_index(root)
    live, stuck = worker_snapshot(root, index)
    # a worker that exited between the chores tick and this snapshot still
    # has its outbox: the next tick folds it, and THAT decides what it is
    known_stuck = {wid for wid in stuck if not (chores and fold_pending(root, wid))}
    saw_live = bool(live)
    print(f"await[{agent}]: {root}")
    print(f"  triggers: {','.join(sorted(wants))}   cursor: {cursor}   "
          f"poll {fmt_dur(poll)}   debounce {fmt_dur(debounce)}   "
          f"timeout {fmt_dur(timeout) if deadline else 'none'}")
    if known_stuck:
        print(f"  pre-existing (not a wake): {len(known_stuck)} worker(s) already "
              f"need resume: {', '.join(sorted(known_stuck))}")
    print(f"  live workers: {', '.join(sorted(live)) if live else 'none'}")
    if chores:
        print("  supervising dispatched workers: heartbeat + sync while alive, "
              "fold on exit")

    def finish(hits):
        by_trig = {}
        for trig, rec in hits:
            by_trig.setdefault(trig, []).append(rec)
        reason = ", ".join(f"{len(v)} {k}" for k, v in sorted(by_trig.items()))
        if me:
            supervisor_update(root, me, wakes=int(load_supervisor(root)
                                                  .get("wakes", 0)) + 1,
                              last_wake={"ts": now(), "reason": reason})
        head = last_seq(root)
        if args.json:
            print(json.dumps({"wake": reason, "waited_seconds": int(time.time() - started),
                              "from_cursor": start_cursor, "cursor": head,
                              "hits": [{"trigger": t, **r} for t, r in hits]},
                             indent=2))
            return
        print(f"WAKE: {reason}  (waited {fmt_dur(time.time() - started)}, "
              f"cursor {start_cursor} -> {head})")
        for trig, rec in hits:
            print(_hit_line(trig, rec))
        nxt = []
        seen = {t for t, _ in hits}
        ids = list(dict.fromkeys(r.get("task") for t, r in hits
                                 if t in ("review", "create") and r.get("task")))
        if ids:
            nxt.append("tasks show " + "; tasks show ".join(ids[:3]))
        if "needs-resume" in seen:
            nxt.append("dispatch.py list   (then: dispatch.py resume <worker>)")
        if "blocked" in seen:
            nxt.append("tasks list  (free-text blockers are questions for you)")
        nxt.append(f"tasks since --cursor {start_cursor}   # the full delta, cheap")
        print("next: " + "\n      ".join(nxt))

    def journal_size():
        try:
            return os.path.getsize(events_path(root))
        except OSError:
            return 0

    hits, fire_at, last_refresh, first = [], None, time.time(), True
    drained = False
    last_size = journal_size()
    while True:
        if me:
            lost = supervisor_lost(root, me)
            if lost:
                print(f"STOP: {lost}")
                print("This session is no longer supervising this queue. Do NOT "
                      "re-arm a watcher.")
                print("If you are holding context worth keeping, write it down "
                      "and stop: `tasks handoff --write --note \"...\"`.")
                sys.exit(4)
            if time.time() - last_refresh >= 30:
                supervisor_update(root, me)
                last_refresh = time.time()

        do_chores()                    # a fold here is journaled: read it below
        size = journal_size()          # append-only: unchanged size, nothing new
        events = []
        if size != last_size:
            last_size = size
            events = read_events(root, after=cursor)
        if events:
            cursor = events[-1].get("seq", cursor)
        for rec in events:
            if rec.get("agent") == agent and "any" not in wants:
                continue  # self-inflicted: never wake a planner with its own writes
            trig = classify_event(rec)
            if "any" in wants and not trig:
                trig = rec.get("kind")
            if trig and (trig in wants or "any" in wants):
                hits.append((trig, rec))

        if {"needs-resume", "drain"} & wants:
            index = load_index(root)
            live, stuck = worker_snapshot(root, index)
            if "needs-resume" in wants:
                for wid in sorted(set(stuck) - known_stuck):
                    if chores and fold_pending(root, wid):
                        continue  # judged by the fold on the next tick
                    hits.append(("needs-resume",
                                 {"task": stuck[wid], "agent": wid, "ts": now(),
                                  "msg": f"worker {wid} exited with {stuck[wid]} "
                                         f"still in_progress -- resume or reassign"}))
                    known_stuck.add(wid)
            if "drain" in wants:
                busy = [t for t in index["tasks"].values()
                        if t["status"] == "in_progress"]
                if live or busy:
                    saw_live = True
                elif (saw_live or first) and not drained:
                    drained = True
                    hits.append(("drain",
                                 {"task": "-", "agent": "-", "ts": now(),
                                  "msg": "no live workers and nothing in_progress "
                                         "-- the batch has drained"}))
        first = False

        if hits and fire_at is None:
            fire_at = time.time() + debounce
        if fire_at is not None and time.time() >= fire_at:
            finish(hits)
            sys.exit(0)
        if deadline and time.time() > deadline and fire_at is None:
            head = last_seq(root)
            if args.json:
                print(json.dumps({"wake": None, "quiet_seconds": int(time.time() - started),
                                  "cursor": head}, indent=2))
            else:
                print(f"QUIET: nothing needed you in {fmt_dur(time.time() - started)} "
                      f"(cursor {head}; live workers: "
                      f"{', '.join(sorted(live)) if live else 'none'})")
                print("hint: a supervising session pays its whole context on every "
                      "wake. If the remaining work is long-running and nothing is "
                      "waiting on your judgment, hand off instead of waiting:")
                print("      tasks handoff --write --retire --note \"<what you know, "
                      "what you would do next>\"")
            sys.exit(2)
        time.sleep(poll)


# ---------------------------------------------------------------- supervisor

def cmd_supervisor(args):
    root = find_dir()
    cfg = load_config(root)
    agent = default_agent(args.agent)
    if args.action == "claim":
        me = supervisor_claim(root, cfg, agent, takeover=args.takeover)
        print(f"supervisor: {agent} (generation {me['generation']}) -- any older "
              f"watcher will exit on its next poll")
        return
    if args.action == "release":
        with Lock(root):
            cur = load_supervisor(root)
            if not cur:
                print("supervisor: none")
                return
            if cur.get("agent") != agent and not args.force:
                die(f"supervisor is {cur.get('agent')}, not {agent} -- "
                    "--force to clear it anyway")
            try:
                os.unlink(supervisor_path(root))
            except OSError:
                pass
        print("supervisor: released")
        return
    if args.action == "retire":
        retire_supervisor(root, agent, args.note)
        print("supervisor: retired -- live `await` watchers exit on their next "
              "poll, and nothing re-arms until a planner claims it")
        return
    if args.json:
        sup = load_supervisor(root)
        print(json.dumps({**sup, "stale": supervisor_stale(sup, cfg)}
                         if sup else {}, indent=2))
        return
    print(supervisor_line(root, cfg) or "supervisor: none (nobody is watching "
                                        "this queue)")


# ---------------------------------------------------------------- handoff

def git_facts(root):
    """Where the repo stood at handoff time -- the next planner starts by
    orienting in the tree, not in a transcript."""
    repo = os.path.dirname(root)

    def git(*a):
        try:
            out = subprocess.run(["git", "-C", repo, *a], capture_output=True,
                                 text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    head = git("rev-parse", "--short", "HEAD")
    if head is None:
        return None
    dirty = [ln for ln in (git("status", "--porcelain") or "").splitlines()
             if ln.strip()]
    return {"branch": git("rev-parse", "--abbrev-ref", "HEAD") or "?",
            "head": head, "subject": git("log", "-1", "--pretty=%s") or "",
            "dirty": len(dirty)}


def compose_handoff(root, cfg, agent, intent):
    index = load_index(root)
    live, stuck = worker_snapshot(root, index)
    workers = dispatcher_workers(root)
    cursor = last_seq(root)
    git = git_facts(root)
    out = []
    add = out.append

    def titles(ids):
        return [f"- `{tid}` {index['tasks'][tid]['title']}" for tid in ids]

    add(f"# Planner handoff -- {now()}")
    add("")
    add(f"- written by: `{agent}`")
    add(f"- cursor: `{cursor}` -- a fresh planner catches up with "
        f"`tasks since --cursor {cursor}`")
    if git:
        add(f"- repo: `{git['branch']}` @ `{git['head']}` -- {git['subject']}"
            + (f" ({git['dirty']} uncommitted file(s))" if git["dirty"]
               else " (clean tree)"))
    add("")

    add("## Needs a decision")
    pending = []
    review = [t for t in sorted(index["tasks"]) if index["tasks"][t]["status"] == "review"]
    for tid in review:
        pending.append(f"- `{tid}` **review** -- {index['tasks'][tid]['title']} "
                       f"(worked by {index['tasks'][tid].get('assignee') or '?'}); "
                       f"verify against its acceptance criteria before `tasks done`")
    for tid in sorted(index["tasks"]):
        task = index["tasks"][tid]
        unresolved = unresolved_blockers(index, task)
        free = [b for b in unresolved if b not in index["tasks"]]
        if free and task["status"] not in TERMINAL:
            pending.append(f"- `{tid}` **blocked** -- {task['title']}; waiting on: "
                           + "; ".join(free))
    for wid, tid in sorted(stuck.items()):
        pending.append(f"- `{tid}` **worker died mid-task** -- resume with "
                       f"`dispatch.py resume {wid}`, or reassign the task")
    out.extend(pending or ["_nothing is waiting on a decision._"])
    add("")

    add("## In flight")
    inflight = []
    for tid in sorted(index["tasks"]):
        task = index["tasks"][tid]
        if task["status"] != "in_progress":
            continue
        wid = next((w for w, d in workers.items()
                    if d.get("task") == tid and w in live), None)
        state = (f"live worker `{wid}`" if wid else
                 "no live dispatched worker" + (" (lease EXPIRED -- claimable)"
                                                if lease_expired(task) else ""))
        inflight.append(f"- `{tid}` {task['title']} -- {task.get('assignee') or '?'}, "
                        f"{state}")
    out.extend(inflight or ["_nothing in progress._"])
    add("")

    add("## Ready to dispatch")
    ready = _ready_tasks(index)[:8]
    out.extend(titles(ready) or ["_nothing ready (all open work is blocked or done)._"])
    add("")

    blocked_on_tasks = [tid for tid in sorted(index["tasks"])
                        if index["tasks"][tid]["status"] not in TERMINAL
                        and [b for b in unresolved_blockers(index, index["tasks"][tid])
                             if b in index["tasks"]]]
    if blocked_on_tasks:
        add("## Waiting on other tasks")
        for tid in blocked_on_tasks:
            deps = [b for b in unresolved_blockers(index, index["tasks"][tid])
                    if b in index["tasks"]]
            add(f"- `{tid}` {index['tasks'][tid]['title']} <- {', '.join(deps)}")
        add("")

    add("## Planner's intent")
    add("")
    add(intent or "_(none recorded -- the machine facts above are all that "
                  "survived this session)_")
    add("")
    add("## Pick this up")
    add("")
    add("```")
    since_cmd = f"tasks since --cursor {cursor} --actionable"
    width = max(len(since_cmd), 30)
    add("tasks handoff --show".ljust(width) + "  # this document")
    add(since_cmd.ljust(width) + "  # what happened after it")
    add("tasks board".ljust(width) + "  # current state")
    add("dispatch.py list".ljust(width) + "  # workers, [NEEDS-RESUME] flags")
    add("```")
    add("")
    add("Claim supervision before arming a watcher, so the previous session's "
        "watcher retires instead of waking on your changes:")
    add("")
    add("```")
    add("tasks await --agent planner   # claims the lease, then waits for a decision")
    add("```")
    return "\n".join(out) + "\n"


def cmd_handoff(args):
    """The ready-to-die protocol. A planner that is out of useful context --
    or facing hours of unattended work -- writes what it knows to a document
    the next session can boot from, and retires its watcher."""
    root = find_dir()
    cfg = load_config(root)
    path = os.path.join(root, HANDOFF)
    if not args.write:
        exists = os.path.isfile(path)
        text = ""
        if exists:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        if args.json:
            print(json.dumps({"path": path, "exists": exists, "text": text,
                              "cursor": last_seq(root)}, indent=2))
            return
        if not exists:
            print("no handoff recorded -- fresh queue, or the last planner "
                  "ended without one. Start from `tasks board`.")
            return
        sys.stdout.write(text)
        return

    agent = default_agent(args.agent)
    intent = args.note
    if args.file:
        intent = (sys.stdin.read() if args.file == "-"
                  else open(args.file, encoding="utf-8").read())
    text = compose_handoff(root, cfg, agent, (intent or "").strip())
    with Lock(root):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        if args.retire:
            retire_supervisor(root, agent, "handoff written")
    print(f"handoff written: {path}")
    if args.retire:
        print("supervisor retired -- live `await` watchers exit on their next "
              "poll; nothing will wake this session again")
    if not (intent or "").strip():
        print("note: no intent recorded. The machine facts are in the document, "
              "but what you were *thinking* is not -- add it with "
              "--note/--file before you stop.")


def cmd_board(args):
    root = find_dir()
    cfg = load_config(root)
    index = load_index(root)
    by_status = {s: [] for s in STATUSES}
    for tid in sorted(index["tasks"]):
        by_status[index["tasks"][tid]["status"]].append(tid)
    blocked = {tid: u for tid, t in index["tasks"].items()
               if t["status"] not in TERMINAL
               and (u := unresolved_blockers(index, t))}
    if args.json:
        sup = load_supervisor(root)
        print(json.dumps({
            "dir": root,
            "cursor": last_seq(root),
            "supervisor": ({**sup, "stale": supervisor_stale(sup, cfg)}
                           if sup else None),
            "handoff": (os.path.join(root, HANDOFF)
                        if os.path.isfile(os.path.join(root, HANDOFF)) else None),
            "counts": {s: len(ids) for s, ids in by_status.items()},
            "by_status": by_status,
            "blocked": blocked,
            "expired_lease": [tid for tid in by_status["in_progress"]
                              if lease_expired(index["tasks"][tid])],
            "resources_held": held_resources(index),
        }, indent=2))
        return
    total = len(index["tasks"])
    print(f"queue: {root}  ({total} task{'s' if total != 1 else ''})")
    expired = [tid for tid in by_status["in_progress"]
               if lease_expired(index["tasks"][tid])]
    for status in ("open", "in_progress", "review"):
        ids = [tid for tid in by_status[status] if tid not in expired]
        if not ids:
            continue
        print(f"{status}:")
        for tid in ids:
            task = index["tasks"][tid]
            assignee = f"({task['assignee']})  " if task.get("assignee") else ""
            marker = f"  [blocked <- {', '.join(blocked[tid])}]" if tid in blocked else ""
            print(f"  {tid}  {assignee}{task['title']}{marker}")
    if expired:
        print("expired lease (claimable):")
        for tid in expired:
            task = index["tasks"][tid]
            print(f"  {tid}  (was {task.get('assignee')})  {task['title']}"
                  f"  lease expired {task['lease_until']}")
    held = held_resources(index)
    if held:
        print("resources held:")
        for tag in sorted(held):
            holder = index["tasks"][held[tag]]
            print(f"  {tag} <- {held[tag]} ({holder.get('assignee')})")
    print(f"done: {len(by_status['done'])}, cancelled: {len(by_status['cancelled'])}"
          f"  (cursor: {last_seq(root)})")
    line = supervisor_line(root, cfg)
    if line:
        print(line)
    if os.path.isfile(os.path.join(root, HANDOFF)):
        print(f"handoff on file: `tasks handoff --show` "
              f"({fmt_age(_handoff_ts(root))})")


# ---------------------------------------------------------------- cli

def main():
    parser = argparse.ArgumentParser(
        prog="tasks", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def agent_flag(p):
        p.add_argument("--agent", help=f"who is acting (default: ${AGENT_ENV} or 'agent')")

    p = sub.add_parser("init", help="create the queue folder in the current directory")
    p.add_argument("--github", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("create", help="create a task")
    p.add_argument("title")
    p.add_argument("--body", help="description (markdown ok)")
    p.add_argument("--criteria", help="acceptance criteria (markdown ok)")
    p.add_argument("--priority", choices=PRIORITIES, default="normal")
    p.add_argument("--tags", help="comma-separated tags")
    p.add_argument("--blocked-by", help="comma-separated blockers (task ids or free text)")
    p.add_argument("--model", help="pin the model this task needs (e.g. opus)")
    p.add_argument("--resources", help="comma-separated exclusive-resource tags "
                                       "(e.g. db-migrations,browser)")
    p.add_argument("--remote", help="opaque reference to this task in an outside "
                                    "tracker, for the project's seed/sync hooks "
                                    "(e.g. a vault stem or an issue URL)")
    agent_flag(p)
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("remote", help="print or set a task's remote-tracker "
                                      "reference (set '-' to clear)")
    p.add_argument("id")
    p.add_argument("ref", nargs="?", help="new reference; omit to print the current one")
    p.add_argument("--json", action="store_true")
    agent_flag(p)
    p.set_defaults(func=cmd_remote)

    p = sub.add_parser("list", help="list tasks (hides done/cancelled unless --all)")
    p.add_argument("--status", help="filter: comma-separated statuses")
    p.add_argument("--assignee")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show a task's metadata and note")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("next", help="pick the best ready task (exit 1 if none)")
    p.add_argument("--claim", action="store_true", help="atomically claim it too")
    p.add_argument("--assignee")
    p.add_argument("--tier", help="only tasks whose model is unset or at/below "
                                  "this model_tiers entry")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("claim", help="claim an open, unblocked task")
    p.add_argument("id")
    p.add_argument("--assignee")
    p.add_argument("--tier", help="refuse if the task's model is above/outside "
                                  "this model_tiers entry")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("status", help="set a task's status")
    p.add_argument("id")
    p.add_argument("new_status", choices=STATUSES)
    agent_flag(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("done", help="mark a task done (reviewer's call)")
    p.add_argument("id")
    p.add_argument("--summary", help="closing summary appended to the work log")
    agent_flag(p)
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("block", help="add blockers (task ids or free text)")
    p.add_argument("id")
    p.add_argument("blockers", nargs="+")
    agent_flag(p)
    p.set_defaults(func=cmd_block)

    p = sub.add_parser("unblock", help="remove blockers")
    p.add_argument("id")
    p.add_argument("blockers", nargs="+")
    agent_flag(p)
    p.set_defaults(func=cmd_unblock)

    p = sub.add_parser("assign", help="set the assignee")
    p.add_argument("id")
    p.add_argument("assignee")
    agent_flag(p)
    p.set_defaults(func=cmd_assign)

    p = sub.add_parser("delete", aliases=["rm"],
                       help="delete a task from the index and its note "
                            "(refuses a claimed/running task unless --force)")
    p.add_argument("id")
    p.add_argument("--force", action="store_true",
                   help="delete even if the task is in_progress (claimed)")
    agent_flag(p)
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("log", help="append a work-log entry to a task's note")
    p.add_argument("id")
    p.add_argument("message")
    agent_flag(p)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("heartbeat", help="extend your claim's lease on a task")
    p.add_argument("id")
    p.add_argument("--assignee")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_heartbeat)

    p = sub.add_parser("note", help="print a task's note path, or --append "
                                    "a stamped block into its Notes section")
    p.add_argument("id")
    p.add_argument("--append", action="store_true",
                   help="append a block (from --file or stdin) into ## Notes")
    p.add_argument("--file", help="read the block from this file instead of stdin")
    agent_flag(p)
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("lock", help="acquire a named mutex (exit 4 = BUSY); "
                                    "stale locks are stolen after a timeout")
    p.add_argument("name")
    agent_flag(p)
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("unlock", help="release a named mutex you hold")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="break someone else's lock")
    agent_flag(p)
    p.set_defaults(func=cmd_unlock)

    p = sub.add_parser("doctor", help="integrity report: index/note drift, "
                                      "orphan claims, strays (exit 1 on findings)")
    p.add_argument("--fix", action="store_true",
                   help="rewrite drifted note frontmatter from the index")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("board", help="status overview of the whole queue")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("since", help="journal delta: what changed after a cursor "
                                     "(the cheap way to catch up)")
    p.add_argument("--cursor", type=int, help="last sequence number you saw "
                                              "(default: 0 -- from the beginning)")
    p.add_argument("--limit", type=int, default=50, help="keep the last N (default 50)")
    p.add_argument("--task", help="only this task's events")
    p.add_argument("--kind", help="comma-separated event kinds "
                                  "(status, block, create, claim, log, ...)")
    p.add_argument("--actionable", action="store_true",
                   help="only events that ask a planner for a decision")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_since)

    p = sub.add_parser("await", help="block until the queue needs a decision, then "
                                     "print ONE digest (exit 0 = wake, 2 = quiet "
                                     "timeout, 4 = superseded/retired)")
    p.add_argument("--for", dest="triggers",
                   help=f"comma-separated triggers (default: {DEFAULT_TRIGGERS}; "
                        f"choices: {', '.join(AWAIT_TRIGGERS)})")
    p.add_argument("--cursor", type=int,
                   help="start from this journal sequence (default: now, i.e. "
                        "ignore everything that already happened)")
    p.add_argument("--timeout", help="give up after this long, e.g. 90m/4h "
                                     "(default: config await_timeout_minutes; 0 = never)")
    p.add_argument("--debounce", type=float,
                   help="seconds to keep collecting after the first hit, so a "
                        "burst of finishes is one wake (default: config)")
    p.add_argument("--poll", type=float, help="seconds between checks (default: config)")
    p.add_argument("--takeover", action="store_true",
                   help="supersede another agent's supervisor lease")
    p.add_argument("--no-supervisor", action="store_true",
                   help="watch without claiming the lease (retires nobody, and "
                        "nobody can retire you)")
    p.add_argument("--json", action="store_true")
    agent_flag(p)
    p.set_defaults(func=cmd_await)

    p = sub.add_parser("supervisor", help="who is watching this queue "
                                          "(show/claim/release/retire)")
    p.add_argument("action", nargs="?", default="show",
                   choices=["show", "claim", "release", "retire"])
    p.add_argument("--note", help="retire: why supervision ended")
    p.add_argument("--takeover", action="store_true",
                   help="claim: supersede another agent's fresh lease")
    p.add_argument("--force", action="store_true", help="release: clear someone else's")
    p.add_argument("--json", action="store_true")
    agent_flag(p)
    p.set_defaults(func=cmd_supervisor)

    p = sub.add_parser("handoff", help="read the planner handoff document, or "
                                       "--write one before this session ends")
    p.add_argument("--show", action="store_true",
                   help="print the document (the default when --write is absent)")
    p.add_argument("--write", action="store_true", help="compose and write it")
    p.add_argument("--note", help="the intent section: what you know that the "
                                  "queue does not, and what you would do next")
    p.add_argument("--file", help="read the intent from this file ('-' = stdin)")
    p.add_argument("--retire", action="store_true",
                   help="with --write: retire supervision too (live watchers exit)")
    p.add_argument("--json", action="store_true")
    agent_flag(p)
    p.set_defaults(func=cmd_handoff)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
