"""
SessionStart hook that records which profile started or resumed a Claude Code session.

The installer registers `cc-auth-switch session-start` in every profile's settings.json. Claude Code runs it on
startup, resume, clear, compact and fork, with the hook payload on stdin and the wrapper's
environment. The hook appends one JSON line to

    <transcript_path without .jsonl>/auth-switch-profile-starts.jsonl

Tools such as cc-blackbox read that file to tell which profile owns a session, which they cannot
see in Claude Code's own records once several profiles share one sessions/ directory. The line
format is a contract with those readers:

    {"v": 1, "ts": <epoch ms>, "session_id": ..., "pid": ..., "source": ..., "profile": ..., "config_dir": ..., "cwd": ...}

Claude Code puts the stdout of a SessionStart hook into the model context, and shows stderr to the
user when the exit code is not 0. The hook therefore prints nothing, always exits 0, and writes
errors to a log file in the profile directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

LOG_NAME = "auth-switch-profile-starts.jsonl"
ERROR_LOG_NAME = "auth-switch-session-start.log"
FORMAT_VERSION = 1
# A claude process sits a few levels above the hook at most (claude -> shell -> hook).
MAX_PARENT_DEPTH = 16


def config_dir() -> Path:
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(value) if value else Path.home() / ".claude"


def parent_pid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # The command name in parentheses may contain spaces, so split after the last ')'.
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        result = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        return int(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def find_claude_pid(sessions_dir: Path) -> int | None:
    """Return the pid whose sessions/<pid>.json is the record of this session.

    Claude Code sets CLAUDE_PID for hooks. Without it, walk up the parent processes to the first
    one that has a record.
    """
    value = os.environ.get("CLAUDE_PID", "")
    if value.isdigit():
        return int(value)
    pid: int | None = os.getppid()
    for _ in range(MAX_PARENT_DEPTH):
        if pid is None or pid <= 1:
            return None
        if (sessions_dir / f"{pid}.json").exists():
            return pid
        pid = parent_pid(pid)
    return None


def build_line(payload: dict, now_ms: int) -> dict:
    return {
        "v": FORMAT_VERSION,
        "ts": now_ms,
        "session_id": payload.get("session_id"),
        "pid": find_claude_pid(config_dir() / "sessions"),
        "source": payload.get("source"),
        "profile": os.environ.get("FP_CC_AUTH_SWITCH_PROFILE") or None,
        "config_dir": os.environ.get("CLAUDE_CONFIG_DIR") or None,
        "cwd": payload.get("cwd") or os.getcwd(),
    }


def record(stdin_text: str) -> None:
    payload = json.loads(stdin_text)
    transcript_path = payload.get("transcript_path") or ""
    if not transcript_path:
        # Remote and storage modes have no local transcript, so there is no place for the line.
        return
    if not transcript_path.endswith(".jsonl"):
        raise ValueError(f"transcript_path does not end with .jsonl: {transcript_path!r}")

    # Claude Code creates <session-id>/ lazily, the first time a tool result or subagent needs it.
    session_dir = Path(transcript_path[: -len(".jsonl")])
    session_dir.mkdir(parents=True, exist_ok=True)

    line = json.dumps(build_line(payload, int(time.time() * 1000)), ensure_ascii=False) + "\n"
    # One O_APPEND write per line, so concurrent hooks of the same session never interleave.
    fd = os.open(session_dir / LOG_NAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def log_error(stdin_text: str) -> None:
    try:
        with open(config_dir() / ERROR_LOG_NAME, "a") as log:
            log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} stdin={stdin_text[:2000]!r}\n{traceback.format_exc()}\n")
    except OSError:
        pass


def main() -> int:
    stdin_text = ""
    try:
        # Read stdin to the end, or Claude Code may report a broken pipe.
        stdin_text = sys.stdin.read()
        record(stdin_text)
    except Exception:  # noqa: BLE001 -- the hook must never disturb the session
        log_error(stdin_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
