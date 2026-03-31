"""Command runner — execute shell commands and report results.


Used by auto-fix and validation phases. Runs commands in the workspace
directory, captures output and exit code.

Trust model for shell=True: commands are user-configured in the task config.
The user controls what commands run (see step_runner.py module docstring).
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)


def run_gate_command(
    command: str,
    workspace_path: str,
    *,
    timeout: int = 120,
) -> tuple[bool, str]:
    """Run a single shell command and return (passed, output).

    Args:
        command: Shell command to execute.
        workspace_path: Working directory for the subprocess.
        timeout: Maximum seconds before killing the process.

    Returns:
        Tuple of (passed, output) where passed is True if exit code == 0.
    """
    logger.debug("running command: %s (cwd=%s)", command, workspace_path)

    proc = subprocess.Popen(  # noqa: S602 — shell=True trust model documented above
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=workspace_path,
    )

    try:
        stdout_bytes, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            logger.warning("process %d did not exit after kill+terminate", proc.pid)
        output = f"command timed out after {timeout}s"
        logger.debug("command timed out: %s", command)
        return False, output

    output = stdout_bytes.decode("utf-8", errors="replace").strip()
    passed = proc.returncode == 0

    logger.debug(
        "command exit=%d: %s (output: %.200s)",
        proc.returncode,
        command,
        output,
    )
    return passed, output
