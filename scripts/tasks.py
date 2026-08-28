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
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

DIR_ENV = "AGENT_TASKS_DIR"
AGENT_ENV = "AGENT_TASKS_AGENT"
DIR_NAME = ".agent-tasks"
INDEX = "index.json"
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
    "runner": ["uv", "run", "python"],  # interpreter argv used everywhere a
                                        # command is composed (prompts, skills,
                                        # allowlists, bootstrap)
    "lease_minutes": 90,  # claim lease length; an expired lease is stealable
    "model_tiers": ["haiku", "sonnet", "opus"],  # ordered cheap → capable,
                                                 # for next/claim --tier
    # -- dispatcher --
    "worktree": False,          # isolate each worker in a git worktree?
    "worktree_root": None,      # default: sibling "<repo>-worktrees/"
    "model": None,              # default: the claude CLI's own default model
    "permission_mode": "acceptEdits",
    "allowed_tools": [],        # extra permission rules, e.g. ["Bash(pnpm test:*)"]
    "bootstrap": ".claude/task-worker-bootstrap.py",  # run via runner in fresh worktrees
    "claude_bin": "claude",     # string or argv list (e.g. ["cmd", "/c", "claude"])
    "extra_args": [],           # extra claude CLI args, e.g. ["--verbose"]
}


def load_config(root):
    cfg = dict(CONFIG_DEFAULTS)
    path = os.path.join(root, "config.json")
    if os.path.isfile(path):
        try:
            with open(path) as f:
                user = json.load(f)
        except ValueError as e:
            die(f"bad {path}: {e}")
        for key in cfg:
            if key in user:
                cfg[key] = user[key]
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
    """Exclusive lock over the queue folder via O_EXCL lock file."""

    def __init__(self, root):
        self.path = os.path.join(root, LOCK)

    def __enter__(self):
        deadline = time.time() + LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
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
        try:
            os.unlink(self.path)
        except OSError:
            pass


def load_index(root):
    with open(os.path.join(root, INDEX)) as f:
        return json.load(f)


def save_index(root, index):
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".index-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
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
        return msg + " — use --force to take it anyway"
    unresolved = unresolved_blockers(index, task)
    if unresolved and not force:
        return f"{tid} is blocked by: {', '.join(unresolved)} — use --force to override"
    task["status"] = "in_progress"
    task["assignee"] = assignee
    task["claimed_at"] = now()
    task["lease_until"] = compute_lease(cfg)
    task["updated"] = now()
    save_index(root, index)
    set_note_status(root, tid, "in_progress")
    if stolen:
        append_log(root, tid, assignee,
                   f"stole expired claim (was {stolen[0]}, lease expired {stolen[1]})")
    append_log(root, tid, assignee, "claimed")
    return None


# ---------------------------------------------------------------- notes

def note_path(root, tid):
    return os.path.join(root, TASKS_SUBDIR, f"{tid}.md")


NOTE_TEMPLATE = """---
id: {tid}
title: {title}
status: open
created: {ts}
---

# {tid} — {title}

## Description

{body}

## Acceptance criteria

{criteria}

## Notes

_(worker scratch space — findings, decisions, open questions)_

## Work log

"""


def write_note(root, tid, title, body, criteria, ts):
    text = NOTE_TEMPLATE.format(
        tid=tid, title=title, ts=ts,
        body=body or "_(no description yet — planner should fill this in)_",
        criteria=criteria or "_(none specified)_",
    )
    with open(note_path(root, tid), "w") as f:
        f.write(text)


def set_note_status(root, tid, status):
    path = note_path(root, tid)
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return
    new, n = re.subn(r"^status: .*$", f"status: {status}", text, count=1, flags=re.M)
    if n:
        with open(path, "w") as f:
            f.write(new)


def append_log(root, tid, agent, msg):
    """Append to the Work log. The Work log must stay the note's last section."""
    with open(note_path(root, tid), "a") as f:
        f.write(f"- {now()} [{agent}] {msg}\n")


# ---------------------------------------------------------------- output

def fmt_row(tid, task, unresolved):
    assignee = task.get("assignee") or "-"
    line = (f"{tid:<10} {task['status']:<12} {task.get('priority', 'normal'):<7} "
            f"{assignee:<14} {task['title']}")
    if unresolved:
        line += f"  [blocked ← {', '.join(unresolved)}]"
    if task.get("model"):
        line += f"  [model {task['model']}]"
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

- `index.json` — source of truth for task **metadata**: status, assignee,
  blockers, priority, tags. Change these via the `tasks.py` CLI only, never by
  hand-editing this file (the CLI serializes concurrent writers).
- `tasks/TASK-NNN.md` — one note per task. The note body is free-form and
  agents are meant to edit it directly (description, notes, findings) — that is
  the point of the system. Keep **Work log** as the last section; the CLI
  appends entries to the end of the file.

- `config.json` — optional per-project dispatcher defaults (this is where a
  project records its own judgment calls). All keys optional:
  `worktree` (false), `worktree_root` (sibling `<repo>-worktrees/`),
  `runner` (["uv", "run", "python"] — interpreter argv composed into worker
  prompts, allowlists, and the bootstrap invocation),
  `lease_minutes` (90 — claim lease length; expired claims are stealable),
  `model_tiers` (["haiku","sonnet","opus"] — ordering behind `--tier`),
  `model` (claude CLI default), `permission_mode` ("acceptEdits"),
  `allowed_tools` ([] — extra permission rules for what your workers may run),
  `bootstrap` (".claude/task-worker-bootstrap.py" — a Python script),
  `claude_bin` ("claude" — string or argv list), `extra_args` ([]).
- `runtime/` — machine-local dispatcher state (worker registry, spawn logs);
  self-gitignored, never committed.

Statuses: open → in_progress → review → done (or cancelled).
A blocker that names a task id auto-resolves when that task is done/cancelled;
free-text blockers stay until removed with `unblock`.

This folder belongs in the repo: commit it and task state travels with the
project (with history for free). Projects that prefer not to can gitignore it.
"""


def cmd_init(args):
    root = os.path.abspath(os.environ.get(DIR_ENV) or os.path.join(os.getcwd(), DIR_NAME))
    if os.path.exists(os.path.join(root, INDEX)):
        print(f"already initialized: {root}")
        return
    os.makedirs(os.path.join(root, TASKS_SUBDIR), exist_ok=True)
    save_index(root, {"version": 1, "tasks": {}})
    with open(os.path.join(root, "README.md"), "w") as f:
        f.write(FOLDER_README.format(gh=args.github or "bs7280"))
    print(f"initialized {root}")


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
        index["tasks"][tid] = entry
        save_index(root, index)
        write_note(root, tid, args.title, args.body, args.criteria, ts)
        append_log(root, tid, agent, "created")
    print(f"created {tid}  {os.path.relpath(note_path(root, tid))}")


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
              + ("  (EXPIRED — claimable)" if lease_expired(task) else ""))
    if task.get("tags"):
        print(f"tags: {', '.join(task['tags'])}")
    print(f"note: {path}")
    print("-" * 60)
    try:
        with open(path) as f:
            sys.stdout.write(f.read())
    except OSError:
        print("(note file missing)")


def _ready_tasks(index):
    """Claimable tasks (open, or in_progress with an expired lease) with no
    unresolved blockers, best-first."""
    prio = {p: i for i, p in enumerate(PRIORITIES)}
    ready = [tid for tid, t in index["tasks"].items()
             if (t["status"] == "open" or lease_expired(t))
             and not unresolved_blockers(index, t)]
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
        append_log(root, tid, agent, f"status: {old} → {new_status}")
    print(f"{tid} status: {old} → {new_status}")


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
            append_log(root, tid, agent, f"blocked on: {', '.join(added)}")
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
            append_log(root, tid, agent, f"unblocked: {', '.join(removed)}")
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
        append_log(root, tid, default_agent(args.agent), f"assigned to {args.assignee}")
    print(f"{tid} assignee: {args.assignee}")


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
            die(f"{tid} is {task['status']}, not in_progress — nothing to heartbeat")
        if task.get("assignee") != assignee and not args.force:
            die(f"{tid} is assigned to {task.get('assignee')}, not {assignee} — "
                "your expired claim may have been stolen; stop working on it "
                "(--force extends the lease anyway)")
        task["lease_until"] = compute_lease(cfg)
        task["updated"] = now()
        save_index(root, index)
    print(f"{tid} lease extended to {task['lease_until']}")


def cmd_note(args):
    root = find_dir()
    index = load_index(root)
    print(note_path(root, resolve_id(index, args.id)))


def cmd_board(args):
    root = find_dir()
    index = load_index(root)
    by_status = {s: [] for s in STATUSES}
    for tid in sorted(index["tasks"]):
        by_status[index["tasks"][tid]["status"]].append(tid)
    blocked = {tid: u for tid, t in index["tasks"].items()
               if t["status"] not in TERMINAL
               and (u := unresolved_blockers(index, t))}
    if args.json:
        print(json.dumps({
            "dir": root,
            "counts": {s: len(ids) for s, ids in by_status.items()},
            "by_status": by_status,
            "blocked": blocked,
            "expired_lease": [tid for tid in by_status["in_progress"]
                              if lease_expired(index["tasks"][tid])],
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
            marker = f"  [blocked ← {', '.join(blocked[tid])}]" if tid in blocked else ""
            print(f"  {tid}  {assignee}{task['title']}{marker}")
    if expired:
        print("expired lease (claimable):")
        for tid in expired:
            task = index["tasks"][tid]
            print(f"  {tid}  (was {task.get('assignee')})  {task['title']}"
                  f"  lease expired {task['lease_until']}")
    print(f"done: {len(by_status['done'])}, cancelled: {len(by_status['cancelled'])}")


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
    agent_flag(p)
    p.set_defaults(func=cmd_create)

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

    p = sub.add_parser("note", help="print the path to a task's note file")
    p.add_argument("id")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("board", help="status overview of the whole queue")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_board)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
