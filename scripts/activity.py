#!/usr/bin/env python3
"""Transcript-derived worker activity: what a worker last did, how many API
turns it has taken, its token usage, and an estimated cost -- read from the
session transcript Claude Code writes live (one jsonl line per message
content block, each carrying the message's `usage` and `model`).

The dispatcher owns this so every bridge sees ONE normalized block instead
of parsing the raw log itself:

    {"payload_version": 1, "transcript": "<path or null>",
     "last_event_ts": "…Z", "last_tool": {"name", "target", "ts"} | null,
     "last_text": "…" | null, "turns": N, "tool_calls": N, "model": "…",
     "usage": {"input", "output", "cache_write", "cache_write_1h", "cache_read"},
     "cost_usd_estimated": 0.42 | null, "pricing": "<price-table key>" | null}

Reads are incremental: `scan` parses only the bytes appended since the
stored byte offset and never consumes a partial trailing line, so a tick on
a multi-megabyte transcript costs one stat. The state it returns is what
the dispatcher persists per worker in workers.json.

Accounting: a message's usage is counted once per message id -- the
transcript repeats it on every content-block line of that message (a naive
sum of all lines roughly doubles a tool-heavy run). Raw usage is always
shipped; the cost is an estimate from the price table below, overridable
per project with config `prices` (USD per million tokens).
"""

import glob
import json
import os
import re
import time
from datetime import datetime, timezone

PAYLOAD_VERSION = 1
RECENT_IDS = 16          # message ids remembered for de-duplication (lines of one message are contiguous)
TEXT_LIMIT = 200
TARGET_LIMIT = 120

# USD per million tokens. Cache reads are 0.1x the input price (0.025x on
# Claude Fable 5.1); cache writes 1.25x for the 5-minute TTL, 2x for the
# 1-hour TTL. Anthropic first-party rates, cached 2026-06. A project can
# override or extend this with config `prices` -- the same shape, per model id.
PRICES = {
    "claude-fable-5-1":  {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write_5m": 12.5, "cache_write_1h": 20.0},
    "claude-mythos-5-1": {"input": 10.0, "output": 50.0, "cache_read": 1.0,  "cache_write_5m": 12.5, "cache_write_1h": 20.0},
    "claude-fable-5":    {"input": 10.0, "output": 50.0, "cache_read": 1.0,  "cache_write_5m": 12.5, "cache_write_1h": 20.0},
    "claude-opus-5":     {"input": 5.0,  "output": 25.0, "cache_read": 0.5,  "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "claude-opus-4-8":   {"input": 5.0,  "output": 25.0, "cache_read": 0.5,  "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "claude-opus-4-7":   {"input": 5.0,  "output": 25.0, "cache_read": 0.5,  "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "claude-opus-4-6":   {"input": 5.0,  "output": 25.0, "cache_read": 0.5,  "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "claude-sonnet-5":   {"input": 2.0,  "output": 10.0, "cache_read": 0.2,  "cache_write_5m": 2.5,  "cache_write_1h": 4.0},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0, "cache_read": 0.3,  "cache_write_5m": 3.75, "cache_write_1h": 6.0},
    "claude-haiku-4-5":  {"input": 1.0,  "output": 5.0,  "cache_read": 0.1,  "cache_write_5m": 1.25, "cache_write_1h": 2.0},
}
PRICE_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


def price_for(model, overrides=None):
    """(table key, prices) for a model id -- exact, else with a trailing
    dated snapshot suffix stripped (claude-x-4-5-20250929, claude-x@20250929).
    (None, None) when nothing matches: the cost stays null, usage still ships."""
    if not model:
        return None, None
    table = dict(PRICES)
    for key, row in (overrides or {}).items():
        if isinstance(row, dict):
            base = dict(PRICES.get(key) or {})
            base.update({k: float(v) for k, v in row.items() if k in PRICE_KEYS})
            table[key] = base
    cands = [model]
    m = re.match(r"^(.*?)(?:-|@)\d{8}$", model)
    if m:
        cands.append(m.group(1))
    for c in cands:
        row = table.get(c)
        if row and all(k in row for k in PRICE_KEYS):
            return c, row
    return None, None


def find_transcript(session_id):
    """Claude Code persists <session-id>.jsonl under <config dir>/projects/
    <encoded cwd>/. The cwd encoding is not stable across versions; the
    session id is. Honors CLAUDE_CONFIG_DIR (kept for workers), then ~/.claude."""
    if not session_id:
        return None
    roots = [os.environ.get("CLAUDE_CONFIG_DIR"), os.path.expanduser("~/.claude")]
    hits = []
    for root in roots:
        if root:
            hits += glob.glob(os.path.join(root, "projects", "*", f"{session_id}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def new_state():
    return {"offset": 0, "recent_ids": [], "turns": 0, "tool_calls": 0,
            "model": None, "last_event_ts": None, "last_tool": None, "last_text": None,
            "usage": {"input": 0, "output": 0, "cache_write": 0, "cache_write_1h": 0,
                      "cache_read": 0}}


def _trunc(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _target(inp):
    for key in ("file_path", "command", "pattern", "url", "description", "prompt",
                "query", "path", "notebook_path"):
        if inp.get(key):
            return _trunc(inp[key], TARGET_LIMIT)
    return ""


def _ingest(state, obj):
    kind = obj.get("type")
    if kind not in ("assistant", "user"):
        return
    msg = obj.get("message") or {}
    ts = obj.get("timestamp")
    if ts:
        state["last_event_ts"] = ts
    if kind != "assistant":
        return
    mid = msg.get("id")
    if mid and mid not in state["recent_ids"]:
        state["recent_ids"] = (state["recent_ids"] + [mid])[-RECENT_IDS:]
        state["turns"] += 1
        u = msg.get("usage") or {}
        us = state["usage"]
        us["input"] += int(u.get("input_tokens") or 0)
        us["output"] += int(u.get("output_tokens") or 0)
        us["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
        us["cache_write_1h"] += int((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
        us["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        if msg.get("model"):
            state["model"] = msg["model"]
    for blk in msg.get("content") or []:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "text" and str(blk.get("text", "")).strip():
            state["last_text"] = _trunc(blk["text"], TEXT_LIMIT)
        elif blk.get("type") == "tool_use":
            state["tool_calls"] += 1
            state["last_tool"] = {"name": blk.get("name") or "?",
                                  "target": _target(blk.get("input") or {}), "ts": ts}


def scan(path, state=None):
    """Parse what was appended to `path` since state['offset']. Returns
    (state, changed). A shrunken file (rotated, truncated) restarts from
    zero; a partial trailing line waits for the next scan."""
    state = json.loads(json.dumps(state)) if state else new_state()
    try:
        size = os.path.getsize(path)
    except OSError:
        return state, False
    if size < state["offset"]:
        state = new_state()
    if size == state["offset"]:
        return state, False
    with open(path, "rb") as f:
        f.seek(state["offset"])
        buf = f.read()
    end = buf.rfind(b"\n")
    if end < 0:
        return state, False
    for raw in buf[:end].split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            _ingest(state, obj)
    state["offset"] += end + 1
    return state, True


def cost_of(usage, price):
    """USD for a usage dict at a price row; 5-minute writes are the cache
    writes not attributed to the 1-hour TTL."""
    cw1h = usage.get("cache_write_1h", 0)
    cw5m = max(0, usage.get("cache_write", 0) - cw1h)
    return (usage.get("input", 0) * price["input"]
            + usage.get("output", 0) * price["output"]
            + usage.get("cache_read", 0) * price["cache_read"]
            + cw5m * price["cache_write_5m"] + cw1h * price["cache_write_1h"]) / 1e6


def block(state, transcript, prices=None):
    """The normalized payload block for a state (see the module docstring)."""
    state = state or new_state()
    key, price = price_for(state.get("model"), prices)
    cost = round(cost_of(state["usage"], price), 4) if price else None
    return {"payload_version": PAYLOAD_VERSION, "transcript": transcript,
            "last_event_ts": state.get("last_event_ts"),
            "last_tool": state.get("last_tool"), "last_text": state.get("last_text"),
            "turns": state.get("turns", 0), "tool_calls": state.get("tool_calls", 0),
            "model": state.get("model"), "usage": dict(state["usage"]),
            "cost_usd_estimated": cost, "pricing": key}


def _age(ts):
    if not ts:
        return None
    try:
        then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    secs = max(0, (datetime.now(timezone.utc) - then).total_seconds())
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs // 60)}m ago"
    return f"{secs / 3600:.1f}h ago"


def fmt_tokens(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1000 else str(n)


def one_liner(act):
    """`list`-sized rendering of a block."""
    if not act or not act.get("transcript"):
        return "activity: (no transcript found yet)"
    parts = [f"{act['turns']} turns", f"{act['tool_calls']} tool calls"]
    us = act["usage"]
    parts.append(f"out {fmt_tokens(us['output'])} / cache w {fmt_tokens(us['cache_write'])} r {fmt_tokens(us['cache_read'])}")
    cost = act.get("cost_usd_estimated")
    parts.append(f"~${cost:.2f}" if cost is not None else f"cost ? ({act.get('model') or 'model unknown'})")
    age = _age(act.get("last_event_ts"))
    last = act.get("last_tool")
    if last:
        tail = f"{last['name']}" + (f": {_trunc(last['target'], 60)}" if last.get("target") else "")
        parts.append((f"{age} " if age else "") + tail)
    elif act.get("last_text"):
        parts.append((f"{age} " if age else "") + f"\"{_trunc(act['last_text'], 60)}\"")
    return "activity: " + " · ".join(parts)
