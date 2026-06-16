"""Standalone pty runner for the claude_interactive provider.

Runs ONE interactive ``claude`` turn inside a ConPTY and exits. It is launched as a
short-lived subprocess (by absolute path, NOT imported) so that all pywinpty state lives
in a process that terminates cleanly — driving the pty directly inside the long-lived
``tao run`` process leaves a pywinpty pump thread alive and hangs the host on exit.

Protocol:
    stdin  : one JSON object — {argv, cwd, wrapped_prompt, done_path, out_path,
                                timeout, startup_timeout, rows, cols}
    stdout : one JSON object — {"success": true, "output": "..."}  or
                               {"success": false, "error": "..."}

This module is intentionally self-contained (stdlib + winpty only, no ``src`` imports)
so it can be executed by file path without package context.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time

try:  # optional, Windows-only dependency
    from winpty import PtyProcess
except Exception:  # pragma: no cover - import guard
    PtyProcess = None  # type: ignore[assignment]

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\x1b[=>]")
# Bracketed-paste markers: make the TUI treat a multi-line prompt as one literal paste
# (raw newlines would otherwise submit the prompt at the first '\n').
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"


def _navigate_startup(proc, buf: list[str], startup_timeout: float) -> None:
    """Clear the mandatory startup dialogs by injecting keystrokes.

    - Bypass-permissions warning (default "No, exit") → Down + Enter to pick "Yes, I accept".
    - Folder-trust dialog (new cwd) → Enter.
    Returns once a dialog is cleared / the input bar appears, or on timeout.
    """
    handled: set[str] = set()
    t0 = time.monotonic()
    while time.monotonic() - t0 < startup_timeout:
        time.sleep(1.0)
        flat = re.sub(r"\s+", "", _ANSI.sub("", "".join(buf))).lower()
        if "bypasspermissions" in flat and "yes,iaccept" in flat and "bypass" not in handled:
            proc.write("\x1b[B")
            time.sleep(0.3)
            proc.write("\r")
            handled.add("bypass")
            continue
        trust = "trustthefiles" in flat or ("trust" in flat and "folder" in flat)
        if trust and "trust" not in handled:
            proc.write("\r")
            handled.add("trust")
            continue
        ready = "foragents" in flat or "?forshortcuts" in flat
        if ready or (handled and "yes,iaccept" not in flat):
            time.sleep(1.0)  # small settle before the caller injects the prompt
            return


def _teardown(proc) -> None:
    """Kill the pty child + release the ConPTY handle (no orphan claude.exe)."""
    try:
        proc.terminate(force=True)
    except Exception:
        pass
    try:
        proc.close()
    except Exception:
        pass
    pid = getattr(proc, "pid", None)
    if pid and sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            pass


def run(spec: dict) -> dict:
    """Drive one interactive turn: spawn → navigate → inject → wait sentinel → read output."""
    if PtyProcess is None:
        return {"success": False, "error": "pywinpty is not installed (Windows only)"}

    argv = spec["argv"]
    cwd = spec.get("cwd")
    wrapped_prompt = spec["wrapped_prompt"]
    done_path = spec["done_path"]
    out_path = spec["out_path"]
    timeout = float(spec.get("timeout", 1800))
    startup_timeout = float(spec.get("startup_timeout", 30))
    dims = (int(spec.get("rows", 50)), int(spec.get("cols", 120)))

    proc = PtyProcess.spawn(argv, cwd=cwd, env=dict(os.environ), dimensions=dims)
    buf: list[str] = []
    stop = threading.Event()

    def _reader() -> None:
        while not stop.is_set():
            try:
                data = proc.read(4096)
            except Exception:
                break
            if data:
                buf.append(data)

    threading.Thread(target=_reader, daemon=True).start()
    try:
        _navigate_startup(proc, buf, startup_timeout)
        proc.write(_PASTE_START + wrapped_prompt + _PASTE_END)
        time.sleep(0.6)
        proc.write("\r")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if os.path.exists(done_path):
                break
        else:
            return {
                "success": False,
                "error": f"turn timed out after {timeout:.0f}s (no completion sentinel)",
            }

        if not os.path.exists(out_path):
            return {"success": False, "error": "completed but produced no output file"}
        with open(out_path, encoding="utf-8") as f:
            return {"success": True, "output": f.read()}
    finally:
        stop.set()
        _teardown(proc)


def main() -> None:
    try:
        spec = json.load(sys.stdin)
        result = run(spec)
    except Exception as exc:  # never hang the parent — always emit a JSON verdict
        result = {"success": False, "error": f"pty runner crashed: {exc}"}
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    # Hard-exit: guarantees the subprocess returns even if pywinpty left a pump thread
    # alive (the whole reason this runs out-of-process). stdout is already flushed.
    os._exit(0)


if __name__ == "__main__":
    main()
