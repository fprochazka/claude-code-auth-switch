"""Reading and validating auth-switch.yaml and its secrets file."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("~/.claude/auth-switch.yaml").expanduser()
# Kept working so an existing install does not break when ~/.claude/auth-switch.yaml is absent.
LEGACY_CONFIG_PATH = Path("~/.claude-profiles/config.yaml").expanduser()
DEFAULT_SOURCE_DIR = Path("~/.claude").expanduser()
DEFAULT_BIN_DIR = Path("~/.local/bin").expanduser()
DEFAULT_PROFILES_DIR = Path("~/.claude-profiles").expanduser()

# The secrets check runs on every launch, so a git that hangs must not hang the launch with it.
GIT_TIMEOUT_SECONDS = 5

# Model classes whose alias (sonnet/opus/haiku/fable) can be pinned to a specific
# version id via the corresponding ANTHROPIC_DEFAULT_<CLASS>_MODEL env var.
VALID_MODEL_CLASSES = ("sonnet", "opus", "haiku", "fable")


# Names that bash's `export NAME=...` accepted; the bash wrappers of earlier versions stopped at any other.
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ConfigError(Exception):
    """The config does not describe a launchable profile or role."""


def read_yaml(path: Path) -> object:
    """Parse a YAML file; an unreadable file or a syntax error stops the run with the path and the error."""
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as error:
        print(f"Error: cannot read {path}: {error}", file=sys.stderr)
        sys.exit(1)


def load_config(config_path: Path) -> dict:
    config = read_yaml(config_path)
    if not isinstance(config, dict) or "profiles" not in config:
        print(f"Error: No profiles defined in {config_path}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(config["profiles"], dict):
        print(f"Error: {config_path}: 'profiles' must be a mapping of profile name to settings", file=sys.stderr)
        sys.exit(1)
    # A name becomes a directory under profiles_dir and the launcher claude-<name>, so it is checked
    # here, before the installer writes anything for the profiles that come before it.
    for name in config["profiles"]:
        if not isinstance(name, str) or not name or "/" in name or "\0" in name:
            print(
                f"Error: {config_path}: profile name {name!r} must be a non-empty string without '/' (quote it in YAML if it is a number)",
                file=sys.stderr,
            )
            sys.exit(1)
    return config


def resolve_config_path(cli_arg: Path | None, new_default: Path, legacy_default: Path) -> Path:
    """Pick the config file to read.

    An explicit --config always wins, even when it does not exist, so the caller reports the
    path the user actually asked for. Otherwise the new default wins, and the legacy path is
    only used when the new default is absent.
    """
    if cli_arg is not None:
        return cli_arg.expanduser()
    if new_default.exists():
        return new_default
    if legacy_default.exists():
        print(
            f"Note: using legacy config {legacy_default} — the preferred location is {new_default}",
            file=sys.stderr,
        )
        return legacy_default
    return new_default


def secrets_path_for(config_path: Path) -> Path:
    """Sibling secrets file: auth-switch.yaml -> auth-switch.secrets.yaml."""
    return config_path.with_name(f"{config_path.stem}.secrets.yaml")


def check_secrets_git_safety(secrets_path: Path) -> None:
    """Refuse to run when the secrets file could reach a git remote.

    Tracked file -> fatal. Inside a repo but not ignored -> fatal. Outside a repo, or without a
    git binary, there is nothing to check. Git output stays captured so no path or content leaks.
    A git that does not answer in time is fatal too, because the file cannot be shown safe without it.
    """
    # git -C reads a relative path against that directory, not against the current one.
    directory = str(secrets_path.parent.absolute())
    name = str(secrets_path.absolute())

    def run(*git_args: str) -> int | None:
        try:
            return subprocess.run(
                ["git", "-C", directory, *git_args],
                capture_output=True,
                timeout=GIT_TIMEOUT_SECONDS,
            ).returncode
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired:
            print(
                f"Error: git did not answer within {GIT_TIMEOUT_SECONDS} seconds while checking whether secrets file {secrets_path} is tracked or ignored.",
                file=sys.stderr,
            )
            print(
                f"Without that check the file cannot be shown safe. Run `git -C {directory} status` to see why git hangs.",
                file=sys.stderr,
            )
            sys.exit(1)

    tracked = run("ls-files", "--error-unmatch", name)
    if tracked is None:
        return
    if tracked == 0:
        print(f"Error: Secrets file {secrets_path} is tracked in git.", file=sys.stderr)
        print(f"Remove it from the index and ignore it:", file=sys.stderr)
        print(f"  git -C {directory} rm --cached {secrets_path.name}", file=sys.stderr)
        print(f"  echo '{secrets_path.name}' >> {secrets_path.parent / '.gitignore'}", file=sys.stderr)
        sys.exit(1)
    if tracked == 128:
        return

    ignored = run("check-ignore", "-q", name)
    if ignored in (0, 128, None):
        return
    print(f"Error: Secrets file {secrets_path} is inside a git repository but is not ignored.", file=sys.stderr)
    print(f"Add it to .gitignore before running the installer:", file=sys.stderr)
    print(f"  echo '{secrets_path.name}' >> {secrets_path.parent / '.gitignore'}", file=sys.stderr)
    sys.exit(1)


def load_secrets(secrets_path: Path, config: dict) -> dict[str, dict[str, str]]:
    """Read the secrets file into {profile_name: {env_key: value}}.

    The file mirrors the main config but allows only profiles.<name>.env with string values.
    Anything else aborts the install, so a typo cannot silently drop a credential.
    """
    data = read_yaml(secrets_path)
    if data is None:
        return {}

    def fatal(message: str) -> None:
        print(f"Error: {secrets_path}: {message}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        fatal("expected a mapping with a single top-level 'profiles' key")
    if extra := sorted(set(data) - {"profiles"}):
        fatal(f"only the top-level key 'profiles' is allowed, found: {', '.join(extra)}")

    profiles = data.get("profiles") or {}
    if not isinstance(profiles, dict):
        fatal("'profiles' must be a mapping of profile name to settings")

    secrets: dict[str, dict[str, str]] = {}
    for profile_name, profile_config in profiles.items():
        if profile_name not in config["profiles"]:
            fatal(f"profile '{profile_name}' is not defined in the main config")
        if profile_config is None:
            continue
        if not isinstance(profile_config, dict):
            fatal(f"profile '{profile_name}' must be a mapping containing only 'env'")
        if extra := sorted(set(profile_config) - {"env"}):
            fatal(f"profile '{profile_name}' may only contain 'env', found: {', '.join(extra)}")
        env = profile_config.get("env") or {}
        if not isinstance(env, dict):
            fatal(f"profile '{profile_name}': 'env' must be a mapping of variable name to string value")
        for key, value in env.items():
            if not isinstance(key, str):
                fatal(f"profile '{profile_name}': env variable names must be strings")
            if not isinstance(value, str):
                fatal(f"profile '{profile_name}': env value for '{key}' must be a string (quote it in YAML)")
        secrets[profile_name] = dict(env)
    return secrets


def merge_secret_env(profile_name: str, env_vars: dict[str, str], secret_env: dict[str, str]) -> dict[str, str]:
    """Merge secret env vars into a profile's env, rejecting keys defined in both files.

    A key in both places has no obvious winner, so the install stops instead of guessing.
    The value is never printed — only the profile and the variable name.
    """
    if collisions := sorted(set(env_vars) & set(secret_env)):
        for key in collisions:
            print(
                f"Error: Profile '{profile_name}' defines env var '{key}' in both the config "
                f"and the secrets file — keep it in exactly one of them",
                file=sys.stderr,
            )
        sys.exit(1)
    return {**env_vars, **secret_env}


def expand_path(path_str: str) -> Path:
    return Path(os.path.expanduser(path_str))


def pinned_models_to_env(profile_name: str, pinned_models: dict) -> dict[str, str]:
    """Translate a profile's pinned_models mapping into ANTHROPIC_DEFAULT_<CLASS>_MODEL env vars.

    Each entry pins what a model-class alias (sonnet/opus/haiku/fable) resolves to. An unknown
    class key or a non-string / empty version aborts the whole install (same fatal style as load_config).
    """
    env: dict[str, str] = {}
    for model_class, version in pinned_models.items():
        if model_class not in VALID_MODEL_CLASSES:
            print(
                f"Error: Profile '{profile_name}' has unknown pinned_models class '{model_class}' "
                f"(valid classes: {', '.join(VALID_MODEL_CLASSES)})",
                file=sys.stderr,
            )
            sys.exit(1)
        if not isinstance(version, str) or not version.strip():
            print(
                f"Error: Profile '{profile_name}' has invalid pinned_models value for '{model_class}': "
                f"expected a non-empty model version id string",
                file=sys.stderr,
            )
            sys.exit(1)
        env[f"ANTHROPIC_DEFAULT_{model_class.upper()}_MODEL"] = version
    return env


def read_config(config_arg: Path | None) -> tuple[Path, dict]:
    config_path = resolve_config_path(config_arg, DEFAULT_CONFIG_PATH, LEGACY_CONFIG_PATH)
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        print("Create it from config.example.yaml in the repository:", file=sys.stderr)
        print("  cp config.example.yaml ~/.claude/auth-switch.yaml", file=sys.stderr)
        sys.exit(1)
    return config_path, load_config(config_path)
