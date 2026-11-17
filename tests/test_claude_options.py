import pytest

from claude_code_auth_switch.claude_options import has_option, remove_option, scan


def names_and_values(args):
    return [(option.name, option.values) for option in scan(args)]


@pytest.mark.parametrize(
    "args, expected",
    [
        # A required value is taken even when it looks like an option.
        (["--name", "-v", "-p"], [("--name", ("-v",)), ("--print", ())]),
        (["--append-system-prompt", "-h"], [("--append-system-prompt", ("-h",))]),
        (["--name", "--model", "hi"], [("--name", ("--model",))]),
        # An optional value is taken only when it does not look like an option.
        (["-r", "-p", "x"], [("--resume", ()), ("--print", ())]),
        (["--resume", "abc", "--debug", "api"], [("--resume", ("abc",)), ("--debug", ("api",))]),
        # A variadic option takes words up to the next option.
        (["--add-dir", "/a", "/b", "-p", "hi"], [("--add-dir", ("/a", "/b")), ("--print", ())]),
        (["--add-dir", "-a"], [("--add-dir", ("-a",))]),
        # =, combined short options, aliases.
        (
            ["--model=opus", "--allowedTools=Bash", "--verbose=x"],
            [("--model", ("opus",)), ("--allowed-tools", ("Bash",)), ("--verbose=x", ())],
        ),
        (
            ["-pc", "-rabc", "-nfoo"],
            [("--print", ()), ("--continue", ()), ("--resume", ("abc",)), ("--name", ("foo",))],
        ),
        # Unknown options take no value; -- ends the options.
        (["--agent-role", "x", "--", "--model", "y"], [("--agent-role", ())]),
        (["-", "word"], []),
    ],
)
def test_scan_reads_arguments_like_claudes_option_parser(args, expected):
    assert names_and_values(args) == expected


def test_has_option_matches_any_alias_and_ignores_values_and_prompt_words():
    assert has_option(["-c"], "--continue") and has_option(["--allowedTools", "X"], "--allowed-tools")
    assert not has_option(["--name", "--model"], "--model")
    assert not has_option(["--", "--help"], "--help")


def test_remove_option_removes_each_occurrence_with_its_values():
    assert remove_option(
        ["--settings", "a.json", "-p", "--settings={}", "--name", "--settings", "--", "--settings", "x"], "--settings"
    ) == (
        [("a.json",), ("{}",)],
        ["-p", "--name", "--settings", "--", "--settings", "x"],
    )
