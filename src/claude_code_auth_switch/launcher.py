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
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

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


class ConfigError(Exception):
    """The config does not describe a launchable profile."""


@dataclass(frozen=True)
class Profile:
    name: str
    config_dir: Path
    # Final values: pinned_models, then env: and the secrets file on top.
    env: dict[str, str] = field(default_factory=dict)
    add_dirs: tuple[Path, ...] = ()
    model: str | None = None


@dataclass(frozen=True)
class LaunchContext:
    """What a launch reads from the machine it runs on."""

    cwd: str
    environ: Mapping[str, str]
    # Name of the tmux session that holds a pane, or None when tmux cannot say.
    tmux_session: Callable[[str], str | None]
    # Settings from the [claude-code "attribution"] git config for a directory, or None.
    git_attribution: Callable[[str], dict | None]


@dataclass(frozen=True)
class Launch:
    argv: list[str]
    env: dict[str, str]


def has_flag(args: Sequence[str], *flags: str, with_value: Sequence[str] = ()) -> bool:
    """Tell whether any argument is one of flags, or --flag=value for a flag in with_value.

    Every argument is checked, also one that is the value of another option, as the bash wrappers did.
    """
    return any(arg in flags or any(arg.startswith(f"{flag}=") for flag in with_value) for arg in args)


def merge_settings(*layers: dict | None) -> dict:
    """Deep-merge settings objects; a later layer wins per key, nested objects merge."""
    merged: dict = {}
    for layer in layers:
        for key, value in (layer or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = merge_settings(merged[key], value)
            else:
                merged[key] = value
    return merged


def build_launch(profile: Profile, user_args: Sequence[str], context: LaunchContext) -> Launch:
    env = {key: value for key, value in context.environ.items() if not key.startswith(CLEARED_ENV_PREFIXES)}
    env["CLAUDE_CONFIG_DIR"] = str(profile.config_dir)
    env["FP_CC_AUTH_SWITCH_PROFILE"] = profile.name
    env.update(profile.env)

    if user_args and user_args[0] in SUBCOMMANDS:
        return Launch(list(user_args), env)

    args = list(user_args)
    if profile.model and not has_flag(user_args, "--model", with_value=["--model"]):
        args += ["--model", profile.model]

    # Claude Code keeps only the last --settings flag instead of merging them, so a --settings from the
    # user replaces everything the launcher would pass.
    settings_args: list[str] = []
    if not has_flag(user_args, "--settings", with_value=["--settings"]):
        settings = merge_settings(context.git_attribution(context.cwd))
        if settings:
            settings_args = ["--settings", json.dumps(settings, ensure_ascii=False)]

    name_args: list[str] = []
    tmux, pane = context.environ.get("TMUX", ""), context.environ.get("TMUX_PANE", "")
    if tmux and pane and not has_flag(user_args, *NAME_SKIP_FLAGS, with_value=NAME_SKIP_FLAGS_WITH_VALUE):
        if tmux_session := context.tmux_session(pane):
            name_args = ["--name", f"{tmux_session}-p{pane.removeprefix('%')}"]

    add_dir_args = [arg for directory in profile.add_dirs for arg in ("--add-dir", str(directory))]
    return Launch([*add_dir_args, *settings_args, *name_args, *args], env)


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

    add_dirs = profile_config.get("add_dirs") or []
    if not isinstance(add_dirs, list) or not all(isinstance(directory, str) for directory in add_dirs):
        raise ConfigError(f"profile '{name}': 'add_dirs' must be a list of paths")

    model = profile_config.get("model") or None
    return Profile(
        name=name,
        config_dir=profiles_base / name,
        env=env_vars,
        add_dirs=tuple(expand_path(directory) for directory in add_dirs),
        model=str(model) if model is not None else None,
    )


def load_profile(config_path: Path, name: str) -> Profile:
    """Read the config and the secrets file and resolve one profile. Exits or raises ConfigError on a broken config."""
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
    return resolve_profile(config, secrets, profiles_base, name)


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
        profile = load_profile(config_path, name)
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
    launch = build_launch(profile, user_args, context)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvpe("claude", ["claude", *launch.argv], launch.env)
    except OSError as error:
        print(f"{PROG}: cannot run claude: {error}", file=sys.stderr)
        return 127
    return 0  # not reached
