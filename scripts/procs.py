#!/usr/bin/env python3
"""Cross-platform process primitives for the dispatcher.

Why this exists: the POSIX idioms the dispatcher wants (signal-0 liveness
probes, SIGTERM to a process group, start_new_session) are missing or actively
dangerous on Windows -- worst of all, os.kill(pid, 0) on Windows does not
probe a process, it unconditionally TERMINATES it. Every process operation in
dispatch.py goes through this module instead.
"""

import os
import subprocess

WINDOWS = os.name == "nt"


def spawn(cmd, cwd, env, stdout):
    """Start a detached process in its own group/session: stdin closed,
    stderr folded into stdout (an open file object), survives the parent."""
    kwargs = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                  stdout=stdout, stderr=subprocess.STDOUT)
    if WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def is_alive(pid):
    """True if a process with this pid is currently running. Never signals it."""
    if WINDOWS:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_tree(pid):
    """Terminate the process and its descendants. Best-effort: silent if the
    process is already gone."""
    if WINDOWS:
        # /T = whole tree; /F = force -- a headless console process has no
        # window to deliver a polite WM_CLOSE to.
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
        return
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
