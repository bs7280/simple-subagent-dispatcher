#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""agent-tasks <-> herdr bridge: make dispatched workers visible in the herdr sidebar.

Ships with the agent-tasks plugin as hooks/herdr_sync.py; canonical copy lives here.

The `agent-tasks` plugin (bs7280/simple-subagent-dispatcher) spawns each worker as a
detached `claude -p` process. herdr (herdr.dev) cannot adopt a process it did not
start, so this script runs as the plugin's `sync_hook` and does two things:

  1. On the worker's first sync it opens a herdr TAB in the repo's workspace
     (find-or-create, matched on label / pane cwd), running
         <dispatch.py> watch <worker-id> --follow
     so the worker's merged transcript+spawn log streams in a pane you can read.
  2. On every sync it pushes the queue's own view of the worker into herdr's
     sidebar via `herdr pane report-agent`:
         start / running      -> working
         exited: review|ended -> idle   (herdr shows "done" until you look at it)
         exited: blocked:*    -> blocked
         exited: died         -> blocked (needs a human either way)
     and on exit shows a herdr notification.

herdr is best-effort: no server running, no `herdr` on PATH, or any herdr error
is logged to stdout and the hook still exits 0. A `--chain <argv...>` runs another
sync hook (e.g. agent-tasks-vault's vault_sync.py) with the same stdin payload
afterwards; its output and exit code are passed through, so the two compose.

Wire it ONCE per machine in the plugin's user-level config
(`~/.config/agent-tasks/config.json`, read by agent-tasks >= 0.10.0); `{plugin}`
expands to the installed plugin root, so the path survives plugin updates:

    {"sync_hook": ["uv", "run", "{plugin}/hooks/herdr_sync.py", "sync"],
     "sync_interval_seconds": 60}

Hook keys accumulate across config layers, so a repo's own tracker hook in
`.agent-tasks/config.local.json` keeps running beside this one. (`--chain <argv...>`
is the pre-0.10 way to compose two hooks and still works.)

Subcommands:
  sync [--chain ARGV...]   the hook (payload JSON on stdin)
  open <repo-path>         find-or-create the herdr workspace for a repo; prints JSON
  doctor                   server status, resolved dispatch.py, workspaces

Env:
  AGENT_TASKS_DISPATCH   path to dispatch.py (default: this plugin's scripts/dispatch.py,
                         else the newest installed plugin cache version)
  HERDR_SYNC_NO_NOTIFY   set to disable the exit notification
  HERDR_SYNC_NO_RESUME   set to not pre-type `claude --resume <session>` into the
                         worker's pane when it exits (Enter continues the worker's
                         conversation interactively, in its cwd)
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import time

HERDR_TIMEOUT = 15
STATE_DIRNAME = "herdr"  # under <queue>/runtime/


class HerdrError(Exception):
    pass


# ------------------------------------------------------------------ herdr CLI

def herdr(*args, timeout=HERDR_TIMEOUT):
    """Run `herdr <args>` and return the parsed `.result` (or {} for empty output).
    Raises HerdrError with herdr's own code/message on any failure."""
    exe = shutil.which("herdr")
    if not exe:
        raise HerdrError("herdr not on PATH")
    try:
        res = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HerdrError(f"herdr {args[0]} timed out after {timeout}s")
    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()
    body = None
    for text in (out, err):
        if text.startswith("{"):
            try:
                body = json.loads(text.splitlines()[0]) if "\n" in text else json.loads(text)
            except json.JSONDecodeError:
                body = None
            if body is not None:
                break
    if body and body.get("error"):
        e = body["error"]
        raise HerdrError(f"{e.get('code', 'error')}: {e.get('message', '')}".strip())
    if res.returncode != 0:
        raise HerdrError(f"herdr {' '.join(args)} exited {res.returncode}: {err or out}")
    return (body or {}).get("result", {}) if body else {}


def server_running():
    try:
        herdr("workspace", "list")
        return True
    except HerdrError as e:
        if "server_not_running" in str(e) or "not on PATH" in str(e):
            return False
        raise


# --------------------------------------------------------------- workspaces

def _real(p):
    return os.path.realpath(os.path.expanduser(p)) if p else p


def find_workspace(repo):
    """Workspace whose label is the repo basename or that has a pane whose cwd is
    inside the repo. Returns workspace_id or None."""
    repo = _real(repo)
    base = os.path.basename(repo.rstrip(os.sep))
    panes = herdr("pane", "list").get("panes", [])
    by_ws = {}
    for p in panes:
        by_ws.setdefault(p["workspace_id"], []).append(p)
    for ws_id, ps in by_ws.items():
        for p in ps:
            cwd = _real(p.get("cwd") or "")
            if cwd == repo or cwd.startswith(repo + os.sep):
                return ws_id
    for ws in herdr("workspace", "list").get("workspaces", []):
        if ws.get("label") == base:
            return ws["workspace_id"]
    return None


def open_workspace(repo):
    """Find-or-create. Returns (workspace_id, created: bool)."""
    ws = find_workspace(repo)
    if ws:
        return ws, False
    base = os.path.basename(_real(repo).rstrip(os.sep))
    res = herdr("workspace", "create", "--cwd", _real(repo), "--label", base, "--no-focus")
    return res["workspace"]["workspace_id"], True


# ------------------------------------------------------------- dispatch.py

def resolve_dispatch():
    """dispatch.py: $AGENT_TASKS_DISPATCH, else the sibling scripts/dispatch.py of
    this plugin checkout, else the newest installed plugin cache version."""
    env = os.environ.get("AGENT_TASKS_DISPATCH")
    if env and os.path.isfile(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    sibling = os.path.join(os.path.dirname(here), "scripts", "dispatch.py")
    if os.path.isfile(sibling):
        return sibling
    cands = glob.glob(os.path.expanduser(
        "~/.claude/plugins/cache/simple-subagent-dispatcher/agent-tasks/*/scripts/dispatch.py"))

    def ver(path):
        v = path.split("/agent-tasks/")[1].split("/")[0]
        return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))
    return sorted(cands, key=ver)[-1] if cands else None


def watch_argv(dispatch, wid):
    runner = ["uv", "run", "python"] if shutil.which("uv") else ["python3"]
    return [*runner, dispatch, "watch", wid, "--follow"]


def watch_command(dispatch, wid):
    return " ".join(sh_quote(a) for a in watch_argv(dispatch, wid))


def sh_quote(s):
    if not s or any(c in s for c in " \t\n'\"$`\\!*?[]{}();<>|&"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


# ------------------------------------------------------------------ state

def state_path(queue, wid):
    d = os.path.join(queue, "runtime", STATE_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{wid}.json")


def load_state(queue, wid):
    try:
        with open(state_path(queue, wid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(queue, wid, st):
    p = state_path(queue, wid)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, p)


# ------------------------------------------------------------------- sync

def herdr_state_for(phase, outcome):
    if phase != "exited":
        return "working"
    o = (outcome or "").lower()
    if o.startswith("blocked") or o == "died":
        return "blocked"
    return "idle"  # review | ended


def outcome_tag(outcome):
    o = (outcome or "").lower()
    if o == "review":
        return "✓ review"
    if o.startswith("blocked"):
        return "⏸ " + (outcome if len(outcome) < 24 else outcome[:21] + "…")
    if o == "died":
        return "✗ died"
    return "· " + (outcome or "ended")


def settle_pane(pane_id, max_wait=12.0, hold=2):
    """Poll `pane get` until herdr's terminal_title for the pane has changed
    from its initial value and then stayed the same for `hold` consecutive
    polls (1 s apart), or max_wait elapses. For a live worker the title
    becomes the watch command and holds; for an exited worker it flips to the
    command and back to the shell prompt, then holds."""
    def title():
        try:
            return herdr("pane", "get", pane_id).get("pane", {}).get("terminal_title", "")
        except HerdrError:
            return None
    first = title()
    changed, same, last = False, 0, first
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(1.0)
        t = title()
        if t != first:
            changed = True
        same = same + 1 if t == last else 0
        last = t
        if changed and same >= hold:
            return True
    return False


def wait_for_shell(pane_id, max_wait=20.0):
    """True once the pane's foreground is no longer the watch command (its
    title stops mentioning ' watch '), i.e. the shell prompt is back."""
    deadline = time.time() + max_wait
    while True:
        try:
            title = herdr("pane", "get", pane_id).get("pane", {}).get("terminal_title", "") or ""
        except HerdrError:
            return False
        if " watch " not in title and "dispatch" not in title:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(1.0)


def pane_alive(pane_id):
    try:
        herdr("pane", "get", pane_id)
        return True
    except HerdrError:
        return False


def do_sync(payload):
    """Returns a one-line summary. Never raises for herdr trouble."""
    w = payload.get("worker") or {}
    wid = w.get("id") or "worker"
    queue = payload.get("queue")
    repo = payload.get("repo") or os.getcwd()
    phase = payload.get("phase") or "running"
    outcome = payload.get("outcome")
    task = payload.get("task") or {}
    tid = task.get("id") or w.get("task") or "?"
    title = (task.get("title") or "").strip()
    act = payload.get("activity") or {}

    if payload.get("event") == "seed":
        return "herdr: seed (nothing to do)"
    if not queue:
        return "herdr: skipped (payload has no queue)"
    try:
        if not server_running():
            return "herdr: skipped (server not running)"
    except HerdrError as e:
        return f"herdr: skipped ({e})"

    st = load_state(queue, wid)
    notes = []
    try:
        # 1. workspace + tab (once; re-create if the tab was closed and the worker lives)
        if not st.get("pane_id") or not pane_alive(st["pane_id"]):
            if st.get("pane_id") and phase == "exited":
                return f"herdr: tab for {wid} was closed; worker exited ({outcome}) -- nothing to show"
            ws_id, created = open_workspace(repo)
            if created:
                notes.append(f"created workspace {ws_id}")
            cwd = w.get("cwd") if w.get("cwd") and os.path.isdir(w["cwd"]) else repo
            label = f"{wid} {tid}"
            res = herdr("tab", "create", "--workspace", ws_id, "--cwd", cwd,
                        "--label", label, "--env", f"AGENT_TASKS_DIR={queue}", "--no-focus")
            pane_id = res["root_pane"]["pane_id"]
            tab_id = res["tab"]["tab_id"]
            st.update({"workspace_id": ws_id, "tab_id": tab_id, "pane_id": pane_id,
                       "label": label, "repo": repo, "task": tid})
            save_state(queue, wid, st)
            dispatch = resolve_dispatch()
            if dispatch:
                herdr("pane", "run", pane_id, watch_command(dispatch, wid))
                notes.append(f"tab {tab_id} watching")
            else:
                herdr("pane", "run", pane_id,
                      f"echo 'herdr_sync: dispatch.py not found; set AGENT_TASKS_DISPATCH'")
                notes.append(f"tab {tab_id} (no dispatch.py found)")
            # herdr clears a reported agent whenever it notices the pane's
            # foreground process changed -- and it notices on its own poll,
            # possibly seconds after `pane run`. Report only once herdr's view
            # of the pane has settled (title changed, then held still).
            settle_pane(pane_id)

        # 2. sidebar state -- reported on EVERY sync, not just on change: herdr's
        # own process detection can wipe a reported agent, and a re-report heals it
        state = herdr_state_for(phase, outcome)
        if phase == "exited":
            detail = outcome_tag(outcome)
        else:
            detail = act.get("last_tool") or ("starting" if phase == "start" else "working")
        msg = f"{tid}" + (f" {title[:48]}" if title else "") + f" — {detail}"
        report = ["pane", "report-agent", st["pane_id"], "--source", "agent-tasks",
                  "--agent", wid, "--state", state, "--message", msg]
        dispatch = resolve_dispatch()
        if dispatch and phase != "exited":
            # herdr relaunches this after a server restart (its RESUME_ARG),
            # so a live worker's watch pane survives `herdr server stop`
            report += ["--", *watch_argv(dispatch, wid)]
        herdr(*report)
        if (st.get("state"), st.get("msg")) != (state, msg):
            st["state"], st["msg"] = state, msg
            save_state(queue, wid, st)
            notes.append(state)

        # 3. exit: relabel the tab, notify once
        if phase == "exited" and not st.get("notified"):
            try:
                herdr("tab", "rename", st["tab_id"], f"{wid} {tid} {outcome_tag(outcome)}")
            except HerdrError as e:
                notes.append(f"rename failed: {e}")
            if not os.environ.get("HERDR_SYNC_NO_NOTIFY"):
                sound = "done" if state == "idle" else "request"
                try:
                    herdr("notification", "show", f"{wid}: {outcome or 'ended'}",
                          "--body", f"{tid} {title}".strip(), "--sound", sound)
                except HerdrError as e:
                    notes.append(f"notify failed: {e}")
            st["notified"] = True
            save_state(queue, wid, st)

        # 4. exit: once the watch process has given the pane back to the shell,
        # pre-type `claude --resume <session>` (no Enter) so one keystroke
        # continues the worker's full conversation interactively, in its cwd
        if phase == "exited" and not st.get("resume_prefilled") and w.get("session_id") \
                and not os.environ.get("HERDR_SYNC_NO_RESUME"):
            if wait_for_shell(st["pane_id"]):
                try:
                    # (herdr drops a reported agent once the pane is back at a
                    # bare shell -- an agent is a process to it -- so an exited
                    # worker is carried by the tab label + notification, not a
                    # sidebar row; re-reporting here does not stick)
                    herdr("pane", "send-text", st["pane_id"],
                          f"claude --resume {w['session_id']}")
                    st["resume_prefilled"] = True
                    save_state(queue, wid, st)
                    notes.append("resume prefilled")
                except HerdrError as e:
                    notes.append(f"prefill failed: {e}")
            else:
                notes.append("watch still running; resume not prefilled")
    except HerdrError as e:
        notes.append(f"error: {e}")
    except (KeyError, TypeError) as e:
        notes.append(f"unexpected herdr response shape: {e!r}")

    return f"herdr: {st.get('pane_id', '?')} " + (", ".join(notes) if notes else "unchanged")


# ------------------------------------------------------------------- CLI

def cmd_sync(argv):
    chain = None
    if "--chain" in argv:
        i = argv.index("--chain")
        chain = argv[i + 1:]
        if not chain:
            print("herdr_sync: --chain needs an argv", file=sys.stderr)
            return 2
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(f"herdr_sync: bad payload: {e}", file=sys.stderr)
        payload = {}

    rc = 0
    if chain:
        res = subprocess.run(chain, input=raw, capture_output=True, text=True)
        sys.stdout.write(res.stdout)
        sys.stderr.write(res.stderr)
        rc = res.returncode
    try:
        summary = do_sync(payload)
    except Exception as e:  # the bridge must never fail the dispatcher
        summary = f"herdr: internal error {e!r}"
    print(summary)
    return rc


def cmd_open(argv):
    if len(argv) != 1:
        print("usage: herdr_sync.py open <repo-path>", file=sys.stderr)
        return 2
    try:
        ws, created = open_workspace(argv[0])
    except HerdrError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps({"workspace_id": ws, "created": created, "repo": _real(argv[0])}))
    return 0


def cmd_doctor(_argv):
    print(f"herdr binary: {shutil.which('herdr') or 'NOT FOUND'}")
    print(f"dispatch.py:  {resolve_dispatch() or 'NOT FOUND (set AGENT_TASKS_DISPATCH)'}")
    try:
        up = server_running()
    except HerdrError as e:
        print(f"server:       error: {e}")
        return 1
    print(f"server:       {'running' if up else 'NOT running (start `herdr` in a terminal)'}")
    if not up:
        return 1
    for ws in herdr("workspace", "list").get("workspaces", []):
        print(f"  {ws['workspace_id']:4} {ws.get('agent_status', '?'):8} "
              f"tabs={ws.get('tab_count')} panes={ws.get('pane_count')}  {ws.get('label')}")
    agents = herdr("agent", "list").get("agents", [])
    for a in agents:
        print(f"  agent {a.get('agent')} {a.get('agent_status')} in {a.get('pane_id')} ({a.get('cwd')})")
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "sync":
        return cmd_sync(rest)
    if cmd == "open":
        return cmd_open(rest)
    if cmd == "doctor":
        return cmd_doctor(rest)
    print(f"herdr_sync: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
