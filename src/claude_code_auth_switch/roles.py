"""
The top-level `roles:` section of auth-switch.yaml: named sets of launch options that `cc-auth-switch start`
applies on top of any profile, chosen with `--agent-role <name>` or a profile's `default_role`.

Every key maps to one claude flag, except `env`, `description` and `extra_args`. Validation is strict: a typo
in a role would otherwise launch a session without the option the role exists for.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from claude_code_auth_switch.claude_options import BOOLEAN, CANONICAL, CLAUDE_VERSION, KIND
from claude_code_auth_switch.config import ENV_NAME, ConfigError

# `--agent-role none` starts without a role, also when the profile has a default_role.
NO_ROLE = "none"
PROMPT_MODES = ("append", "replace", "none")

STRING_KEYS = ("description", "model", "effort", "permission_mode")
LIST_KEYS = ("tools", "allowed_tools", "disallowed_tools", "add_dirs", "plugin_dirs", "mcp_config", "extra_args")
KNOWN_KEYS = frozenset((*STRING_KEYS, *LIST_KEYS, "prompt", "autocompact", "settings", "strict_mcp_config", "agents", "env"))


@dataclass(frozen=True)
class RolePrompt:
    # Absolute path of the prompt file.
    file: str
    # append: --append-system-prompt-file, replace: --system-prompt-file, none: no flag, a hook injects it.
    mode: str


@dataclass(frozen=True)
class Role:
    name: str
    description: str | None = None
    prompt: RolePrompt | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    autocompact: str | None = None
    settings: dict = field(default_factory=dict)
    tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    add_dirs: tuple[str, ...] = ()
    plugin_dirs: tuple[str, ...] = ()
    mcp_config: tuple[str, ...] = ()
    strict_mcp_config: bool = False
    # Inline JSON or the path of a JSON file, as --agents takes it.
    agents: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    extra_args: tuple[str, ...] = ()


def expand_path(value: str) -> str:
    return os.path.expanduser(value)


def resolve_role(name: str, role_config: object) -> Role:
    def error(message: str) -> ConfigError:
        return ConfigError(f"role '{name}': {message}")

    if role_config is None:
        role_config = {}
    if not isinstance(role_config, dict):
        raise error("must be a mapping of role keys")
    if unknown := sorted(str(key) for key in role_config if key not in KNOWN_KEYS):
        raise error(f"unknown key {', '.join(repr(key) for key in unknown)} (known keys: {', '.join(sorted(KNOWN_KEYS))})")

    values: dict = {}
    for key in STRING_KEYS:
        value = role_config.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise error(f"'{key}' must be a non-empty string")
        if value is not None and "\0" in value:
            raise error(f"'{key}' contains a NUL character")
        values[key] = value

    for key in LIST_KEYS:
        value = role_config.get(key)
        if value is None:
            value = []
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise error(f"'{key}' must be a list of non-empty strings")
        if any("\0" in item for item in value):
            raise error(f"an entry of '{key}' contains a NUL character")
        if key in ("add_dirs", "plugin_dirs"):
            value = [expand_path(item) for item in value]
        elif key == "mcp_config":
            value = [item if item.lstrip().startswith("{") else expand_path(item) for item in value]
        values[key] = tuple(value)

    autocompact = role_config.get("autocompact")
    if autocompact is not None:
        if isinstance(autocompact, bool) or not isinstance(autocompact, (int, str)) or not str(autocompact).strip():
            raise error("'autocompact' must be a number of tokens or 'auto'")
        autocompact = str(autocompact)
        if "\0" in autocompact:
            raise error("'autocompact' contains a NUL character")
    values["autocompact"] = autocompact

    settings = role_config.get("settings")
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise error("'settings' must be a mapping, merged into the --settings JSON")
    values["settings"] = settings

    strict = role_config.get("strict_mcp_config", False)
    if not isinstance(strict, bool):
        raise error("'strict_mcp_config' must be true or false")
    values["strict_mcp_config"] = strict

    agents = role_config.get("agents")
    if isinstance(agents, dict):
        agents = json.dumps(agents, ensure_ascii=False)
    elif isinstance(agents, str) and agents.strip():
        agents = agents if agents.lstrip().startswith("{") else expand_path(agents)
    elif agents is not None:
        raise error("'agents' must be a mapping of agents, a JSON string or the path of a JSON file")
    if agents is not None and "\0" in agents:
        raise error("'agents' contains a NUL character")
    values["agents"] = agents

    env = role_config.get("env")
    if env is None:
        env = {}
    if not isinstance(env, dict):
        raise error("'env' must be a mapping of variable name to value")
    for key, value in env.items():
        if not isinstance(key, str) or not ENV_NAME.fullmatch(key):
            raise error(f"env variable name {key!r} is not a valid shell variable name (letters, digits and _, not starting with a digit)")
        if key == "CLAUDE_CONFIG_DIR" or key.startswith("FP_CC_AUTH_SWITCH_"):
            raise error(f"env variable '{key}' is reserved: the profile selects the config dir, and start sets the FP_CC_AUTH_SWITCH_* variables")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise error(f"env variable '{key}' must have a string or number value")
        if "\0" in str(value):
            raise error(f"env value of '{key}' contains a NUL character")
    values["env"] = {key: str(value) for key, value in env.items()}

    for item in values["extra_args"]:
        if reason := extra_arg_problem(item):
            raise error(f"'extra_args' item {item!r} {reason}")

    values["prompt"] = resolve_prompt(role_config.get("prompt"), error)
    return Role(name=name, **values)


def extra_arg_problem(item: str) -> str | None:
    """Tell why an extra_args item could take the user's arguments as its value, or None when it cannot.

    start puts the user's arguments right after extra_args, so a value in its own item, or an option that
    takes a value written without "=", would make claude read the user's first word as a value.
    """
    name, has_value, _ = item.partition("=")
    if item.startswith("--") and has_value and len(name) > 2:
        return None
    if KIND.get(item) == BOOLEAN:
        return None
    if not item.startswith("-"):
        return "is not an option; write a value into its option as --flag=value"
    option = item if item in KIND else item[:2] if not item.startswith("--") and KIND.get(item[:2], BOOLEAN) != BOOLEAN else None
    if option is not None:
        return f"takes a value; write it as {CANONICAL[option]}=<value>"
    return f"is not an option without a value in Claude Code {CLAUDE_VERSION}; write an option that takes a value as --flag=value"


def resolve_prompt(prompt: object, error) -> RolePrompt | None:
    if prompt is None:
        return None
    if not isinstance(prompt, dict):
        raise error("'prompt' must be a mapping with 'file' and 'mode'")
    if unknown := sorted(str(key) for key in prompt if key not in ("file", "mode")):
        raise error(f"'prompt' has unknown key {', '.join(repr(key) for key in unknown)} (known keys: file, mode)")
    file = prompt.get("file")
    if not isinstance(file, str) or not file.strip():
        raise error("'prompt.file' must be the absolute path of the prompt file")
    if "\0" in file:
        raise error("'prompt.file' contains a NUL character")
    file = expand_path(file)
    if not os.path.isabs(file):
        raise error(f"'prompt.file' must be an absolute path, got {file!r}")
    mode = prompt.get("mode", "append")
    if mode not in PROMPT_MODES:
        raise error(f"'prompt.mode' must be one of {', '.join(PROMPT_MODES)}, got {mode!r}")
    return RolePrompt(file=file, mode=mode)


def resolve_roles(config: dict) -> dict[str, Role]:
    """Validate the `roles:` section and return the roles by name."""
    roles = config.get("roles")
    if roles is None:
        return {}
    if not isinstance(roles, dict):
        raise ConfigError("'roles' must be a mapping of role name to role keys")
    resolved: dict[str, Role] = {}
    for name, role_config in roles.items():
        if not isinstance(name, str) or not name or "\0" in name:
            raise ConfigError(f"role name {name!r} must be a non-empty string without NUL characters (quote it in YAML if it is a number)")
        if name == NO_ROLE:
            raise ConfigError(f"role name '{NO_ROLE}' is reserved: `--agent-role {NO_ROLE}` starts without a role")
        resolved[name] = resolve_role(name, role_config)
    return resolved
