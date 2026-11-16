"""
Print the Claude Code attribution settings that the user's git config sets for a directory.

The package installs this module as the claude-auth-switch-attribution command, and every
generated wrapper runs it by absolute path at launch. The output is either empty or a JSON object
for `claude --settings`:

    {"attribution": {"commit": "...", "pr": "...", "sessionUrl": true}}

The values come from a [claude-code "attribution"] section. Inside a git repository git evaluates
every include and includeIf itself. Outside a repository git skips all gitdir: conditions, so this
script evaluates the plain-prefix ones (gitdir:~/devel/projects/company/) from the global config.

The script never stops a launch: on any error it prints a message to stderr, prints nothing to
stdout and exits 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

SECTION_PREFIX = "claude-code.attribution."
SECTION_REGEX = r"^claude-code\.attribution\."
INCLUDE_IF_REGEX = r"^includeif\.gitdir(/i)?:.*\.path$"

STRING_KEYS = {"commit": "commit", "pr": "pr"}
BOOLEAN_KEYS = {"sessionurl": "sessionUrl"}

GLOB_CHARS = ("*", "?", "[")
# git stops following includes at this depth, so a cycle cannot recurse forever.
MAX_INCLUDE_DEPTH = 10

PROG = "claude-auth-switch-attribution"


def warn(message: str) -> None:
    print(f"{PROG}: {message}", file=sys.stderr)


def git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True)


def parse_entries(output: bytes, with_origin: bool) -> list[tuple[str | None, str, str | None]]:
    """Parse `git config -z --get-regexp` output into (origin, key, value) tuples.

    Each entry is "key\\nvalue\\0", or "key\\0" for a key without a value. With --show-origin,
    a "file:<path>\\0" record comes before each entry.
    """
    records = output.decode("utf-8", errors="replace").split("\0")
    if records and records[-1] == "":
        records.pop()
    entries: list[tuple[str | None, str, str | None]] = []
    index = 0
    while index < len(records):
        origin = None
        if with_origin:
            origin = records[index]
            index += 1
            if index >= len(records):
                break
        key, newline, value = records[index].partition("\n")
        entries.append((origin, key, value if newline else None))
        index += 1
    return entries


def origin_dir(origin: str | None) -> str | None:
    if origin is None or not origin.startswith("file:"):
        return None
    return os.path.dirname(os.path.abspath(origin[len("file:"):]))


def expand_include_path(path: str, base_dir: str | None) -> str | None:
    """Resolve an include path the way git does: ~/ is the home directory, relative is relative to the declaring file."""
    if path.startswith("~/") or path == "~":
        return os.path.expanduser(path)
    if os.path.isabs(path):
        return path
    if base_dir is None:
        return None
    return os.path.normpath(os.path.join(base_dir, path))


def gitdir_condition_matches(condition: str, base_dir: str | None, directory: str) -> bool:
    """Decide whether an includeIf gitdir: condition applies to a directory that is not a git repository.

    Only patterns that end in / are prefix matches against a directory, and only anchored patterns
    without glob characters can be checked without git. Everything else is skipped.
    """
    case_insensitive = condition.startswith("gitdir/i:")
    pattern = condition.split(":", 1)[1]
    if any(char in pattern for char in GLOB_CHARS):
        return False
    if not pattern.endswith("/"):
        return False
    if pattern.startswith("~/"):
        pattern = os.path.expanduser("~") + pattern[1:]
    elif pattern.startswith("./"):
        if base_dir is None:
            return False
        pattern = os.path.join(base_dir, pattern[2:])
    elif not pattern.startswith("/"):
        # git treats an unanchored pattern as **/pattern, which is a glob.
        return False

    candidates = {directory.rstrip("/") + "/", os.path.realpath(directory).rstrip("/") + "/"}
    if case_insensitive:
        pattern = pattern.casefold()
        candidates = {candidate.casefold() for candidate in candidates}
    return any(candidate.startswith(pattern) for candidate in candidates)


def collect_outside_repo(scope: list[str], directory: str, depth: int = 0) -> list[tuple[str, str | None]]:
    """Read attribution keys from a config file and the gitdir: includes that match the directory, in git's order."""
    if depth > MAX_INCLUDE_DEPTH:
        warn("includes nested too deep, ignoring the rest")
        return []
    pattern = f"({SECTION_REGEX}|{INCLUDE_IF_REGEX})"
    result = git(["config", *scope, "--includes", "--show-origin", "-z", "--get-regexp", pattern], directory)
    # Exit code 1 means no key matched; a missing config file also lands here.
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or f"git config exited with {result.returncode}")

    values: list[tuple[str, str | None]] = []
    for origin, key, value in parse_entries(result.stdout, with_origin=True):
        if key.startswith(SECTION_PREFIX):
            values.append((key, value))
            continue
        if value is None:
            continue
        condition = key[len("includeif."):-len(".path")]
        base_dir = origin_dir(origin)
        if not gitdir_condition_matches(condition, base_dir, directory):
            continue
        include_path = expand_include_path(value, base_dir)
        if include_path is None or not os.path.isfile(include_path):
            continue
        values.extend(collect_outside_repo(["--file", include_path], directory, depth + 1))
    return values


def collect_inside_repo(directory: str) -> list[tuple[str, str | None]]:
    result = git(["config", "-z", "--get-regexp", SECTION_REGEX], directory)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or f"git config exited with {result.returncode}")
    return [(key, value) for _, key, value in parse_entries(result.stdout, with_origin=False)]


def parse_git_bool(value: str | None) -> bool | None:
    """git's boolean rules; None means the value is not a boolean."""
    if value is None:
        return True
    normalized = value.strip().lower()
    if normalized in ("true", "yes", "on"):
        return True
    if normalized in ("false", "no", "off", ""):
        return False
    try:
        return int(normalized, 0) != 0
    except ValueError:
        return None


def build_attribution(values: list[tuple[str, str | None]]) -> dict[str, object]:
    """Turn raw (key, value) pairs into the attribution object; the last value of a key wins."""
    last: dict[str, str | None] = {}
    for key, value in values:
        last[key[len(SECTION_PREFIX):]] = value

    attribution: dict[str, object] = {}
    for name, value in last.items():
        if name in STRING_KEYS:
            if value is None:
                warn(f"{SECTION_PREFIX}{name} has no value, ignoring it")
                continue
            attribution[STRING_KEYS[name]] = value
        elif name in BOOLEAN_KEYS:
            parsed = parse_git_bool(value)
            if parsed is None:
                warn(f"{SECTION_PREFIX}{name} is not a boolean: {value!r}, ignoring it")
                continue
            attribution[BOOLEAN_KEYS[name]] = parsed
        else:
            warn(f"unknown key {SECTION_PREFIX}{name}, ignoring it")
    return attribution


def settings_for(directory: str) -> dict[str, object] | None:
    directory = os.path.abspath(directory)
    if git(["rev-parse", "--git-dir"], directory).returncode == 0:
        values = collect_inside_repo(directory)
    else:
        values = collect_outside_repo(["--global"], directory)
    attribution = build_attribution(values)
    if not attribution:
        return None
    return {"attribution": attribution}


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    if len(argv) > 2:
        warn(f"usage: {PROG} [DIRECTORY]")
        return 0
    directory = argv[1] if len(argv) == 2 else os.getcwd()
    try:
        settings = settings_for(directory)
    except Exception as error:  # noqa: BLE001 -- a broken git config must not stop Claude from starting
        warn(f"cannot read attribution from git config: {error}")
        return 0
    if settings is not None:
        print(json.dumps(settings, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
