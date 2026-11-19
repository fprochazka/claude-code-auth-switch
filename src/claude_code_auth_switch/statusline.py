"""
`cc-auth-switch statusline [--delegate COMMAND]`, the status line that `start` puts in front of the user's own.

Claude Code sends the account's rate limits only to the status line command, as `rate_limits` in the JSON on its stdin.
`start` passes this command as the session's statusLine. It merges the payload's `rate_limits` into

    <CLAUDE_CONFIG_DIR>/auth-switch-rate-limits.json

and then replaces itself with `/bin/sh -c COMMAND`, the user's own status line, with the same bytes on stdin. The user's
command so runs as if Claude Code had started it: same process, environment, working directory and stdin, its own
output and exit status. Without --delegate it prints nothing.

Claude Code runs the status line after every response and debounces only to 300 ms, so this path imports nothing
beyond the standard library modules it needs. Nothing here may break the status line: a payload without
`rate_limits`, a broken file or a failed write is reported on stderr, which Claude Code shows only in `claude --debug`,
and the user's command runs anyway.

The file format is a contract with readers such as cc-blackbox:

    {"v": 1, "profile": ..., "written_at": <epoch ms>, "session_id": ..., "model": ..., "claude_version": ...,
     "windows": {"<name>": {"used_percentage": ..., "resets_at": <epoch s>, ..., "observed_at": <epoch ms>,
                            "changed_at": <epoch ms>, "session_id": ...}}}

Every session of the profile writes to the same file, and an idle session keeps reporting the values of its own last
response. Within a window the usage only grows, and the next window resets at least one window length later, so the
merge keeps the higher value of a window, takes a window that resets later, ignores one that resets earlier, and keeps a
window that the payload no longer has: Claude Code drops a window once its reset time has passed.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import signal
import sys
import time
from collections.abc import Iterator
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover -- not on Windows, which the launchers do not support
    fcntl = None  # type: ignore[assignment]

RATE_LIMITS_FILE = "auth-switch-rate-limits.json"
# Held around the read, merge and replace, so that two sessions writing at once do not lose an update.
LOCK_FILE = "auth-switch-rate-limits.lock"
# How long to wait for the lock before writing without it: the status line must not wait for another session.
LOCK_WAIT_SECONDS = 0.05
FORMAT_VERSION = 1
SUBCOMMAND = "statusline"
DELEGATE_FLAG = "--delegate"
# Claude Code sends a few kilobytes; a far larger input is passed on to the user's command but not parsed.
MAX_INPUT_BYTES = 1024 * 1024
# A window whose reset time moved by less than half its length is the same window: Claude Code reports a slightly
# different reset time now and then. A window of unknown length, such as a spend limit, is the same one within a few
# minutes, and a new one when it resets later than that.
WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
UNKNOWN_WINDOW_TOLERANCE_SECONDS = 300
# Values that did not change are written again only after this long, so that dozens of sessions refreshing their status
# line do not rewrite the file every few hundred milliseconds, and observed_at still says the values are current.
REFRESH_OBSERVED_MS = 60_000
# Claude Code's resets_at is in epoch seconds; a value outside 2000-2100 is drifted data.
RESETS_AT_RANGE = (946_684_800, 4_102_444_800)
OUR_WINDOW_KEYS = ("observed_at", "changed_at", "session_id")


def warn(message: str) -> None:
    # Not even a closed stderr may stop the user's status line.
    with contextlib.suppress(Exception):
        print(f"cc-auth-switch statusline: {message}", file=sys.stderr)


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def valid_window(name: str, window: object) -> dict[str, Any] | None:
    """The window as Claude Code sent it, or None when its percentage or reset time is unusable."""
    if not isinstance(window, dict):
        return None
    used = number(window.get("used_percentage"))
    # A spend limit can go above 100; the subscription windows cannot.
    if used is None or used < 0 or (name in WINDOW_SECONDS and used > 100):
        return None
    resets_at = number(window.get("resets_at"))
    if resets_at is not None and not RESETS_AT_RANGE[0] <= resets_at <= RESETS_AT_RANGE[1]:
        return None
    return {key: value for key, value in window.items() if key not in OUR_WINDOW_KEYS}


def payload_windows(payload: object) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("rate_limits"), dict):
        return {}
    windows: dict[str, dict[str, Any]] = {}
    for name, window in payload["rate_limits"].items():
        if isinstance(name, str) and (valid := valid_window(name, window)) is not None:
            windows[name] = valid
    return windows


def compare(name: str, new: dict[str, Any], stored: dict[str, Any]) -> str:
    """ "newer", "same" or "older": whether `new` is a later window than `stored`, the same one, or an earlier one."""
    new_reset, stored_reset = number(new.get("resets_at")), number(stored.get("resets_at"))
    if new_reset is None or stored_reset is None:
        return "same"
    length = WINDOW_SECONDS.get(name)
    tolerance = length / 2 if length is not None else UNKNOWN_WINDOW_TOLERANCE_SECONDS
    if new_reset - stored_reset > tolerance:
        return "newer"
    if stored_reset - new_reset > tolerance:
        return "older"
    return "same"


def merge(stored: object, payload: object, *, now_ms: int, profile: str | None) -> dict[str, Any] | None:
    """The new file content, or None when the file needs no write."""
    new_windows = payload_windows(payload)
    if not new_windows:
        return None
    old = stored if isinstance(stored, dict) and stored.get("v") == FORMAT_VERSION else {}
    old_windows = old.get("windows")
    old_windows = old_windows if isinstance(old_windows, dict) else {}
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    session_id = session_id if isinstance(session_id, str) and session_id else None

    windows: dict[str, Any] = {name: window for name, window in old_windows.items() if isinstance(window, dict)}
    changed = False
    stale_observation = False
    for name, new in new_windows.items():
        current = windows.get(name)
        current_values = valid_window(name, current) if current is not None else None
        relation = "newer" if current_values is None else compare(name, new, current_values)
        if relation == "older":
            continue
        if relation == "same" and current is not None and current_values is not None:
            used_new, used_old = number(new["used_percentage"]), number(current_values["used_percentage"])
            assert used_new is not None and used_old is not None  # valid_window checked both
            if used_new < used_old:
                # An idle session reporting the value of its last response.
                continue
            if new == current_values:
                observed = number(current.get("observed_at"))
                if observed is None or now_ms - observed >= REFRESH_OBSERVED_MS:
                    stale_observation = True
                    windows[name] = {**current, "observed_at": now_ms, "session_id": session_id}
                continue
        changed = True
        windows[name] = {**new, "observed_at": now_ms, "changed_at": now_ms, "session_id": session_id}

    if not changed and not stale_observation:
        return None
    model = payload.get("model") if isinstance(payload, dict) else None
    model_id = model.get("id") if isinstance(model, dict) else None
    version = payload.get("version") if isinstance(payload, dict) else None
    return {
        "v": FORMAT_VERSION,
        "profile": profile,
        "written_at": now_ms,
        "session_id": session_id,
        "model": model_id if isinstance(model_id, str) else None,
        "claude_version": version if isinstance(version, str) else None,
        "windows": windows,
    }


def read_json(path: str) -> object:
    try:
        with open(path, "rb") as file:
            return json.loads(file.read())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        warn(f"ignoring unreadable {path}: {error}")
        return None


def write_atomically(path: str, data: dict[str, Any]) -> None:
    temporary = f"{path}.tmp-{os.getpid()}"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def record(stdin: bytes, environ: dict[str, str] | os._Environ[str], now_ms: int) -> None:
    config_dir = environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir or len(stdin) > MAX_INPUT_BYTES:
        return
    try:
        payload = json.loads(stdin)
    except ValueError:
        return
    path = os.path.join(config_dir, RATE_LIMITS_FILE)
    with locked(os.path.join(config_dir, LOCK_FILE)):
        stored = read_json(path)
        merged = merge(stored, payload, now_ms=now_ms, profile=environ.get("FP_CC_AUTH_SWITCH_PROFILE") or None)
        if merged is not None:
            write_atomically(path, merged)


@contextlib.contextmanager
def locked(path: str) -> Iterator[None]:
    """Hold an exclusive lock on `path` while the block runs, or run it without one when the lock is not free within
    LOCK_WAIT_SECONDS or cannot be taken at all."""
    descriptor: int | None = None
    try:
        if fcntl is not None:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        warn("the rate limits file is locked by another session, writing without the lock")
                        break
                    time.sleep(0.005)
    except OSError as error:
        warn(f"cannot lock {path}, writing without the lock: {error}")
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)


def stdin_from(data: bytes) -> None:
    """Put `data` on file descriptor 0, readable from the start, for the command that this process becomes."""
    if hasattr(os, "memfd_create"):
        descriptor = os.memfd_create("statusline-stdin")
    else:
        import tempfile

        with tempfile.TemporaryFile() as file:
            descriptor = os.dup(file.fileno())
    _write_all(descriptor, data)
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.dup2(descriptor, 0)
    os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]


def restore_process_state(environ_path: str = "/proc/self/environ") -> None:
    """Undo what the Python runtime changed in this process and the command it becomes would inherit.

    Python ignores SIGPIPE and SIGXFSZ, and an ignored signal stays ignored across exec, so a status line that writes
    to a closed pipe would get an error instead of being stopped. Python can also set LC_CTYPE to coerce a C locale to
    UTF-8 (PEP 538); the variable goes back to its value at exec time, which Linux keeps in /proc/self/environ.
    """
    for name in ("SIGPIPE", "SIGXFSZ"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    try:
        with open(environ_path, "rb") as file:
            initial = file.read().split(b"\0")
    except OSError:
        return
    original = next((entry.partition(b"=")[2] for entry in initial if entry.startswith(b"LC_CTYPE=")), None)
    if original is None:
        os.environ.pop("LC_CTYPE", None)
    else:
        os.environb[b"LC_CTYPE"] = original


def parse_args(argv: list[str]) -> str | None:
    """The --delegate command, or None. Unknown arguments are ignored: a status line must not fail over them."""
    delegate: str | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == DELEGATE_FLAG and index + 1 < len(argv):
            delegate, index = argv[index + 1], index + 2
        elif arg.startswith(f"{DELEGATE_FLAG}="):
            delegate, index = arg.removeprefix(f"{DELEGATE_FLAG}="), index + 1
        else:
            index += 1
    return delegate or None


def main(argv: list[str]) -> int:
    delegate = parse_args(argv)
    stdin = b""
    try:
        stdin = sys.stdin.buffer.read()
        record(stdin, os.environ, int(time.time() * 1000))
    except Exception as error:  # noqa: BLE001 -- recording the limits must never cost the status line
        warn(f"could not record the rate limits: {type(error).__name__}: {error}")
    if delegate is None:
        return 0
    try:
        stdin_from(stdin)
        restore_process_state()
        sys.stdout.flush()
        os.execv("/bin/sh", ["/bin/sh", "-c", delegate])
    except OSError as error:
        warn(f"cannot run the status line command: {error}")
        return 127
    return 0  # not reached
