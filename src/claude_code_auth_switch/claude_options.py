"""
The options of the `claude` command and a scanner that reads an argument list the way Claude Code does.

start must know which arguments are options and which are their values: in `--name -v`, `-v` is the
session name, not a request for the version. The table comes from `claude --help` of Claude Code 2.1.325,
plus the two hidden system prompt file options that start passes itself. The scanner follows the rules
of the commander parser bundled in that binary:

- An argument is option-like when it is longer than one character and starts with "-".
- An option with a required value (`<x>`) takes the next argument, even an option-like one.
- An option with an optional value (`[x]`) takes the next argument only when it is not option-like.
- A variadic option (`<x...>`) takes the next argument, then every following one up to the next option-like one.
- `--option=value` takes nothing more, for an option that takes a value.
- `-Xvalue` gives a short option that takes a value the rest of the argument; for a boolean short
  option, the rest is read as further short options.
- `--` ends the options; everything after it is a prompt word.

An option that is not in the table, a hidden one included, is read as taking no value.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Sequence

CLAUDE_VERSION = "2.1.325"

BOOLEAN = "boolean"
REQUIRED = "required"
OPTIONAL = "optional"
VARIADIC = "variadic"

# Every name of an option, aliases included, and the kind of its value. The first name of each group
# is the canonical one that start compares against.
_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (BOOLEAN, ("--allow-dangerously-skip-permissions",)),
    (BOOLEAN, ("--ax-screen-reader",)),
    (BOOLEAN, ("--background", "--bg")),
    (BOOLEAN, ("--bare",)),
    (BOOLEAN, ("--brief",)),
    (BOOLEAN, ("--chrome",)),
    (BOOLEAN, ("--continue", "-c")),
    (BOOLEAN, ("--dangerously-skip-permissions",)),
    (BOOLEAN, ("--desktop",)),
    (BOOLEAN, ("--disable-slash-commands",)),
    (BOOLEAN, ("--exclude-dynamic-system-prompt-sections",)),
    (BOOLEAN, ("--fork-session",)),
    (BOOLEAN, ("--forward-subagent-text",)),
    (BOOLEAN, ("--help", "-h")),
    (BOOLEAN, ("--ide",)),
    (BOOLEAN, ("--include-hook-events",)),
    (BOOLEAN, ("--include-partial-messages",)),
    (BOOLEAN, ("--no-chrome",)),
    (BOOLEAN, ("--no-session-persistence",)),
    (BOOLEAN, ("--print", "-p")),
    (BOOLEAN, ("--replay-user-messages",)),
    (BOOLEAN, ("--restricted",)),
    (BOOLEAN, ("--safe-mode",)),
    (BOOLEAN, ("--strict-mcp-config",)),
    (BOOLEAN, ("--tmux",)),
    (BOOLEAN, ("--verbose",)),
    (BOOLEAN, ("--version", "-v")),
    (REQUIRED, ("--agent",)),
    (REQUIRED, ("--agents",)),
    (REQUIRED, ("--append-system-prompt",)),
    (REQUIRED, ("--append-system-prompt-file",)),  # hidden from --help
    (REQUIRED, ("--autocompact",)),
    (REQUIRED, ("--debug-file",)),
    (REQUIRED, ("--effort",)),
    (REQUIRED, ("--environment",)),
    (REQUIRED, ("--fallback-model",)),
    (REQUIRED, ("--input-format",)),
    (REQUIRED, ("--json-schema",)),
    (REQUIRED, ("--max-budget-usd",)),
    (REQUIRED, ("--model",)),
    (REQUIRED, ("--name", "-n")),
    (REQUIRED, ("--output-format",)),
    (REQUIRED, ("--permission-mode",)),
    (REQUIRED, ("--permission-prompts",)),
    (REQUIRED, ("--plugin-dir",)),
    (REQUIRED, ("--plugin-url",)),
    (REQUIRED, ("--remote-control-session-name-prefix",)),
    (REQUIRED, ("--session-id",)),
    (REQUIRED, ("--setting-sources",)),
    (REQUIRED, ("--settings",)),
    (REQUIRED, ("--system-prompt",)),
    (REQUIRED, ("--system-prompt-file",)),  # hidden from --help
    (REQUIRED, ("--system-prompt-snapshot",)),
    (OPTIONAL, ("--cloud",)),
    (OPTIONAL, ("--debug", "-d")),
    (OPTIONAL, ("--from-pr",)),
    (OPTIONAL, ("--prompt-suggestions",)),
    (OPTIONAL, ("--remote-control",)),
    (OPTIONAL, ("--resume", "-r")),
    (OPTIONAL, ("--teleport",)),
    (OPTIONAL, ("--worktree", "-w")),
    (VARIADIC, ("--add-dir",)),
    (VARIADIC, ("--allowed-tools", "--allowedTools")),
    (VARIADIC, ("--betas",)),
    (VARIADIC, ("--disallowed-tools", "--disallowedTools")),
    (VARIADIC, ("--file",)),
    (VARIADIC, ("--mcp-config",)),
    (VARIADIC, ("--tools",)),
)

KIND: dict[str, str] = {name: kind for kind, names in _GROUPS for name in names}
CANONICAL: dict[str, str] = {name: names[0] for _, names in _GROUPS for name in names}


@dataclass(frozen=True)
class Option:
    # The canonical name of a known option; for an unknown one, the argument up to any "=".
    name: str
    values: tuple[str, ...]
    # The arguments args[start:end] hold the option and its values. Short options combined in one
    # argument share it.
    start: int
    end: int
    known: bool


def option_like(arg: str) -> bool:
    return len(arg) > 1 and arg[0] == "-"


def scan(args: Sequence[str]) -> list[Option]:
    """Return the options of args in order, up to a `--`, with the values each one takes."""
    options: list[Option] = []
    queue: deque[tuple[str, int]] = deque((arg, index) for index, arg in enumerate(args))
    variadic: Option | None = None
    while queue:
        arg, index = queue.popleft()
        if arg == "--":
            break
        if variadic is not None and not option_like(arg):
            options[-1] = variadic = Option(variadic.name, (*variadic.values, arg), variadic.start, index + 1, True)
            continue
        variadic = None
        if not option_like(arg):
            continue

        kind = KIND.get(arg)
        if kind is not None:
            values: tuple[str, ...] = ()
            end = index + 1
            if kind in (REQUIRED, VARIADIC) and queue:
                value, value_index = queue.popleft()
                values, end = (value,), value_index + 1
            elif kind == OPTIONAL and queue and not option_like(queue[0][0]):
                value, value_index = queue.popleft()
                values, end = (value,), value_index + 1
            options.append(Option(CANONICAL[arg], values, index, end, True))
            if kind == VARIADIC and values:
                variadic = options[-1]
            continue

        if len(arg) > 2 and arg[1] != "-" and arg[:2] in KIND:
            short = arg[:2]
            if KIND[short] == BOOLEAN:
                options.append(Option(CANONICAL[short], (), index, index + 1, True))
                queue.appendleft((f"-{arg[2:]}", index))
            else:
                options.append(Option(CANONICAL[short], (arg[2:],), index, index + 1, True))
            continue

        name, has_value, value = arg.partition("=")
        if arg.startswith("--") and has_value and KIND.get(name, BOOLEAN) != BOOLEAN:
            options.append(Option(CANONICAL[name], (value,), index, index + 1, True))
            continue

        # commander rejects --flag=value for an option without a value as unknown, so it names no known
        # option; an unknown --option=value is named by its part before "=".
        options.append(Option(name if arg.startswith("--") and name not in KIND else arg, (), index, index + 1, False))
    return options


def canonical(*names: str) -> set[str]:
    return {CANONICAL.get(name, name) for name in names}


def has_option(args: Sequence[str], *names: str) -> bool:
    """Tell whether args pass one of the options, by any of its names, before a `--`."""
    wanted = canonical(*names)
    return any(option.name in wanted for option in scan(args))


def remove_option(args: Sequence[str], name: str) -> tuple[list[tuple[str, ...]], list[str]]:
    """Remove every occurrence of an option with its values. Returns (the values of each occurrence, the rest)."""
    wanted = canonical(name)
    found = [option for option in scan(args) if option.name in wanted]
    removed = {index for option in found for index in range(option.start, option.end)}
    return [option.values for option in found], [arg for index, arg in enumerate(args) if index not in removed]
