#!/usr/bin/env python3
"""Smoke test for scripts/procs.py on the current platform.

Stdlib only, cross-platform. Run: uv run python tests/procs_smoke.py
"""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, os.pardir, "scripts")))
import procs  # noqa: E402


def fail(msg):
    raise SystemExit(f"FAIL: {msg}")


def main():
    with tempfile.TemporaryFile() as out:
        child = procs.spawn([sys.executable, "-c", "import time; time.sleep(60)"],
                            cwd=os.getcwd(), env=dict(os.environ), stdout=out)
        try:
            if not procs.is_alive(child.pid):
                fail("fresh child should read alive")
            # the Windows trap this shim exists for: a liveness PROBE must
            # never terminate the target
            time.sleep(0.3)
            if not procs.is_alive(child.pid):
                fail("liveness probe killed the child")
            if not procs.WINDOWS:
                if os.getpgid(child.pid) == os.getpgid(0):
                    fail("child not detached into its own process group")

            procs.terminate_tree(child.pid)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                fail("terminate_tree did not stop the child")
        finally:
            try:
                child.kill()
            except OSError:
                pass
            child.wait()

    if procs.is_alive(child.pid):
        fail("exited child still reads alive")
    procs.terminate_tree(child.pid)  # already-gone pid: must not raise

    for _ in range(3):
        if not procs.is_alive(os.getpid()):
            fail("own pid should read alive")

    print("ALL PROCS SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
