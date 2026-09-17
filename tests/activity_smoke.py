#!/usr/bin/env python3
"""activity.py checks on a fixture transcript: once-per-message usage,
incremental scanning with a partial trailing line, price lookup and
overrides, CLAUDE_CONFIG_DIR-aware transcript discovery. Stdlib only.
Run: python3 tests/activity_smoke.py"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, os.pardir, "scripts")))
import activity  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "worker-transcript.jsonl")


def fail(msg):
    raise SystemExit(f"FAIL: {msg}")


def check(cond, msg):
    if not cond:
        fail(msg)
    print(f"ok: {msg}")


def main():
    state, changed = activity.scan(FIXTURE)
    check(changed, "first scan reports change")
    check(state["turns"] == 3, f"three API turns (unique message ids), got {state['turns']}")
    us = state["usage"]
    check(us == {"input": 6, "output": 1467, "cache_write": 43064, "cache_write_1h": 42564,
                 "cache_read": 141531},
          f"usage counted once per message, 1h writes split out: {us}")
    check(state["tool_calls"] == 2, "two tool calls")
    check(state["last_tool"] == {"name": "Bash", "target": "pnpm --filter web typecheck",
                                 "ts": "2026-09-17T22:23:20.000Z"},
          f"last tool carries name/target/ts: {state['last_tool']}")
    check(state["last_text"] == "Done: typecheck is clean and the filter bar renders. Writing the outbox summary now.",
          f"last text whitespace-collapsed: {state['last_text']!r}")
    check(state["model"] == "claude-sonnet-5" and state["last_event_ts"] == "2026-09-17T22:24:40.000Z",
          "model and last event timestamp")
    check(state["offset"] == os.path.getsize(FIXTURE), "offset lands at EOF")
    again, changed = activity.scan(FIXTURE, state)
    check(not changed and again == state, "rescan of an unchanged file is a no-op")

    # cost: sonnet-5 built-in prices, 1h writes at 2x, 5m writes at 1.25x
    blk = activity.block(state, FIXTURE)
    want = (6 * 2.0 + 1467 * 10.0 + 141531 * 0.2 + 500 * 2.5 + 42564 * 4.0) / 1e6
    check(abs(blk["cost_usd_estimated"] - round(want, 4)) < 1e-9 and blk["pricing"] == "claude-sonnet-5",
          f"cost from the built-in table: {blk['cost_usd_estimated']}")
    check(blk["payload_version"] == activity.PAYLOAD_VERSION and blk["transcript"] == FIXTURE
          and blk["usage"] == us, "block shape")
    over = activity.block(state, FIXTURE, {"claude-sonnet-5": {"output": 100.0}})
    check(over["cost_usd_estimated"] > blk["cost_usd_estimated"], "config prices override a single field")
    unknown = dict(state, model="claude-future-9")
    ub = activity.block(unknown, FIXTURE)
    check(ub["cost_usd_estimated"] is None and ub["pricing"] is None and ub["usage"] == us,
          "unknown model: cost null, raw usage still shipped")
    check(activity.price_for("claude-sonnet-4-5-20250929", {"claude-sonnet-4-5": PRICES_ROW})[0] == "claude-sonnet-4-5",
          "dated snapshot ids fall back to the undated key")
    line = activity.one_liner(blk)
    check("3 turns" in line and "2 tool calls" in line and "Bash: pnpm" in line and "~$" in line,
          f"one-liner: {line}")
    check(activity.one_liner(activity.block(None, None)).endswith("(no transcript found yet)"),
          "one-liner without a transcript")

    # incremental: first half, then the rest plus a partial trailing line
    tmp = tempfile.mkdtemp(prefix="activity-")
    try:
        with open(FIXTURE, "rb") as f:
            raw = f.read()
        lines = raw.split(b"\n")
        part = os.path.join(tmp, "t.jsonl")
        with open(part, "wb") as f:
            f.write(b"\n".join(lines[:4]) + b"\n")
        s1, _ = activity.scan(part)
        check(s1["turns"] == 1 and s1["usage"]["output"] == 447, "partial file: one message so far")
        with open(part, "ab") as f:
            f.write(b"\n".join(lines[4:]))          # the rest (fixture ends with a newline)
            f.write(b'{"type": "assistant", "message": {"id": "msg_4", "usage": {"output_tokens": 5')  # partial
        s2, changed = activity.scan(part, s1)
        check(changed and s2["turns"] == 3 and s2["usage"] == us, "incremental scan matches the one-shot totals")
        check(s2["offset"] < os.path.getsize(part), "partial trailing line is not consumed")
        with open(part, "ab") as f:
            f.write(b'0}, "model": "claude-sonnet-5", "content": []}}\n')
        s3, _ = activity.scan(part, s2)
        check(s3["turns"] == 4 and s3["usage"]["output"] == us["output"] + 50, "completed line is picked up next")
        with open(part, "wb") as f:
            f.write(b"\n".join(lines[:2]) + b"\n")   # truncated/rotated
        s4, _ = activity.scan(part, s3)
        check(s4["turns"] == 1, "a shrunken transcript restarts from zero")

        # discovery honors CLAUDE_CONFIG_DIR, then ~/.claude
        cfg = os.path.join(tmp, "cfg")
        os.makedirs(os.path.join(cfg, "projects", "-enc-oded"))
        shutil.copy(FIXTURE, os.path.join(cfg, "projects", "-enc-oded", "abc-123.jsonl"))
        os.environ["CLAUDE_CONFIG_DIR"] = cfg
        try:
            check(activity.find_transcript("abc-123").endswith("abc-123.jsonl"),
                  "find_transcript looks under CLAUDE_CONFIG_DIR/projects")
            check(activity.find_transcript("") is None, "empty session id finds nothing")
        finally:
            del os.environ["CLAUDE_CONFIG_DIR"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL ACTIVITY SMOKE TESTS PASSED")


PRICES_ROW = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write_5m": 3.75, "cache_write_1h": 6.0}

if __name__ == "__main__":
    main()
