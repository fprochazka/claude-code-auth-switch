"""
Share one sessions/ directory between profiles, and undo it.

Claude Code discovers other local sessions (ListAgents, SendMessage by name) only through
$CLAUDE_CONFIG_DIR/sessions/<pid>.json. A profile whose sessions/ is a symlink to ~/.claude/sessions
sees the sessions of every other profile that does the same, and of plain `claude`.

Live sessions keep rewriting their records in place while the installer replaces the real directory
with the symlink. The migration therefore copies instead of moving, swaps the directory and the
symlink in one syscall, keeps the old directory as a backup, and copies from the backup again after
a pause, for writes that resolved the old path just before the swap. Hard links are not an option:
the backup would share inodes with the live records, and a stale file could overwrite a newer one.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_code_auth_switch.session_start import LOG_NAME

SETTLE_SECONDS = 1.5
SWAP_NAME = "sessions.swap"
BACKUP_PREFIX = "sessions.bak-"
# Retries when a session recreates sessions/ between the two renames of the fallback swap.
MAX_SWAP_ATTEMPTS = 5

AT_FDCWD_LINUX = -100
RENAME_EXCHANGE_LINUX = 2
RENAME_SWAP_DARWIN = 0x2


class SwapUnsupported(Exception):
    """The platform or file system has no atomic exchange of two paths."""


class SessionsError(Exception):
    """A state the installer must not guess its way through; the message says what to do."""


def shared_sessions_dir() -> Path:
    """The directory of plain `claude`, which every sharing profile points at."""
    return Path.home() / ".claude" / "sessions"


def exchange(first: Path, second: Path) -> None:
    """Swap two paths atomically: renameat2(RENAME_EXCHANGE) on Linux, renamex_np(RENAME_SWAP) on macOS.

    The macOS branch follows the documented API but has not run on a Mac.
    """
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    a, b = os.fsencode(first), os.fsencode(second)
    if sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        if function is None:
            raise SwapUnsupported("libc has no renameat2")
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = function(AT_FDCWD_LINUX, a, AT_FDCWD_LINUX, b, RENAME_EXCHANGE_LINUX)
    elif sys.platform == "darwin":
        function = getattr(libc, "renamex_np", None)
        if function is None:
            raise SwapUnsupported("libc has no renamex_np")
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = function(a, b, RENAME_SWAP_DARWIN)
    else:
        raise SwapUnsupported(f"no atomic exchange on {sys.platform}")
    if result != 0:
        code = ctypes.get_errno()
        if code in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise SwapUnsupported(os.strerror(code))
        raise OSError(code, os.strerror(code), f"{first} <-> {second}")


@dataclass(frozen=True)
class Copied:
    """What a migration or rollback wrote into a file of the target directory."""

    size: int
    mtime_ns: int
    digest: bytes


# What the passes of one migration or rollback wrote, by source directory (st_dev, st_ino) and file
# name. The identity of the directory, not its path, because the old sessions/ is renamed to a backup
# between the first pass and the catch-up passes.
CopyRecord = dict[tuple[int, int, str], Copied]


def read_file(path: str | Path) -> tuple[bytes, os.stat_result]:
    """Read a file and the stat that belongs to the bytes read."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        stat_result = os.fstat(fd)
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks), stat_result
    finally:
        os.close(fd)


def fingerprint(path: Path) -> Copied | None:
    try:
        data, stat_result = read_file(path)
    except (FileNotFoundError, IsADirectoryError):
        return None
    return Copied(len(data), stat_result.st_mtime_ns, hashlib.sha256(data).digest())


TEMP_SUFFIX = ".cc-auth-switch-tmp"
# write_copy's temp files: ".<name>.", the 8 characters that tempfile.mkstemp adds, then the suffix.
TEMP_NAME = re.compile(r"\..+\.[a-z0-9_]{8}" + re.escape(TEMP_SUFFIX))


def remove_leftover_temps(directory: Path) -> None:
    """Delete the temp files of a pass that was killed between writing and renaming."""
    for entry in os.scandir(directory):
        if TEMP_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
            Path(entry.path).unlink(missing_ok=True)


def write_copy(target: Path, data: bytes, source_stat: os.stat_result) -> Copied:
    """Put data into target with the source's mode and mtime, through a temp file and a rename.

    The result describes exactly the bytes and the mtime written, even when a session rewrites
    target right after the rename.
    """
    fd, temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=TEMP_SUFFIX, dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp, stat.S_IMODE(source_stat.st_mode))
        os.utime(temp, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return Copied(len(data), source_stat.st_mtime_ns, hashlib.sha256(data).digest())


def copy_newer(source_dir: Path, target_dir: Path, names: set[str] | None = None, copied: CopyRecord | None = None) -> list[str]:
    """Copy regular files whose copy in target_dir is missing or older, keeping the mtime.

    With `copied`, the caller's record of what earlier passes wrote into target_dir, one more case
    copies: the target still holds exactly what an earlier pass wrote from this same source directory,
    and the source differs from it. Nothing has written the target since, so the source holds the
    later state of that file, whatever the mtimes say. A session that empties and then rewrites its
    record within one timestamp tick otherwise leaves an empty copy behind, because both writes carry
    the same mtime. A target written from another source directory is compared by mtime, so with
    several backups the newest file wins in any order. Every file written is added to `copied`.

    A file that disappears while copying belongs to a session that just ended, so it is skipped. Temp
    files that a killed pass left in target_dir are deleted first, and never copied from source_dir.
    """
    remove_leftover_temps(target_dir)
    written: list[str] = []
    source_id = os.stat(source_dir)
    for entry in os.scandir(source_dir):
        if names is not None and entry.name not in names:
            continue
        if TEMP_NAME.fullmatch(entry.name):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            data, source_stat = read_file(entry.path)
            target = target_dir / entry.name
            try:
                target_mtime = os.stat(target).st_mtime_ns
            except FileNotFoundError:
                target_mtime = None
            if target_mtime is not None:
                ours = copied.get((source_id.st_dev, source_id.st_ino, entry.name)) if copied is not None else None
                if ours is not None and fingerprint(target) == ours:
                    if hashlib.sha256(data).digest() == ours.digest:
                        continue
                elif target_mtime >= source_stat.st_mtime_ns:
                    continue
            record = write_copy(target, data, source_stat)
            if copied is not None:
                copied[(source_id.st_dev, source_id.st_ino, entry.name)] = record
            written.append(entry.name)
        except FileNotFoundError:
            continue
    return sorted(written)


def backup_path(profile_dir: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = profile_dir / f"{BACKUP_PREFIX}{stamp}"
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = profile_dir / f"{BACKUP_PREFIX}{stamp}-{counter}"
        counter += 1
    return candidate


def put_in_place(staged: Path, live: Path, profile_dir: Path, target: Path, copied: CopyRecord) -> list[Path]:
    """Replace `live` with `staged` and return where the replaced directory went.

    The atomic exchange leaves the old directory at `staged`, which is then renamed to a backup. The
    fallback renames twice, and a session that runs `mkdir -p` in the gap recreates `live` as a real
    directory. That directory is merged into `target`, kept as one more backup, and the swap retried.
    """
    try:
        exchange(staged, live)
    except SwapUnsupported:
        pass
    else:
        backup = backup_path(profile_dir)
        os.rename(staged, backup)
        return [backup]

    backups: list[Path] = []
    for _ in range(MAX_SWAP_ATTEMPTS):
        backup = backup_path(profile_dir)
        os.rename(live, backup)
        backups.append(backup)
        try:
            os.rename(staged, live)
            return backups
        except OSError as error:
            if error.errno not in (errno.EISDIR, errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR):
                raise
            copy_newer(live, target, copied=copied)
    raise SessionsError(f"{live} was recreated by a running session {MAX_SWAP_ATTEMPTS} times in a row, try again")


def migrate(profile_dir: Path, target: Path, settle: float = SETTLE_SECONDS) -> list[Path]:
    """Replace the real <profile>/sessions with a symlink to `target` and return the backup directories."""
    live = profile_dir / "sessions"
    staged = profile_dir / SWAP_NAME
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged.symlink_to(target)
    copied: CopyRecord = {}
    copy_newer(live, target, copied=copied)
    backups = put_in_place(staged, live, profile_dir, target, copied)

    # Writes that resolved the old path just before the swap land in the backup. Wait for them, then
    # copy what is newer there; a second pass should find nothing left.
    time.sleep(settle)
    for backup in backups:
        copy_newer(backup, target, copied=copied)
    time.sleep(settle)
    late = [name for backup in backups for name in copy_newer(backup, target, copied=copied)]
    if late:
        print(f"  warning: copied late writes after the second pass: {', '.join(late)}")
    return backups


def remove_leftover_swap(profile_dir: Path, target: Path) -> None:
    staged = profile_dir / SWAP_NAME
    if staged.is_symlink() and os.path.realpath(staged) == os.path.realpath(target):
        staged.unlink()
        print(f"  removed leftover {staged}")
    elif staged.is_symlink() or staged.exists():
        raise SessionsError(
            f"{staged} is left over from an interrupted migration and is not a symlink to {target}. "
            f"Check what it holds, move it out of the way, then run the installer again."
        )


def ensure_shared(profile_name: str, profile_dir: Path, target: Path, settle: float = SETTLE_SECONDS) -> None:
    """Make <profile>/sessions a symlink to `target`, migrating a real directory."""
    remove_leftover_swap(profile_dir, target)
    live = profile_dir / "sessions"
    if live.is_symlink():
        if os.path.realpath(live) != os.path.realpath(target):
            print(f"  warning: {live} is a symlink to {os.readlink(live)}, not to {target}; not sharing sessions")
        return
    if not live.exists():
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        live.symlink_to(target)
        print(f"  symlinked sessions/ -> {target}")
        return
    if not live.is_dir():
        print(f"  warning: {live} is not a directory; not sharing sessions")
        return

    backups = migrate(profile_dir, target, settle)
    print(f"  migrated sessions/ -> {target}")
    for backup in backups:
        print(f"  kept the old directory as {backup}")
    print(f"  to undo: cc-auth-switch rollback-sessions {profile_name}")


def warn_if_still_shared(profile_name: str, profile_dir: Path, target: Path) -> None:
    live = profile_dir / "sessions"
    if live.is_symlink() and os.path.realpath(live) == os.path.realpath(target):
        print(f"  warning: sessions/ is still shared with {target}, although share_sessions is off")
        print(f"  to undo: cc-auth-switch rollback-sessions {profile_name}")


# Rollback


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def owner_from_log(pid: int, target: Path, profile_dir: Path, profile_name: str) -> bool | None:
    """Ask the SessionStart log of the record's session; None when it has no line for this pid."""
    try:
        session_id = json.loads((target / f"{pid}.json").read_text()).get("sessionId")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(session_id, str) or not session_id or "/" in session_id:
        return None
    latest = None
    for log in (profile_dir / "projects").glob(f"*/{session_id}/{LOG_NAME}"):
        try:
            lines = log.read_text().splitlines()
        except OSError:
            continue
        for raw in lines:
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict) and line.get("pid") == pid and (latest is None or line.get("ts", 0) >= latest.get("ts", 0)):
                latest = line
    if latest is None:
        return None
    if latest.get("config_dir"):
        return same_path(latest["config_dir"], profile_dir)
    if latest.get("profile"):
        return latest["profile"] == profile_name
    return None


def owner_from_environ(pid: int, profile_dir: Path) -> bool | None:
    """Read CLAUDE_CONFIG_DIR of a live process; Linux only, None when it cannot be read."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for item in raw.split(b"\0"):
        if item.startswith(b"CLAUDE_CONFIG_DIR="):
            return same_path(os.fsdecode(item.split(b"=", 1)[1]), profile_dir)
    # Without the variable it is plain `claude`, which uses ~/.claude.
    return False


def owner_from_backups(name: str, profile_dir: Path) -> bool:
    return any((backup / name).exists() for backup in profile_dir.glob(f"{BACKUP_PREFIX}*"))


@dataclass
class Ownership:
    live: set[str] = field(default_factory=set)
    dead: set[str] = field(default_factory=set)


def find_owned(target: Path, profile_dir: Path, profile_name: str) -> Ownership:
    """Sort the files of the shared directory that belong to this profile into live and dead sessions.

    Files are named after the session's pid (<pid>.json, <pid>.<hash>.key). The owner comes from the
    SessionStart log, then the live process's environment, then the migration backups.
    """
    owned = Ownership()
    for entry in os.scandir(target):
        prefix = entry.name.split(".", 1)[0]
        if not prefix.isdigit() or not entry.is_file(follow_symlinks=False):
            continue
        pid = int(prefix)
        alive = pid_alive(pid)
        owner = owner_from_log(pid, target, profile_dir, profile_name)
        if owner is None and alive:
            owner = owner_from_environ(pid, profile_dir)
        if owner is None:
            owner = owner_from_backups(entry.name, profile_dir)
        if owner:
            (owned.live if alive else owned.dead).add(entry.name)
    return owned


def put_back(staged: Path, live: Path, copied: CopyRecord) -> None:
    """Replace the `live` symlink with the real directory `staged`."""
    try:
        exchange(staged, live)
    except SwapUnsupported:
        pass
    else:
        staged.unlink()
        return
    for _ in range(MAX_SWAP_ATTEMPTS):
        if live.is_symlink():
            live.unlink()
        try:
            os.rename(staged, live)
            return
        except OSError as error:
            if error.errno not in (errno.EISDIR, errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR):
                raise
            # A session recreated sessions/ in the gap: keep its files and retry.
            copy_newer(live, staged, copied=copied)
            shutil.rmtree(live)
    raise SessionsError(f"{live} was recreated by a running session {MAX_SWAP_ATTEMPTS} times in a row, try again")


def rollback(profile_name: str, profile_dir: Path, target: Path, settle: float = SETTLE_SECONDS) -> Ownership:
    """Give a profile back its own sessions/ directory with the records of its live sessions."""
    live = profile_dir / "sessions"
    staged = profile_dir / SWAP_NAME
    if not (live.is_symlink() and same_path(live, target)):
        raise SessionsError(f"{live} is not a symlink to {target}, so there is nothing to roll back")
    if staged.is_symlink() or staged.exists():
        raise SessionsError(f"{staged} is left over from an interrupted run. Check what it holds and move it out of the way first.")

    owned = find_owned(target, profile_dir, profile_name)
    staged.mkdir(mode=0o700)
    copied: CopyRecord = {}
    copy_newer(target, staged, owned.live, copied)
    put_back(staged, live, copied)

    # Same race as the migration, in the other direction: late writes through the old symlink land
    # in the shared directory.
    time.sleep(settle)
    copy_newer(target, live, owned.live, copied)
    for name in sorted(owned.live | owned.dead):
        try:
            (target / name).unlink()
        except FileNotFoundError:
            pass
    return owned
