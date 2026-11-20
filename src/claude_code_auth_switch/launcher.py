"""
`cc-auth-switch start [--config PATH] <profile> [claude arguments...]`, which every claude-<profile> launcher runs.

It reads auth-switch.yaml and the secrets file on every launch, builds the environment and the arguments
for Claude Code, and replaces itself with `claude`. A config that cannot be read or is invalid stops the
launch with the config path and the error, so a broken config never starts Claude under the wrong account.

build_launch() turns a resolved profile, the user's arguments and the launch context into argv and env.
It runs no processes and reads no files itself, so tests can call it directly.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from claude_code_auth_switch.claude_options import has_option, remove_option, scan
from claude_code_auth_switch.config import ENV_NAME, ConfigError
from claude_code_auth_switch.role_prompt_cache import cache_dir, joined_prompt_file
from claude_code_auth_switch.roles import NO_ROLE, Role, resolve_roles

PROG = "cc-auth-switch start"

# Claude Code subcommands reject --add-dir, --model and the other session options with "unknown option",
# so arguments that start with one of them pass through unchanged.
SUBCOMMANDS = frozenset(
    {
        "mcp",
        "auth",
        "doctor",
        "install",
        "setup-token",
        "update",
        "upgrade",
        "agents",
        "auto-mode",
        "plugin",
        "plugins",
        "attach",
        "logs",
        "respawn",
        "rm",
        "stop",
        "kill",
        "purge",
        "import",
        "gateway",
        "ultrareview",
    }
)

# A name given at launch replaces the saved title of a resumed session, /rename included, so the tmux
# name is left out when the user names the session or resumes one.
NAME_SKIP_OPTIONS = ("--name", "--resume", "--continue", "--session-id", "--fork-session", "--from-pr")

# A tmux server that does not answer costs the session name, never the launch.
TMUX_TIMEOUT_SECONDS = 2

# Inherited variables of these prefixes are dropped, so a launch from inside another Claude Code
# session does not carry over its account, config dir or session state.
CLEARED_ENV_PREFIXES = ("CLAUDE_", "ANTHROPIC_")

# Hooks read these to tell which role runs and whether its prompt is already in the system prompt. They
# are always cleared first: a launcher run from another session's Bash would otherwise inherit its role.
ROLE_ENV = "FP_CC_AUTH_SWITCH_ROLE"
ROLE_PROMPT_ENV = "FP_CC_AUTH_SWITCH_ROLE_PROMPT"
ROLE_PROMPT_MODE_ENV = "FP_CC_AUTH_SWITCH_ROLE_PROMPT_MODE"
# The role's prompt files as a JSON array, in order. ROLE_PROMPT_ENV names the file that holds their joined text.
ROLE_PROMPT_FILES_ENV = "FP_CC_AUTH_SWITCH_ROLE_PROMPT_FILES"
# NAME=<hash of the value> for each variable that the role's env: set, comma-separated. The next launch
# from inside the session clears a listed variable while it still holds the role's value, so a worker
# started from a dispatcher's Bash does not inherit its role's env, and a value the user set since stays.
# The hash keeps a secret in a role's env out of every child process's environment.
ROLE_ENV_KEYS_ENV = "FP_CC_AUTH_SWITCH_ROLE_ENV_KEYS"
ROLE_ENV_HASH_LENGTH = 16
ROLE_ENV_VARS = (ROLE_ENV, ROLE_PROMPT_ENV, ROLE_PROMPT_MODE_ENV, ROLE_PROMPT_FILES_ENV, ROLE_ENV_KEYS_ENV)

# Set to 1 to get the traceback of an unexpected error in `start` instead of a one-line message.
DEBUG_ENV = "CC_AUTH_SWITCH_DEBUG"

# Set to 1 to start a session in the home directory once, although refuse_home_cwd is on. Outside CLAUDE_*,
# so start does not drop it with the inherited variables; start drops it itself, so it does not carry over
# to a launch from inside the session.
ALLOW_HOME_ENV = "CC_AUTH_SWITCH_ALLOW_HOME"
REFUSE_HOME_CWD_KEY = "refuse_home_cwd"

# Put `cc-auth-switch statusline` in front of the user's status line, to record the rate limits Claude Code passes it.
CAPTURE_RATE_LIMITS_KEY = "capture_rate_limits"
STATUS_LINE_KEY = "statusLine"
STATUSLINE_SUBCOMMAND = "statusline"
# The statusLine keys that change how Claude Code shows the line, copied from the user's statusLine to the injected one.
STATUS_LINE_DISPLAY_KEYS = ("padding", "refreshInterval", "hideVimModeIndicator")
# The settings files whose statusLine the injected one replaces, lowest precedence first, by their --setting-sources name.
STATUS_LINE_SOURCES = ("user", "project", "local")
# Managed settings, which win over --settings. A statusLine there, or allowManagedHooksOnly, makes Claude Code ignore ours.
MANAGED_SETTINGS_DIRS = {
    "linux": Path("/etc/claude-code"),
    "darwin": Path("/Library/Application Support/ClaudeCode"),
}

AGENT_ROLE_FLAG = "--agent-role"

# With one of these, claude prints and exits without a session, so start passes the arguments through
# as for a subcommand: no role, no prompt file check, no session name.
PASSTHROUGH_OPTIONS = ("--version", "--help")

# The system prompt flag of each prompt mode, and the user flags that replace it.
PROMPT_FLAGS = {
    "append": ("--append-system-prompt-file", ("--append-system-prompt", "--append-system-prompt-file")),
    "replace": ("--system-prompt-file", ("--system-prompt", "--system-prompt-file")),
}


# Claude Code reapplies nothing of the role on resume, so start looks up the role that a session ran as
# in the start log. The lookup is by session id, which --resume also accepts as a name or search term.
UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SNAPSHOT_FLAG = "--system-prompt-snapshot"
# Claude Code names a project directory after the cwd with every other character replaced by "-", and
# cuts names longer than this, adding a hash of the path.
PROJECT_DIR_NAME_LIMIT = 200


class LaunchError(Exception):
    """The command line asks for something start cannot do."""


@dataclass(frozen=True)
class Profile:
    name: str
    config_dir: Path
    # Final values: pinned_models, then env: and the secrets file on top.
    env: dict[str, str] = field(default_factory=dict)
    add_dirs: tuple[Path, ...] = ()
    model: str | None = None
    # Role applied when the command line has no --agent-role, or NO_ROLE.
    default_role: str | None = None
    # Refuse a session whose start directory is the home directory, see refuse_home_cwd.
    refuse_home_cwd: bool = True
    # Inject the statusline tap, see capture_rate_limits. Off here, so that a Profile built by hand launches as before;
    # resolve_profile() turns it on unless the config turns it off.
    capture_rate_limits: bool = False


@dataclass(frozen=True)
class StatusLineSources:
    """The statusLine objects of the settings files that Claude Code would read for a session, and whether it would
    ignore the one start passes."""

    # (source, statusLine) of each file that has one, in STATUS_LINE_SOURCES order.
    layers: tuple[tuple[str, dict[str, Any]], ...] = ()
    # Managed settings set a statusLine or allowManagedHooksOnly, so a statusLine in --settings never runs.
    managed: bool = False


@dataclass(frozen=True)
class LaunchContext:
    """What a launch reads from the machine it runs on."""

    cwd: str
    environ: Mapping[str, str]
    # Name of the tmux session that holds a pane, or None when tmux cannot say.
    tmux_session: Callable[[str], str | None]
    # Settings from the [claude-code "attribution"] git config for a directory, or None.
    git_attribution: Callable[[str], dict[str, Any] | None]
    # The settings object of a user's --settings value, inline JSON or a file path relative to the cwd.
    # Raises LaunchError when it cannot be read.
    read_settings: Callable[[str, str], dict[str, Any]] = lambda value, cwd: read_settings_value(value, cwd)
    # Why a role's prompt file cannot be read, or None when it can.
    prompt_file_problem: Callable[[str], str | None] = lambda path: unreadable_file_problem(path)
    # The path of one file with the joined text of a role's prompt files (role name, files). Raises OSError.
    join_prompt_files: Callable[[str, Sequence[str]], str] = lambda role, files: joined_prompt_file(
        role, files, cache_dir(os.environ)
    )
    # The newest start-log line of a session (config dir, session id, cwd), or None without a readable log.
    recorded_start: Callable[[Path, str, str], dict[str, Any] | None] = lambda config_dir, session_id, cwd: (
        recorded_start_of(config_dir, session_id, cwd)
    )
    # The newest start-log line of the newest session in a directory (config dir, cwd), as -c would continue it.
    # An empty dict when that session has no log, None when the directory has no session.
    newest_start: Callable[[Path, str], dict[str, Any] | None] = lambda config_dir, cwd: newest_start_in(
        config_dir, cwd
    )
    # The statusLine settings of the user, project and local settings files for a session (config dir, cwd).
    status_line_sources: Callable[[Path, str], StatusLineSources] = lambda config_dir, cwd: read_status_line_sources(
        config_dir, cwd
    )
    # The shell words that run this tool, for the statusLine command.
    tool_command: Callable[[], str] = lambda: own_command()


@dataclass(frozen=True)
class Launch:
    argv: list[str]
    env: dict[str, str]
    # One-line notes for stderr, about what start did with the role.
    notes: tuple[str, ...] = ()


def merge_settings(*layers: dict[str, Any] | None) -> dict[str, Any]:
    """Deep-merge settings objects; a later layer wins per key, nested objects merge.

    Lists are concatenated without duplicates, as Claude Code merges its own settings sources, so a
    permissions.allow list in a later layer adds rules instead of replacing the earlier ones.
    """
    merged: dict[str, Any] = {}
    for layer in layers:
        for key, value in (layer or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = merge_settings(merged[key], value)
            elif isinstance(value, list) and isinstance(merged.get(key), list):
                merged[key] = merged[key] + [item for item in value if item not in merged[key]]
            else:
                merged[key] = value
    return merged


def read_settings_value(value: str, cwd: str) -> dict[str, Any]:
    """Read a --settings value the way Claude Code takes it: a JSON object, or the path of a JSON file."""
    if value.lstrip().startswith("{"):
        source, text = "inline JSON", value
    else:
        source = os.path.join(cwd, os.path.expanduser(value))
        try:
            text = Path(source).read_text()
        except OSError as error:
            raise LaunchError(f"--settings: cannot read {source}: {error.strerror or error}") from error
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as error:
        raise LaunchError(f"--settings: {source} is not valid JSON: {error}") from error
    if not isinstance(settings, dict):
        raise LaunchError(f"--settings: {source} must hold a JSON object")
    return settings


def own_command() -> str:
    """The command that runs this cc-auth-switch, as shell words: the absolute path it was started by."""
    program = sys.argv[0] if sys.argv else ""
    if program and os.sep not in program:
        program = shutil.which(program) or program
    if not program or program.endswith(".py") or not os.path.isfile(program):
        # Started as `python -m claude_code_auth_switch` or from `python -c`, as some tests do.
        return shlex.join([sys.executable, "-m", "claude_code_auth_switch"])
    return shlex.quote(os.path.abspath(program))


def is_tap_command(command: str) -> bool:
    """Tell whether a statusLine command runs `cc-auth-switch statusline`, which must never call itself."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return any(
        word == STATUSLINE_SUBCOMMAND
        and (os.path.basename(before) == "cc-auth-switch" or before == "claude_code_auth_switch")
        for before, word in zip(words, words[1:], strict=False)
    )


def setting_sources(args: Sequence[str]) -> set[str]:
    """The settings files that a --setting-sources option lets Claude Code read, all of them without one."""
    chosen = [option.values for option in scan(args) if option.name == "--setting-sources" and option.values]
    if not chosen:
        return set(STATUS_LINE_SOURCES)
    return {source.strip() for value in chosen[-1] for source in value.split(",")}


def status_line_tap(
    sources: StatusLineSources, user_settings: dict[str, Any] | None, args: Sequence[str], tool_command: str
) -> dict[str, Any] | None:
    """The statusLine that runs `cc-auth-switch statusline` and then the user's own status line, or None without one.

    The user's status line is the one Claude Code would run without the launcher: the statusLine of the settings files,
    each over the one before, then the user's own --settings over them. The keys that change how the line is shown are
    copied, so the injected object looks the same whether Claude Code merges it with the files' or replaces theirs.

    Without a status line of the user's own, there is nothing to put the tap in front of: any configured statusLine
    hides Claude Code's footer hint row, and asks for workspace trust in an untrusted folder, so the session keeps
    having none, and the profile records no rate limits from it.
    """
    allowed = setting_sources(args)
    effective: dict[str, Any] = {}
    for source, status_line in sources.layers:
        if source in allowed:
            effective.update(status_line)
    user_status_line = (user_settings or {}).get(STATUS_LINE_KEY)
    if isinstance(user_status_line, dict):
        effective.update(user_status_line)
    delegate = effective.get("command")
    if effective.get("type") != "command" or not isinstance(delegate, str) or not delegate or is_tap_command(delegate):
        return None
    command = f"{tool_command} {STATUSLINE_SUBCOMMAND} --delegate {shlex.quote(delegate)}"
    tap: dict[str, Any] = {"type": "command", "command": command}
    tap.update({key: effective[key] for key in STATUS_LINE_DISPLAY_KEYS if key in effective})
    return tap


def read_settings_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def git_dir_of(directory: Path) -> Path | None:
    """The directory that a `.git` entry in `directory` stands for: itself, or a linked worktree's gitdir."""
    entry = directory / ".git"
    if entry.is_dir():
        return entry
    try:
        text = entry.read_text().strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    return (directory / text.removeprefix("gitdir:").strip()).resolve()


def canonical_git_root(directory: Path) -> Path | None:
    """The main checkout of the git repository that `directory` is in, or None outside one. Reads files, runs no git."""
    for candidate in (directory, *directory.parents):
        git_dir = git_dir_of(candidate)
        if git_dir is None:
            continue
        try:
            common = (git_dir / (git_dir / "commondir").read_text().strip()).resolve()
        except OSError:
            common = git_dir
        return common.parent if common.name == ".git" else candidate
    return None


def local_settings_dir(cwd: Path, home: Path) -> Path:
    """Where Claude Code 2.1.325 reads .claude/settings.local.json for a session started in `cwd`.

    Its `localSettings` path is the canonical root of the git repository the start directory is in, when that is neither
    the start directory nor the home directory, and the start directory otherwise. A linked worktree or a subdirectory
    of a repository thus shares the main checkout's local settings.
    """
    root = canonical_git_root(cwd)
    if root is None or root in (cwd, home):
        return cwd
    return root


def read_status_line_sources(config_dir: Path, cwd: str, platform: str = sys.platform) -> StatusLineSources:
    resolved = Path(os.path.realpath(cwd))
    home = Path(os.path.realpath(os.path.expanduser("~")))
    files = (
        ("user", config_dir / "settings.json"),
        ("project", resolved / ".claude" / "settings.json"),
        ("local", local_settings_dir(resolved, home) / ".claude" / "settings.local.json"),
    )
    layers: list[tuple[str, dict[str, Any]]] = []
    for source, path in files:
        status_line = (read_settings_file(path) or {}).get(STATUS_LINE_KEY)
        if isinstance(status_line, dict):
            layers.append((source, status_line))
    return StatusLineSources(tuple(layers), managed_status_line(MANAGED_SETTINGS_DIRS.get(platform)))


def managed_status_line(directory: Path | None) -> bool:
    if directory is None:
        return False
    paths = [directory / "managed-settings.json"]
    with contextlib.suppress(OSError):
        paths += sorted((directory / "managed-settings.d").glob("*.json"))
    for path in paths:
        settings = read_settings_file(path) or {}
        if STATUS_LINE_KEY in settings or settings.get("allowManagedHooksOnly") is True:
            return True
    return False


def unreadable_file_problem(path: str) -> str | None:
    """Tell why a file cannot be read, or None when it can. Opens it without reading, and without blocking on a FIFO."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as error:
        return error.strerror or str(error)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return "not a regular file"
    finally:
        os.close(descriptor)
    return None


def take_agent_role(args: Sequence[str], roles: Mapping[str, Role]) -> tuple[str | None, list[str]]:
    """Read --agent-role from start's own region, the arguments before the first one for claude.

    Returns (the last role name or None, the arguments for claude). An --agent-role further on is an
    error, except inside a subcommand's arguments or after `--`, which belong to claude.
    """
    requested: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == AGENT_ROLE_FLAG:
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                raise LaunchError(f"{AGENT_ROLE_FLAG} needs a role name, or '{NO_ROLE}' for no role")
            requested, index = args[index + 1], index + 2
        elif arg.startswith(f"{AGENT_ROLE_FLAG}="):
            requested, index = arg.removeprefix(f"{AGENT_ROLE_FLAG}="), index + 1
        else:
            break
    rest = list(args[index:])
    # claude would reject it as an unknown option; as the value of another option it is not one.
    if not (rest and rest[0] in SUBCOMMANDS) and any(option.name == AGENT_ROLE_FLAG for option in scan(rest)):
        raise LaunchError(
            f"{AGENT_ROLE_FLAG} must come before the arguments for claude, right after the profile: claude-<profile> {AGENT_ROLE_FLAG} <name> [claude arguments...]"
        )
    if requested is None:
        return None, rest
    if not requested:
        raise LaunchError(f"{AGENT_ROLE_FLAG} needs a role name, or '{NO_ROLE}' for no role")
    if requested != NO_ROLE and requested not in roles:
        defined = ", ".join(sorted(roles)) or "none, the config has no roles: section"
        raise LaunchError(
            f"{AGENT_ROLE_FLAG}: role '{requested}' is not defined in the config (defined roles: {defined})"
        )
    return requested, rest


def is_passthrough(args: Sequence[str]) -> bool:
    """Tell whether claude runs a subcommand, or prints its version or help instead of starting a session."""
    if args and args[0] in SUBCOMMANDS:
        return True
    return has_option(args, *PASSTHROUGH_OPTIONS)


def role_args(role: Role, args: Sequence[str]) -> list[str]:
    """The claude flags of a role, without the ones the user passed: a user's single-value flag wins.

    List flags take one value each in the --flag=value form. Claude Code's option parser lets a
    variadic `--flag value` swallow the following words, the user's prompt included; --flag=value takes one.
    """
    result: list[str] = []
    for flag, value in (
        ("--effort", role.effort),
        ("--permission-mode", role.permission_mode),
        ("--autocompact", role.autocompact),
        ("--agents", role.agents),
    ):
        if value is not None and not has_option(args, flag):
            result += [flag, value]
    for flag, values in (
        ("--tools", role.tools),
        ("--allowed-tools", role.allowed_tools),
        ("--disallowed-tools", role.disallowed_tools),
        ("--plugin-dir", role.plugin_dirs),
        ("--mcp-config", role.mcp_config),
    ):
        result += [f"{flag}={value}" for value in values]
    if role.strict_mcp_config and not has_option(args, "--strict-mcp-config"):
        result.append("--strict-mcp-config")
    return result


def is_home_directory(context: LaunchContext) -> bool:
    """Tell whether the start directory is the home directory, by resolved path, so ~/. and a symlink to ~ count."""
    home = context.environ.get("HOME") or os.path.expanduser("~")
    return os.path.realpath(context.cwd) == os.path.realpath(home)


def resume_target(args: Sequence[str]) -> tuple[str, str | None] | None:
    """Tell what the arguments resume: ("id", <uuid>), ("continue", None), ("picker", None), ("session-id", <uuid>),
    or None for a new session.

    A --resume value that is no UUID is a name or a search term for the picker. --session-id counts only
    when nothing else resumes; it also sets the id of a new session, so the caller treats it as a resume
    only when the session has a start log.
    """
    resumed: tuple[str, str | None] | None = None
    session_id: str | None = None
    for option in scan(args):
        if option.name == "--resume":
            value = option.values[0] if option.values else ""
            resumed = ("id", value) if UUID_PATTERN.match(value) else ("picker", None)
        elif option.name == "--continue":
            resumed = resumed or ("continue", None)
        elif option.name == "--from-pr":
            resumed = resumed or ("picker", None)
        elif option.name == "--session-id" and option.values:
            session_id = option.values[0]
    if resumed is None and session_id is not None and UUID_PATTERN.match(session_id):
        resumed = ("session-id", session_id)
    return resumed


def env_value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()[:ROLE_ENV_HASH_LENGTH]


def role_label(role_name: str | None) -> str:
    return f"role '{role_name}'" if role_name else "no role"


def build_launch(
    profile: Profile, user_args: Sequence[str], context: LaunchContext, roles: Mapping[str, Role] | None = None
) -> Launch:
    roles = roles or {}
    requested_role, args = take_agent_role(user_args, roles)

    inherited_role_env = dict(
        entry.partition("=")[::2] for entry in context.environ.get(ROLE_ENV_KEYS_ENV, "").split(",") if "=" in entry
    )
    env = {
        key: value
        for key, value in context.environ.items()
        if not key.startswith(CLEARED_ENV_PREFIXES)
        and key not in ROLE_ENV_VARS
        and inherited_role_env.get(key) != env_value_hash(value)
        and key != ALLOW_HOME_ENV
    }
    env["CLAUDE_CONFIG_DIR"] = str(profile.config_dir)
    env["FP_CC_AUTH_SWITCH_PROFILE"] = profile.name
    env.update(profile.env)

    if is_passthrough(args):
        return Launch(args, env)

    if profile.refuse_home_cwd and context.environ.get(ALLOW_HOME_ENV) != "1" and is_home_directory(context):
        raise LaunchError(
            f"refusing to start in the home directory {context.cwd}: Claude Code would read ~/.claude/settings.json as the project settings of ~"
            f" and install every enabled plugin at project scope for it. Start in a project directory, run once with {ALLOW_HOME_ENV}=1,"
            f" or set {REFUSE_HOME_CWD_KEY}: false in the config"
        )

    notes: list[str] = []
    requested_name = None if requested_role == NO_ROLE else requested_role
    # A role name, NO_ROLE for an explicit "no role", or None when nothing chose one.
    role_name = requested_role if requested_role is not None else profile.default_role
    # The role a resumed session ran as. Its system prompt is recorded on the first request and sent
    # again on every resume, so a different role needs the snapshot off to reach the model.
    snapshot_args: list[str] = []
    resumed = resume_target(args)
    if resumed is not None and resumed[0] == "session-id":
        # "session-id" always carries the id, see resume_target.
        known = context.recorded_start(profile.config_dir, cast(str, resumed[1]), context.cwd) is not None
        resumed = ("id", resumed[1]) if known else None
    if resumed is not None:
        kind, session_id = resumed
        recorded: dict[str, Any] | None = None
        if kind == "id":
            # "id" always carries the id, see resume_target.
            recorded = context.recorded_start(profile.config_dir, cast(str, session_id), context.cwd) or {}
        elif kind == "continue":
            recorded = context.newest_start(profile.config_dir, context.cwd)
            recorded = recorded if recorded is not None else {}
        recorded_role = recorded.get("role") if recorded is not None else None
        recorded_role = recorded_role if isinstance(recorded_role, str) and recorded_role else None
        # The start log records an explicit "no role" as NO_ROLE, so that a resume keeps it.
        recorded_name = None if recorded_role == NO_ROLE else recorded_role

        if requested_role is None:
            role_name = None
            if kind == "id" and recorded_role:
                if recorded_role == NO_ROLE or recorded_role in roles:
                    role_name = recorded_role
                else:
                    notes.append(
                        f"warning: session {session_id} ran as role '{recorded_role}', which the config no longer defines; resuming it without a role"
                    )
            elif kind == "continue" and recorded_name:
                notes.append(
                    f"--continue starts without a role; the newest session here ran as role '{recorded_name}', add --agent-role {recorded_name} to keep it"
                )
        elif recorded is None or requested_name != recorded_name:
            target = (
                f"session {session_id}"
                if kind == "id"
                else "the newest session here"
                if kind == "continue"
                else "the picked session"
            )
            was = f", which ran as {role_label(recorded_name)}," if recorded is not None else ""
            if not has_option(args, SNAPSHOT_FLAG):
                snapshot_args = [SNAPSHOT_FLAG, "off"]
                notes.append(
                    f"resuming {target}{was} with {role_label(requested_name)}: {SNAPSHOT_FLAG} off sends the new system prompt, at the cost of prompt cache reuse for this run"
                )
    role = roles[role_name] if role_name and role_name != NO_ROLE else None

    prompt_args: list[str] = []
    if role_name == NO_ROLE:
        # Tells hooks, such as the agent-roles plugin's, not to inject a default role of their own.
        env[ROLE_ENV] = NO_ROLE
    if role is not None:
        env.update(role.env)
        if role.env:
            env[ROLE_ENV_KEYS_ENV] = ",".join(
                f"{key}={env_value_hash(value)}" for key, value in sorted(role.env.items())
            )
        env[ROLE_ENV] = role.name
        if role.prompt is not None:
            for source in role.prompt.files:
                if problem := context.prompt_file_problem(source):
                    raise LaunchError(f"role '{role.name}': cannot read the prompt file {source}: {problem}")
            # Claude Code keeps only the last prompt file flag, so several files reach it as one joined file.
            prompt_file = role.prompt.files[0]
            if len(role.prompt.files) > 1:
                try:
                    prompt_file = context.join_prompt_files(role.name, role.prompt.files)
                except OSError as error:
                    raise LaunchError(
                        f"role '{role.name}': cannot join the prompt files into one file: {error}"
                    ) from error
            mode = role.prompt.mode
            if mode in PROMPT_FLAGS:
                flag, user_flags = PROMPT_FLAGS[mode]
                if has_option(args, *user_flags):
                    # The user's own prompt flag wins, so the role's prompt is not in the system prompt.
                    # Mode none tells the hooks to inject it instead.
                    notes.append(
                        f"your {' or '.join(user_flags)} replaces the prompt of role '{role.name}'; {ROLE_PROMPT_MODE_ENV}=none tells hooks to inject it"
                    )
                    mode = "none"
                else:
                    prompt_args = [flag, prompt_file]
            env[ROLE_PROMPT_ENV] = prompt_file
            env[ROLE_PROMPT_FILES_ENV] = json.dumps(list(role.prompt.files), ensure_ascii=False)
            env[ROLE_PROMPT_MODE_ENV] = mode

    model = role.model if role is not None and role.model else profile.model
    model_args = ["--model", model] if model and not has_option(args, "--model") else []
    passed_args = list(args)

    # Claude Code keeps only the last --settings flag instead of merging them, so start passes one
    # --settings with every layer merged: the git config attribution, the role's settings, and the
    # user's own --settings, the last one as Claude Code would take it. The statusLine tap replaces
    # every statusLine, the user's own included, and runs that one itself.
    settings_args: list[str] = []
    launcher_settings = merge_settings(
        context.git_attribution(context.cwd), role.settings if role is not None else None
    )
    user_settings, without_settings = remove_option(passed_args, "--settings")
    # A --settings without a value stays, for claude to report.
    user_settings = [values for values in user_settings if values]
    user_layer: dict[str, Any] | None = None
    tap: dict[str, Any] | None = None
    sources = context.status_line_sources(profile.config_dir, context.cwd) if profile.capture_rate_limits else None
    if sources is not None and not sources.managed:
        try:
            user_layer = context.read_settings(user_settings[-1][0], context.cwd) if user_settings else None
        except LaunchError:
            # Only the tap needs the file then: start leaves the launch as it was, for claude to report the file.
            if launcher_settings:
                raise
        else:
            tap = status_line_tap(sources, user_layer, args, context.tool_command())
    # Without settings of its own, start leaves the user's --settings as it is.
    if launcher_settings or tap is not None:
        if user_settings:
            passed_args = without_settings
            if user_layer is None:
                user_layer = context.read_settings(user_settings[-1][0], context.cwd)
            launcher_settings = merge_settings(launcher_settings, user_layer)
        if tap is not None:
            launcher_settings[STATUS_LINE_KEY] = tap
        settings_args = ["--settings", json.dumps(launcher_settings, ensure_ascii=False)]

    name_args: list[str] = []
    tmux, pane = context.environ.get("TMUX", ""), context.environ.get("TMUX_PANE", "")
    if tmux and pane and not has_option(args, *NAME_SKIP_OPTIONS) and (tmux_session := context.tmux_session(pane)):
        name_args = ["--name", f"{tmux_session}-p{pane.removeprefix('%')}"]

    # Everything start adds goes before the user's arguments, which may end in a prompt or a `--`. The
    # list options use the --flag=value form, which takes one value: `--add-dir dir` would also take
    # the words that follow it, the user's prompt included.
    add_dir_args = [f"--add-dir={directory}" for directory in profile.add_dirs]
    role_launch_args: list[str] = []
    if role is not None:
        add_dir_args += [f"--add-dir={directory}" for directory in role.add_dirs]
        role_launch_args = [*role_args(role, args), *prompt_args, *role.extra_args]
    return Launch(
        [*add_dir_args, *settings_args, *name_args, *role_launch_args, *snapshot_args, *model_args, *passed_args],
        env,
        tuple(notes),
    )


def project_dir_names(cwd: str) -> list[str]:
    """Glob patterns for the projects/ directory of a cwd, by the path as given and resolved."""
    names: list[str] = []
    for path in dict.fromkeys((cwd, os.path.realpath(cwd))):
        name = re.sub(r"[^a-zA-Z0-9]", "-", path)
        # The name holds only letters, digits and "-", so it needs no glob escaping.
        names.append(name if len(name) <= PROJECT_DIR_NAME_LIMIT else name[:PROJECT_DIR_NAME_LIMIT] + "-*")
    return names


def recorded_start_of(config_dir: Path, session_id: str, cwd: str) -> dict[str, Any] | None:
    from claude_code_auth_switch.session_start import LOG_NAME, read_newest_line

    # A resume can start anywhere, and a session that moved between directories, or a copy of it, can
    # have a log in more than one project directory. The newest line by ts wins. Without a ts, or on a
    # tie, the log in the start directory's project directory wins, then the log written last.
    cwd_patterns = project_dir_names(cwd)
    ranked: list[tuple[tuple[int, bool, float], dict[str, Any]]] = []
    for log in (config_dir / "projects").glob(f"*/{session_id}/{LOG_NAME}"):
        line = read_newest_line(log)
        if line is None:
            continue
        in_cwd = any(fnmatch.fnmatchcase(log.parent.parent.name, pattern) for pattern in cwd_patterns)
        try:
            mtime = log.stat().st_mtime
        except OSError:
            mtime = float("-inf")
        ranked.append(((line_ts(line), in_cwd, mtime), line))
    return max(ranked, key=lambda entry: entry[0])[1] if ranked else None


def line_ts(line: dict[str, Any]) -> int:
    ts = line.get("ts")
    return ts if isinstance(ts, int) and not isinstance(ts, bool) else -1


def newest_start_in(config_dir: Path, cwd: str) -> dict[str, Any] | None:
    from claude_code_auth_switch.session_start import LOG_NAME, read_newest_line

    newest: tuple[float, Path] | None = None
    for pattern in project_dir_names(cwd):
        for transcript in (config_dir / "projects").glob(f"{pattern}/*.jsonl"):
            try:
                mtime = transcript.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest[0]:
                newest = (mtime, transcript)
    if newest is None:
        return None
    return read_newest_line(newest[1].with_suffix("") / LOG_NAME) or {}


def resolve_profile(
    config: dict[str, Any], secrets: dict[str, dict[str, str]], profiles_base: Path, name: str
) -> Profile:
    """Turn a profile's config entry into a Profile, validating every key that a launch uses."""
    from claude_code_auth_switch.config import expand_path, merge_secret_env, pinned_models_to_env

    profiles = config["profiles"]
    if name not in profiles:
        raise ConfigError(f"profile '{name}' is not defined")
    profile_config = profiles[name] or {}
    if not isinstance(profile_config, dict):
        raise ConfigError(f"profile '{name}' must be a mapping")

    def mapping(key: str) -> dict[str, Any]:
        value = profile_config.get(key) or {}
        if not isinstance(value, dict):
            raise ConfigError(f"profile '{name}': '{key}' must be a mapping")
        return value

    env_vars = {str(key): str(value) for key, value in mapping("env").items()}
    env_vars = merge_secret_env(name, env_vars, secrets.get(name, {}))
    # An explicit env: entry wins over the value derived from pinned_models.
    env_vars = {**pinned_models_to_env(name, mapping("pinned_models")), **env_vars}
    for key, value in env_vars.items():
        if not ENV_NAME.fullmatch(key):
            raise ConfigError(
                f"profile '{name}': env variable name {key!r} is not a valid shell variable name (letters, digits and _, not starting with a digit)"
            )
        if "\0" in value:
            raise ConfigError(f"profile '{name}': env value of '{key}' contains a NUL character")

    add_dirs = profile_config.get("add_dirs") or []
    if not isinstance(add_dirs, list) or not all(isinstance(directory, str) for directory in add_dirs):
        raise ConfigError(f"profile '{name}': 'add_dirs' must be a list of paths")
    if any("\0" in directory for directory in add_dirs):
        raise ConfigError(f"profile '{name}': an 'add_dirs' entry contains a NUL character")

    default_role = profile_config.get("default_role")
    if default_role is not None:
        if not isinstance(default_role, str) or not default_role:
            raise ConfigError(f"profile '{name}': 'default_role' must be a role name")
        if default_role != NO_ROLE and default_role not in resolve_roles(config):
            raise ConfigError(
                f"profile '{name}': default_role '{default_role}' is not defined under roles: (or '{NO_ROLE}' for no role)"
            )

    refuse_home_cwd = config.get(REFUSE_HOME_CWD_KEY, True)
    if not isinstance(refuse_home_cwd, bool):
        raise ConfigError(f"'{REFUSE_HOME_CWD_KEY}' must be true or false, not {refuse_home_cwd!r}")
    # The profile's value wins over the top-level one.
    refuse_home_cwd = profile_config.get(REFUSE_HOME_CWD_KEY, refuse_home_cwd)
    if not isinstance(refuse_home_cwd, bool):
        raise ConfigError(f"profile '{name}': '{REFUSE_HOME_CWD_KEY}' must be true or false, not {refuse_home_cwd!r}")

    capture_rate_limits = config.get(CAPTURE_RATE_LIMITS_KEY, True)
    if not isinstance(capture_rate_limits, bool):
        raise ConfigError(f"'{CAPTURE_RATE_LIMITS_KEY}' must be true or false, not {capture_rate_limits!r}")
    capture_rate_limits = profile_config.get(CAPTURE_RATE_LIMITS_KEY, capture_rate_limits)
    if not isinstance(capture_rate_limits, bool):
        raise ConfigError(
            f"profile '{name}': '{CAPTURE_RATE_LIMITS_KEY}' must be true or false, not {capture_rate_limits!r}"
        )

    model = profile_config.get("model") or None
    if model is not None and "\0" in str(model):
        raise ConfigError(f"profile '{name}': 'model' contains a NUL character")
    if "\0" in str(profiles_base):
        raise ConfigError("'profiles_dir' contains a NUL character")
    return Profile(
        name=name,
        config_dir=profiles_base / name,
        env=env_vars,
        add_dirs=tuple(expand_path(directory) for directory in add_dirs),
        model=str(model) if model is not None else None,
        default_role=default_role,
        refuse_home_cwd=refuse_home_cwd,
        capture_rate_limits=capture_rate_limits,
    )


def load_profile(config_path: Path, name: str) -> tuple[Profile, dict[str, Role]]:
    """Read the config and the secrets file and resolve one profile and all roles. Exits or raises ConfigError on a broken config."""
    from claude_code_auth_switch.config import (
        DEFAULT_PROFILES_DIR,
        check_secrets_git_safety,
        expand_path,
        load_config,
        load_secrets,
        secrets_path_for,
    )

    if not config_path.exists():
        raise ConfigError("the file does not exist")
    config = load_config(config_path)
    secrets: dict[str, dict[str, str]] = {}
    secrets_path = secrets_path_for(config_path)
    if secrets_path.exists():
        check_secrets_git_safety(secrets_path)
        secrets = load_secrets(secrets_path, config)
    profiles_base = expand_path(str(config.get("profiles_dir", DEFAULT_PROFILES_DIR)))
    return resolve_profile(config, secrets, profiles_base, name), resolve_roles(config)


def logical_cwd(environ: Mapping[str, str]) -> str:
    """The working directory as the shell names it, symlinks kept, like bash's $PWD."""
    pwd = environ.get("PWD", "")
    try:
        if os.path.isabs(pwd) and os.path.samefile(pwd, "."):
            return pwd
    except OSError:
        pass
    return os.getcwd()


def tmux_session_of(pane: str) -> str | None:
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#S"], capture_output=True, timeout=TMUX_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return os.fsdecode(result.stdout).rstrip("\n") or None


def git_attribution_of(directory: str) -> dict[str, Any] | None:
    from claude_code_auth_switch import git_attribution

    try:
        return git_attribution.settings_for(directory)
    except Exception as error:  # noqa: BLE001 -- a broken git config costs the attribution, never the launch
        git_attribution.warn(f"cannot read attribution from git config: {error}")
        return None


def parse_args(argv: Sequence[str]) -> tuple[Path | None, str, list[str]]:
    """Split `[--config PATH] <profile> [claude arguments...]`. Everything after the profile goes to Claude unchanged."""
    args = list(argv)
    config: Path | None = None
    if args and args[0] == "--config" and len(args) >= 2:
        config, args = Path(args[1]), args[2:]
    elif args and args[0].startswith("--config="):
        config, args = Path(args[0].removeprefix("--config=")), args[1:]
    if not args or args[0].startswith("-"):
        raise ValueError(f"usage: {PROG} [--config PATH] <profile> [claude arguments...]")
    return config, args[0], args[1:]


def main(argv: Sequence[str]) -> int:
    from claude_code_auth_switch.config import DEFAULT_CONFIG_PATH, LEGACY_CONFIG_PATH, resolve_config_path

    try:
        config_arg, name, user_args = parse_args(argv)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    config_path = resolve_config_path(config_arg, DEFAULT_CONFIG_PATH, LEGACY_CONFIG_PATH)
    try:
        return launch_profile(config_path, name, user_args)
    except Exception as error:  # noqa: BLE001 -- any failure must stop the launch with a message, not a traceback
        if os.environ.get(DEBUG_ENV) == "1":
            raise
        print(
            f"{PROG}: not starting Claude for profile '{name}' with {config_path}: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        print(f"{PROG}: set {DEBUG_ENV}=1 to see the traceback", file=sys.stderr)
        return 1


def launch_profile(config_path: Path, name: str, user_args: list[str]) -> int:
    """Resolve the profile and replace this process with claude. Returns only when the launch cannot happen."""
    try:
        profile, roles = load_profile(config_path, name)
    except ConfigError as error:
        print(f"Error: {config_path}: {error}", file=sys.stderr)
        print(f"{PROG}: not starting Claude for profile '{name}'", file=sys.stderr)
        return 1
    except SystemExit as error:
        # The config helpers print their error and exit; add which launch they stopped.
        print(f"{PROG}: not starting Claude for profile '{name}', fix {config_path} first", file=sys.stderr)
        return error.code if isinstance(error.code, int) and error.code != 0 else 1

    context = LaunchContext(
        cwd=logical_cwd(os.environ),
        environ=os.environ,
        tmux_session=tmux_session_of,
        git_attribution=git_attribution_of,
    )
    try:
        launch = build_launch(profile, user_args, context, roles)
    except LaunchError as error:
        print(f"{PROG}: {error}", file=sys.stderr)
        print(f"{PROG}: not starting Claude for profile '{name}'", file=sys.stderr)
        return 2
    for note in launch.notes:
        print(f"{PROG}: {note}", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvpe("claude", ["claude", *launch.argv], launch.env)
    except OSError as error:
        print(f"{PROG}: cannot run claude: {error}", file=sys.stderr)
        return 127
    return 0  # not reached
