"""
The one file that holds the joined text of a role's several prompt files.

Claude Code 2.1.325 keeps only the last --append-system-prompt-file or --system-prompt-file, so `start` cannot pass
the flag once per file. It joins the files into one file in the cache and passes that path instead, also to hooks
in FP_CC_AUTH_SWITCH_ROLE_PROMPT and to the start log, which both take one path.

The file name holds a hash of the joined bytes, so a running session never sees its file change: an edited source
gives a new file. Launches with the same content write the same name and the same bytes, so they need no lock.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

CACHE_SUBDIR = Path("cc-auth-switch") / "role-prompts"
HASH_LENGTH = 16
# A launch refreshes the mtime of the file it reuses, so a file is removed this long after the last launch that used it.
# A session that runs longer and re-reads the file in mode none, on resume, /clear or compaction, finds it gone.
RETENTION_SECONDS = 30 * 24 * 60 * 60
# A temporary file of a launch that died between creating and renaming it.
STALE_TEMP_SECONDS = 60 * 60
TEMP_SUFFIX = ".tmp"


def cache_dir(environ: Mapping[str, str]) -> Path:
    """The directory of the joined prompt files: under $XDG_CACHE_HOME, or ~/.cache without it."""
    base = environ.get("XDG_CACHE_HOME", "")
    # The XDG spec says to ignore a relative path.
    if not os.path.isabs(base):
        base = os.path.join(environ.get("HOME") or os.path.expanduser("~"), ".cache")
    return Path(base) / CACHE_SUBDIR


def join_sources(sources: Sequence[str]) -> bytes:
    """The bytes of each source as read, each ending with a newline, with one blank line between them."""
    parts = []
    for source in sources:
        content = Path(source).read_bytes()
        parts.append(content if content.endswith(b"\n") else content + b"\n")
    return b"\n".join(parts)


def file_name(role: str, content: bytes) -> str:
    # The role name is only for humans reading the directory; the hash makes the name unique.
    safe_role = re.sub(r"[^A-Za-z0-9_.-]", "-", role).lstrip(".") or "role"
    return f"{safe_role}-{hashlib.sha256(content).hexdigest()[:HASH_LENGTH]}.md"


def joined_prompt_file(role: str, sources: Sequence[str], directory: Path) -> str:
    """Join the sources into one file in `directory` and return its path. Raises OSError when it cannot."""
    content = join_sources(sources)
    target = directory / file_name(role, content)
    try:
        os.utime(target)
    except FileNotFoundError:
        pass
    else:
        return str(target)

    # The text may be private, so the directory and the file are for the user only. An existing directory stays as it is.
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = directory / f".{target.name}.{secrets.token_hex(8)}{TEMP_SUFFIX}"
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    remove_stale(directory, time.time())
    return str(target)


def remove_stale(directory: Path, now: float) -> None:
    """Delete joined files unused for RETENTION_SECONDS and temporary files older than STALE_TEMP_SECONDS. Never fails."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        name = entry.name
        if name.startswith(".") and name.endswith(TEMP_SUFFIX):
            limit = STALE_TEMP_SECONDS
        elif not name.startswith(".") and name.endswith(".md"):
            limit = RETENTION_SECONDS
        else:
            continue
        with contextlib.suppress(OSError):
            if entry.is_file(follow_symlinks=False) and now - entry.stat(follow_symlinks=False).st_mtime > limit:
                os.unlink(entry.path)
