"""
`cc-auth-switch start [--config PATH] <profile> [claude arguments...]`, which every claude-<profile> launcher runs.

It reads auth-switch.yaml and the secrets file on every launch, builds the environment and the arguments
for Claude Code, and replaces itself with `claude`. A config that cannot be read or is invalid stops the
launch with the config path and the error, so a broken config never starts Claude under the wrong account.

build_launch() turns a resolved profile, the user's arguments and the launch context into argv and env.
It runs no processes and reads no files itself, so tests can call it directly.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from claude_code_auth_switch.config import ENV_NAME, ConfigError
from claude_code_auth_switch.roles import NO_ROLE, Role, resolve_roles

PROG = "cc-auth-switch start"

# Claude Code subcommands reject --add-dir, --model and the other session options with "unknown option",
# so arguments that start with one of them pass through unchanged.
SUBCOMMANDS = frozenset({
    "mcp", "auth", "doctor", "install", "setup-token", "update", "upgrade", "agents", "auto-mode", "plugin", "plugins",
    "attach", "logs", "respawn", "rm", "stop", "kill", "purge", "import", "gateway", "ultrareview",
})

# A name given at launch replaces the saved title of a resumed session, /rename included, so the tmux
# name is left out when the user names the session or resumes one.
NAME_SKIP_FLAGS = ("-n", "--name", "-r", "--resume", "-c", "--continue", "--session-id", "--fork-session", "--from-pr")
NAME_SKIP_FLAGS_WITH_VALUE = ("--name", "--resume", "--session-id", "--from-pr")

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
ROLE_ENV_VARS = (ROLE_ENV, ROLE_PROMPT_ENV, ROLE_PROMPT_MODE_ENV)

# Set to 1 to get the traceback of an unexpected error in `start` instead of a one-line message.
DEBUG_ENV = "CC_AUTH_SWITCH_DEBUG"

AGENT_ROLE_FLAG = "--agent-role"

# With one of these, claude prints and exits without a session, so start passes the arguments through
# as for a subcommand: no role, no prompt file check, no session name.
PASSTHROUGH_FLAGS = frozenset({"-v", "--version", "-h", "--help"})

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
    # Role applied when the command line has no --agent-role.
    default_role: str | None = None


@dataclass(frozen=True)
class LaunchContext:
    """What a launch reads from the machine it runs on."""

    cwd: str
    environ: Mapping[str, str]
    # Name of the tmux session that holds a pane, or None when tmux cannot say.
    tmux_session: Callable[[str], str | None]
    # Settings from the [claude-code "attribution"] git config for a directory, or None.
    git_attribution: Callable[[str], dict | None]
    # The settings object of a user's --settings value, inline JSON or a file path relative to the cwd.
    # Raises LaunchError when it cannot be read.
    read_settings: Callable[[str, str], dict] = lambda value, cwd: read_settings_value(value, cwd)
    is_file: Callable[[str], bool] = os.path.isfile
    # The newest start-log line of a session (config dir, session id, cwd), or None without a readable log.
    recorded_start: Callable[[Path, str, str], dict | None] = lambda config_dir, session_id, cwd: recorded_start_of(config_dir, session_id, cwd)
    # The newest start-log line of the newest session in a directory (config dir, cwd), as -c would continue it.
    # An empty dict when that session has no log, None when the directory has no session.
    newest_start: Callable[[Path, str], dict | None] = lambda config_dir, cwd: newest_start_in(config_dir, cwd)


@dataclass(frozen=True)
class Launch:
    argv: list[str]
    env: dict[str, str]
    # One-line notes for stderr, about what start did with the role.
    notes: tuple[str, ...] = ()


def has_flag(args: Sequence[str], *flags: str, with_value: Sequence[str] = ()) -> bool:
    """Tell whether any argument is one of flags, or --flag=value for a flag in with_value.

    Every argument is checked, also one that is the value of another option, as the bash wrappers did.
    """
    return any(arg in flags or any(arg.startswith(f"{flag}=") for flag in with_value) for arg in args)


def pop_option(args: Sequence[str], flag: str) -> tuple[list[str], list[str]]:
    """Remove every `flag value` and `flag=value` before a `--` from args. Returns (values, remaining args).

    A flag without a value is left in place, for Claude Code to report.
    """
    values: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            rest += args[index:]
            break
        if arg == flag and index + 1 < len(args):
            values.append(args[index + 1])
            index += 2
            continue
        if arg.startswith(f"{flag}="):
            values.append(arg.removeprefix(f"{flag}="))
        else:
            rest.append(arg)
        index += 1
    return values, rest


def merge_settings(*layers: dict | None) -> dict:
    """Deep-merge settings objects; a later layer wins per key, nested objects merge.

    Lists are concatenated without duplicates, as Claude Code merges its own settings sources, so a
    permissions.allow list in a later layer adds rules instead of replacing the earlier ones.
    """
    merged: dict = {}
    for layer in layers:
        for key, value in (layer or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = merge_settings(merged[key], value)
            elif isinstance(value, list) and isinstance(merged.get(key), list):
                merged[key] = merged[key] + [item for item in value if item not in merged[key]]
            else:
                merged[key] = value
    return merged


def read_settings_value(value: str, cwd: str) -> dict:
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
    if not (rest and rest[0] in SUBCOMMANDS):
        before_terminator = rest[: rest.index("--")] if "--" in rest else rest
        if any(arg == AGENT_ROLE_FLAG or arg.startswith(f"{AGENT_ROLE_FLAG}=") for arg in before_terminator):
            raise LaunchError(f"{AGENT_ROLE_FLAG} must come before the arguments for claude, right after the profile: claude-<profile> {AGENT_ROLE_FLAG} <name> [claude arguments...]")
    if requested is None:
        return None, rest
    if not requested:
        raise LaunchError(f"{AGENT_ROLE_FLAG} needs a role name, or '{NO_ROLE}' for no role")
    if requested != NO_ROLE and requested not in roles:
        defined = ", ".join(sorted(roles)) or "none, the config has no roles: section"
        raise LaunchError(f"{AGENT_ROLE_FLAG}: role '{requested}' is not defined in the config (defined roles: {defined})")
    return requested, rest


def is_passthrough(args: Sequence[str]) -> bool:
    """Tell whether claude runs a subcommand, or prints its version or help instead of starting a session."""
    if args and args[0] in SUBCOMMANDS:
        return True
    before_terminator = args[: list(args).index("--")] if "--" in args else args
    return any(arg in PASSTHROUGH_FLAGS for arg in before_terminator)


def role_args(role: Role, args: Sequence[str]) -> list[str]:
    """The claude flags of a role, without the ones the user passed: a user's single-value flag wins.

    List flags take one value each in the --flag=value form. Claude Code's option parser lets a
    variadic `--flag value` swallow the following words, the user's prompt included; --flag=value takes one.
    """
    result: list[str] = []
    for flag, value in (("--effort", role.effort), ("--permission-mode", role.permission_mode), ("--autocompact", role.autocompact), ("--agents", role.agents)):
        if value is not None and not has_flag(args, flag, with_value=[flag]):
            result += [flag, value]
    for flag, values in (
        ("--tools", role.tools), ("--allowed-tools", role.allowed_tools), ("--disallowed-tools", role.disallowed_tools),
        ("--plugin-dir", role.plugin_dirs), ("--mcp-config", role.mcp_config),
    ):
        result += [f"{flag}={value}" for value in values]
    if role.strict_mcp_config and "--strict-mcp-config" not in args:
        result.append("--strict-mcp-config")
    return result


def resume_target(args: Sequence[str]) -> tuple[str, str | None] | None:
    """Tell what the arguments resume: ("id", <uuid>), ("continue", None), ("picker", None), ("session-id", <uuid>),
    or None for a new session.

    A --resume value that is no UUID is a name or a search term for the picker. --session-id counts only
    when nothing else resumes; it also sets the id of a new session, so the caller treats it as a resume
    only when the session has a start log.
    """
    resumed: tuple[str, str | None] | None = None
    session_id: str | None = None
    for index, arg in enumerate(args):
        if arg == "--":
            break
        following = args[index + 1] if index + 1 < len(args) else None
        value: str | None = None
        if arg in ("-r", "--resume"):
            value = following if following is not None and not following.startswith("-") else ""
        elif arg.startswith("--resume="):
            value = arg.removeprefix("--resume=")
        elif arg.startswith("-r") and not arg.startswith("--") and len(arg) > 2:
            value = arg[2:]
        elif arg in ("-c", "--continue"):
            resumed = resumed or ("continue", None)
        elif arg == "--from-pr" or arg.startswith("--from-pr="):
            resumed = resumed or ("picker", None)
        elif arg == "--session-id" and following is not None:
            session_id = following
        elif arg.startswith("--session-id="):
            session_id = arg.removeprefix("--session-id=")
        if value is not None:
            resumed = ("id", value) if UUID_PATTERN.match(value) else ("picker", None)
    if resumed is None and session_id is not None and UUID_PATTERN.match(session_id):
        resumed = ("session-id", session_id)
    return resumed


def role_label(role_name: str | None) -> str:
    return f"role '{role_name}'" if role_name else "no role"


def build_launch(profile: Profile, user_args: Sequence[str], context: LaunchContext, roles: Mapping[str, Role] | None = None) -> Launch:
    roles = roles or {}
    requested_role, args = take_agent_role(user_args, roles)

    env = {key: value for key, value in context.environ.items() if not key.startswith(CLEARED_ENV_PREFIXES) and key not in ROLE_ENV_VARS}
    env["CLAUDE_CONFIG_DIR"] = str(profile.config_dir)
    env["FP_CC_AUTH_SWITCH_PROFILE"] = profile.name
    env.update(profile.env)

    if is_passthrough(args):
        return Launch(args, env)

    notes: list[str] = []
    requested_name = None if requested_role == NO_ROLE else requested_role
    role_name = requested_name if requested_role is not None else profile.default_role
    # The role a resumed session ran as. Its system prompt is recorded on the first request and sent
    # again on every resume, so a different role needs the snapshot off to reach the model.
    snapshot_args: list[str] = []
    resumed = resume_target(args)
    if resumed is not None and resumed[0] == "session-id":
        known = context.recorded_start(profile.config_dir, resumed[1], context.cwd) is not None
        resumed = ("id", resumed[1]) if known else None
    if resumed is not None:
        kind, session_id = resumed
        recorded: dict | None = None
        if kind == "id":
            recorded = context.recorded_start(profile.config_dir, session_id, context.cwd) or {}
        elif kind == "continue":
            recorded = context.newest_start(profile.config_dir, context.cwd)
            recorded = recorded if recorded is not None else {}
        recorded_role = recorded.get("role") if recorded is not None else None
        recorded_role = recorded_role if isinstance(recorded_role, str) and recorded_role else None

        if requested_role is None:
            role_name = None
            if kind == "id" and recorded_role:
                if recorded_role in roles:
                    role_name = recorded_role
                else:
                    notes.append(f"warning: session {session_id} ran as role '{recorded_role}', which the config no longer defines; resuming it without a role")
            elif kind == "continue" and recorded_role:
                notes.append(f"--continue starts without a role; the newest session here ran as role '{recorded_role}', add --agent-role {recorded_role} to keep it")
        elif recorded is None or requested_name != recorded_role:
            target = f"session {session_id}" if kind == "id" else "the newest session here" if kind == "continue" else "the picked session"
            was = f", which ran as {role_label(recorded_role)}," if recorded is not None else ""
            if not has_flag(args, SNAPSHOT_FLAG, with_value=[SNAPSHOT_FLAG]):
                snapshot_args = [SNAPSHOT_FLAG, "off"]
                notes.append(f"resuming {target}{was} with {role_label(requested_name)}: {SNAPSHOT_FLAG} off sends the new system prompt, at the cost of prompt cache reuse for this run")
    role = roles[role_name] if role_name else None

    prompt_args: list[str] = []
    if role is not None:
        env.update(role.env)
        env[ROLE_ENV] = role.name
        if role.prompt is not None:
            if not context.is_file(role.prompt.file):
                raise LaunchError(f"role '{role.name}': the prompt file {role.prompt.file} does not exist")
            mode = role.prompt.mode
            if mode in PROMPT_FLAGS:
                flag, user_flags = PROMPT_FLAGS[mode]
                if has_flag(args, *user_flags, with_value=user_flags):
                    # The user's own prompt flag wins, so the role's prompt is not in the system prompt.
                    # Mode none tells the hooks to inject it instead.
                    notes.append(f"your {' or '.join(user_flags)} replaces the prompt of role '{role.name}'; {ROLE_PROMPT_MODE_ENV}=none tells hooks to inject it")
                    mode = "none"
                else:
                    prompt_args = [flag, role.prompt.file]
            env[ROLE_PROMPT_ENV] = role.prompt.file
            env[ROLE_PROMPT_MODE_ENV] = mode

    model = role.model if role is not None and role.model else profile.model
    model_args = ["--model", model] if model and not has_flag(args, "--model", with_value=["--model"]) else []
    passed_args = list(args)

    # Claude Code keeps only the last --settings flag instead of merging them, so start passes one
    # --settings with every layer merged: the git config attribution, the role's settings, and the
    # user's own --settings, the last one as Claude Code would take it.
    settings_args: list[str] = []
    launcher_settings = merge_settings(context.git_attribution(context.cwd), role.settings if role is not None else None)
    # Without settings of its own, start leaves the user's --settings as it is.
    if launcher_settings:
        user_settings, without_settings = pop_option(passed_args, "--settings")
        if user_settings:
            passed_args = without_settings
            launcher_settings = merge_settings(launcher_settings, context.read_settings(user_settings[-1], context.cwd))
        settings_args = ["--settings", json.dumps(launcher_settings, ensure_ascii=False)]

    name_args: list[str] = []
    tmux, pane = context.environ.get("TMUX", ""), context.environ.get("TMUX_PANE", "")
    if tmux and pane and not has_flag(args, *NAME_SKIP_FLAGS, with_value=NAME_SKIP_FLAGS_WITH_VALUE):
        if tmux_session := context.tmux_session(pane):
            name_args = ["--name", f"{tmux_session}-p{pane.removeprefix('%')}"]

    # Everything start adds goes before the user's arguments, which may end in a prompt or a `--`. The
    # list options use the --flag=value form, which takes one value: `--add-dir dir` would also take
    # the words that follow it, the user's prompt included.
    add_dir_args = [f"--add-dir={directory}" for directory in profile.add_dirs]
    role_launch_args: list[str] = []
    if role is not None:
        add_dir_args += [f"--add-dir={directory}" for directory in role.add_dirs]
        role_launch_args = [*role_args(role, args), *prompt_args, *role.extra_args]
    return Launch([*add_dir_args, *settings_args, *name_args, *role_launch_args, *snapshot_args, *model_args, *passed_args], env, tuple(notes))


def project_dir_names(cwd: str) -> list[str]:
    """Glob patterns for the projects/ directory of a cwd, by the path as given and resolved."""
    names: list[str] = []
    for path in dict.fromkeys((cwd, os.path.realpath(cwd))):
        name = re.sub(r"[^a-zA-Z0-9]", "-", path)
        # The name holds only letters, digits and "-", so it needs no glob escaping.
        names.append(name if len(name) <= PROJECT_DIR_NAME_LIMIT else name[:PROJECT_DIR_NAME_LIMIT] + "-*")
    return names


def recorded_start_of(config_dir: Path, session_id: str, cwd: str) -> dict | None:
    from claude_code_auth_switch.session_start import LOG_NAME, read_newest_line

    projects = config_dir / "projects"
    for pattern in [*project_dir_names(cwd), "*"]:
        for log in projects.glob(f"{pattern}/{session_id}/{LOG_NAME}"):
            return read_newest_line(log)
    return None


def newest_start_in(config_dir: Path, cwd: str) -> dict | None:
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


def resolve_profile(config: dict, secrets: dict[str, dict[str, str]], profiles_base: Path, name: str) -> Profile:
    """Turn a profile's config entry into a Profile, validating every key that a launch uses."""
    from claude_code_auth_switch.config import expand_path, merge_secret_env, pinned_models_to_env

    profiles = config["profiles"]
    if name not in profiles:
        raise ConfigError(f"profile '{name}' is not defined")
    profile_config = profiles[name] or {}
    if not isinstance(profile_config, dict):
        raise ConfigError(f"profile '{name}' must be a mapping")

    def mapping(key: str) -> dict:
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
            raise ConfigError(f"profile '{name}': env variable name {key!r} is not a valid shell variable name (letters, digits and _, not starting with a digit)")
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
        if default_role not in resolve_roles(config):
            raise ConfigError(f"profile '{name}': default_role '{default_role}' is not defined under roles:")

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
        result = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#S"], capture_output=True, timeout=TMUX_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return os.fsdecode(result.stdout).rstrip("\n") or None


def git_attribution_of(directory: str) -> dict | None:
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
        print(f"{PROG}: not starting Claude for profile '{name}' with {config_path}: {type(error).__name__}: {error}", file=sys.stderr)
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
