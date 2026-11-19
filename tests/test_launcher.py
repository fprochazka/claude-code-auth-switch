import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from claude_code_auth_switch import launcher
from claude_code_auth_switch.config import ConfigError
from claude_code_auth_switch.installer import MARKER, cleanup_stale_scripts, generate_launcher_script
from claude_code_auth_switch.launcher import (
    LaunchContext,
    LaunchError,
    Profile,
    build_launch,
    merge_settings,
    parse_args,
)

from legacy_wrapper import generate_wrapper_script

PROFILE = Profile(
    name="work",
    config_dir=Path("/profiles/work"),
    env={"A": '$HOME `x` "q"'},
    add_dirs=(Path("/repo dir"),),
    model="sonnet",
)


def context(environ=None, attribution=None, tmux="sess") -> tuple[LaunchContext, list[tuple[Any, ...]]]:
    calls: list[tuple[Any, ...]] = []

    def tmux_session(pane):
        calls.append(("tmux", pane))
        return tmux

    def git_attribution(cwd):
        calls.append(("git", cwd))
        return attribution

    return LaunchContext(
        cwd="/work", environ=environ or {}, tmux_session=tmux_session, git_attribution=git_attribution
    ), calls


def test_build_launch_orders_arguments_like_the_bash_wrapper():
    ctx, _ = context({"TMUX": "/t", "TMUX_PANE": "%7"}, {"attribution": {"pr": ""}})

    launch = build_launch(PROFILE, ["-p", "hi"], ctx)

    assert launch.argv == [
        "--add-dir=/repo dir",
        "--settings",
        '{"attribution": {"pr": ""}}',
        "--name",
        "sess-p7",
        "--model",
        "sonnet",
        "-p",
        "hi",
    ]


def test_build_launch_clears_inherited_claude_and_anthropic_variables_and_keeps_values_literal():
    ctx, _ = context(
        {
            "CLAUDE_X": "1",
            "ANTHROPIC_API_KEY": "k",
            "CLAUDE_CONFIG_DIR": "/wrong",
            "PATH": "/bin",
            "FP_CC_AUTH_SWITCH_PROFILE": "other",
        }
    )

    env = build_launch(PROFILE, [], ctx).env

    assert env == {
        "PATH": "/bin",
        "CLAUDE_CONFIG_DIR": "/profiles/work",
        "FP_CC_AUTH_SWITCH_PROFILE": "work",
        "A": '$HOME `x` "q"',
    }


def test_profile_env_wins_over_the_variables_the_launcher_sets():
    profile = Profile(name="work", config_dir=Path("/p"), env={"CLAUDE_CONFIG_DIR": "/custom"})

    assert build_launch(profile, [], context()[0]).env["CLAUDE_CONFIG_DIR"] == "/custom"


def test_resume_skips_tmux():
    ctx, calls = context({"TMUX": "/t", "TMUX_PANE": "%7"})

    launch = build_launch(PROFILE, ["--resume=abc"], ctx)

    assert calls == [("git", "/work")]
    assert launch.argv == ["--add-dir=/repo dir", "--model", "sonnet", "--resume=abc"]


def test_the_last_user_settings_is_merged_over_the_attribution_into_one_settings():
    ctx, _ = context(attribution={"attribution": {"commit": "git", "pr": "git"}, "permissions": {"allow": ["Read"]}})

    launch = build_launch(
        PROFILE,
        [
            "--settings",
            '{"ignored": 1}',
            "-p",
            "hi",
            '--settings={"attribution": {"pr": "user"}, "permissions": {"allow": ["Read", "Bash"]}}',
        ],
        ctx,
    )

    assert launch.argv == [
        "--add-dir=/repo dir",
        "--settings",
        '{"attribution": {"commit": "git", "pr": "user"}, "permissions": {"allow": ["Read", "Bash"]}}',
        "--model",
        "sonnet",
        "-p",
        "hi",
    ]


def test_user_settings_file_is_read_relative_to_the_start_directory(tmp_path):
    (tmp_path / "own.json").write_text('{"model": "x"}')
    ctx, _ = context(attribution={"attribution": {"pr": ""}})
    ctx = dataclasses.replace(ctx, cwd=str(tmp_path))

    assert build_launch(PROFILE, ["--settings", "own.json"], ctx).argv[1:3] == [
        "--settings",
        '{"attribution": {"pr": ""}, "model": "x"}',
    ]


@pytest.mark.parametrize(
    "value, error",
    [("missing.json", "cannot read"), ("{not json", "not valid JSON"), ("list.json", "must hold a JSON object")],
)
def test_unreadable_user_settings_stops_the_launch(tmp_path, value, error):
    (tmp_path / "list.json").write_text("[1]")
    with pytest.raises(LaunchError, match=error):
        launcher.read_settings_value(value, str(tmp_path))


RELATIVE_SETTINGS = '{"statusLine": {"type": "command", "command": "./status.sh"}}'


def test_a_relative_settings_path_and_relative_paths_inside_it_stay_as_written(tmp_path):
    (tmp_path / "own.json").write_text(RELATIVE_SETTINGS)
    ctx = dataclasses.replace(context()[0], cwd=str(tmp_path))
    merged_ctx = dataclasses.replace(context(attribution={"attribution": {"pr": ""}})[0], cwd=str(tmp_path))

    # Nothing to merge: claude gets the relative path, as from plain claude.
    assert build_launch(PROFILE, ["--settings", "own.json"], ctx).argv[-2:] == ["--settings", "own.json"]
    # Something to merge: claude gets the content inline, the path inside it unchanged.
    merged = build_launch(PROFILE, ["--settings", "own.json"], merged_ctx).argv
    assert json.loads(merged[merged.index("--settings") + 1]) == {
        "attribution": {"pr": ""},
        **json.loads(RELATIVE_SETTINGS),
    }
    assert "own.json" not in merged


def test_user_settings_pass_unchanged_when_start_has_no_settings_of_its_own():
    ctx, _ = context()

    assert build_launch(PROFILE, ["--settings=/s.json", "--", "--settings", "x"], ctx).argv == [
        "--add-dir=/repo dir",
        "--model",
        "sonnet",
        "--settings=/s.json",
        "--",
        "--settings",
        "x",
    ]


def test_an_option_value_is_never_read_as_the_option_it_looks_like():
    # -p is --print, a flag without a value, so in `-p --model x` the --model is the user's own.
    assert build_launch(PROFILE, ["-p", "--model", "x"], context()[0]).argv == [
        "--add-dir=/repo dir",
        "-p",
        "--model",
        "x",
    ]
    assert build_launch(PROFILE, ["--name", "--model"], context()[0]).argv == [
        "--add-dir=/repo dir",
        "--model",
        "sonnet",
        "--name",
        "--model",
    ]


def test_a_settings_flag_without_a_value_stays_for_claude_to_report():
    ctx, _ = context(attribution={"attribution": {"pr": ""}})

    assert build_launch(PROFILE, ["-p", "--settings"], ctx).argv[-2:] == ["-p", "--settings"]


def test_subcommand_gets_only_the_environment():
    ctx, calls = context({"TMUX": "/t", "TMUX_PANE": "%7"}, {"attribution": {"pr": ""}})

    launch = build_launch(PROFILE, ["mcp", "list"], ctx)

    assert (launch.argv, calls, launch.env["CLAUDE_CONFIG_DIR"]) == (["mcp", "list"], [], "/profiles/work")


def test_merge_settings_merges_nested_objects_concatenates_lists_and_later_layers_win():
    assert merge_settings(
        {"attribution": {"commit": "a", "pr": "b"}, "x": 1, "l": [1, 2]},
        None,
        {"attribution": {"pr": "c"}, "x": {"y": 2}, "l": [2, 3]},
    ) == {
        "attribution": {"commit": "a", "pr": "c"},
        "x": {"y": 2},
        "l": [1, 2, 3],
    }


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["work"], (None, "work", [])),
        (["work", "--config", "x", "-p"], (None, "work", ["--config", "x", "-p"])),
        (["--config", "/c.yaml", "work", "--model", "opus"], (Path("/c.yaml"), "work", ["--model", "opus"])),
        (["--config=/c.yaml", "work"], (Path("/c.yaml"), "work", [])),
    ],
)
def test_parse_args_passes_everything_after_the_profile_to_claude(argv, expected):
    assert parse_args(argv) == expected


@pytest.mark.parametrize("argv", [[], ["--config", "/c.yaml"], ["-p"]])
def test_parse_args_requires_a_profile(argv):
    with pytest.raises(ValueError, match="usage"):
        parse_args(argv)


def test_broken_git_config_costs_only_the_attribution(monkeypatch, capsys):
    def boom(directory):
        raise RuntimeError("bad include")

    monkeypatch.setattr("claude_code_auth_switch.git_attribution.settings_for", boom)

    assert launcher.git_attribution_of("/work") is None
    assert "cannot read attribution from git config: bad include" in capsys.readouterr().err


def test_logical_cwd_keeps_a_symlinked_pwd(tmp_path, monkeypatch):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    monkeypatch.chdir(tmp_path / "link")

    assert launcher.logical_cwd({"PWD": str(tmp_path / "link")}) == str(tmp_path / "link")
    assert launcher.logical_cwd({"PWD": str(tmp_path)}) == str(tmp_path / "real")


def test_stale_cleanup_removes_legacy_wrappers_and_launchers_of_removed_profiles(tmp_path):
    legacy = generate_wrapper_script("old", tmp_path / "profiles/old", "", {}, [], None, Path("/bin/cc-auth-switch"))
    (tmp_path / "claude-old").write_text(legacy)
    (tmp_path / "claude-gone").write_text(
        generate_launcher_script("gone", "", Path("/bin/cc-auth-switch"), Path("/c.yaml"))
    )
    (tmp_path / "claude-kept").write_text(
        generate_launcher_script("kept", "", Path("/bin/cc-auth-switch"), Path("/c.yaml"))
    )
    (tmp_path / "claude-foreign").write_text("#!/bin/sh\nexec claude\n")

    cleanup_stale_scripts(tmp_path, {"claude-kept"})

    assert sorted(p.name for p in tmp_path.iterdir()) == ["claude-foreign", "claude-kept"]
    assert MARKER in legacy


def test_launcher_quotes_paths_and_names():
    script = generate_launcher_script("work", "d", Path("/my tools/cc-auth-switch"), Path("/cfg dir/a.yaml"))

    assert script.splitlines()[-1] == "exec '/my tools/cc-auth-switch' start --config '/cfg dir/a.yaml' work \"$@\""


def test_start_does_not_load_the_installer(sandbox):
    # start runs on every launch, so it pays for yaml but not for the installer and the sessions migration.
    config = sandbox.write(
        "auth-switch.yaml",
        f"profiles_dir: {sandbox.home / 'profiles'}\ncapture_rate_limits: false\nprofiles:\n  work: {{}}\n",
    )
    script = (
        "import os, sys\n"
        "from claude_code_auth_switch.cli import main\n"
        "def fake_exec(file, args, env):\n"
        "    print(sorted(m for m in sys.modules if m.endswith(('.installer', '.sessions'))), args[1:])\n"
        "    sys.exit(0)\n"
        "os.execvpe = fake_exec\n"
        "main(['start', '--config', sys.argv[1], 'work', '-p', 'hi'])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(config)],
        env=sandbox.env,
        capture_output=True,
        text=True,
        cwd=sandbox.mkdir("project"),
    )

    assert result.stdout.strip() == "[] ['-p', 'hi']", result.stderr


@pytest.fixture
def hanging(tmp_path, monkeypatch):
    """Put a git and a tmux that never answer first on PATH."""
    fake_bin = tmp_path / "hanging-bin"
    fake_bin.mkdir()
    for name in ("git", "tmux"):
        (fake_bin / name).write_text("#!/bin/sh\nexec sleep 30\n")
        (fake_bin / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    return tmp_path


def test_secrets_check_stops_when_git_does_not_answer(hanging, monkeypatch, capsys):
    from claude_code_auth_switch import config

    monkeypatch.setattr(config, "GIT_TIMEOUT_SECONDS", 0.3)
    secrets = hanging / "auth-switch.secrets.yaml"
    secrets.write_text("profiles: {}\n")

    with pytest.raises(SystemExit) as exit_info:
        config.check_secrets_git_safety(secrets)

    assert exit_info.value.code == 1
    assert (
        f"git did not answer within 0.3 seconds while checking whether secrets file {secrets}"
        in capsys.readouterr().err
    )


def test_attribution_is_dropped_when_git_does_not_answer(hanging, monkeypatch, capsys):
    from claude_code_auth_switch import git_attribution

    monkeypatch.setattr(git_attribution, "GIT_TIMEOUT_SECONDS", 0.3)

    assert launcher.git_attribution_of(str(hanging)) is None
    assert "cannot read attribution from git config: git did not answer within 0.3 seconds" in capsys.readouterr().err


def test_session_name_is_dropped_when_tmux_does_not_answer(hanging, monkeypatch):
    monkeypatch.setattr(launcher, "TMUX_TIMEOUT_SECONDS", 0.3)

    assert launcher.tmux_session_of("%1") is None


@pytest.fixture
def work_config(tmp_path):
    config = tmp_path / "auth-switch.yaml"
    config.write_text(f"profiles_dir: {tmp_path / 'profiles'}\nprofiles:\n  work: {{}}\n")
    return config


def test_unexpected_error_stops_the_launch_with_one_line_and_no_traceback(work_config, monkeypatch, capsys):
    def boom(*args):
        raise RuntimeError("something broke")

    monkeypatch.setattr(launcher, "build_launch", boom)
    monkeypatch.delenv(launcher.DEBUG_ENV, raising=False)

    assert launcher.main(["--config", str(work_config), "work"]) == 1
    err = capsys.readouterr().err
    assert f"not starting Claude for profile 'work' with {work_config}: RuntimeError: something broke" in err
    assert "set CC_AUTH_SWITCH_DEBUG=1 to see the traceback" in err
    assert "Traceback" not in err


def test_debug_variable_lets_the_traceback_through(work_config, monkeypatch):
    def boom(*args):
        raise RuntimeError("something broke")

    monkeypatch.setattr(launcher, "build_launch", boom)
    monkeypatch.setenv(launcher.DEBUG_ENV, "1")

    with pytest.raises(RuntimeError, match="something broke"):
        launcher.main(["--config", str(work_config), "work"])


def test_launcher_keeps_a_multiline_description_in_its_comment():
    script = generate_launcher_script(
        "work", "first line\ntouch /tmp/pwned\r\nlast", Path("/bin/cc-auth-switch"), Path("/c.yaml")
    )

    assert script.splitlines() == [
        "#!/usr/bin/env bash",
        MARKER,
        "# Profile: work — first line touch /tmp/pwned last",
        'exec /bin/cc-auth-switch start --config /c.yaml work "$@"',
    ]


def home_context(tmp_path, cwd, environ=None):
    ctx, _ = context({"HOME": str(tmp_path / "home"), **(environ or {})})
    return dataclasses.replace(ctx, cwd=str(cwd))


@pytest.mark.parametrize("cwd", ["home", "home/.", "home/sub/..", "link-to-home"])
def test_a_session_in_the_home_directory_is_refused(tmp_path, cwd):
    (tmp_path / "home/sub").mkdir(parents=True)
    (tmp_path / "link-to-home").symlink_to(tmp_path / "home")

    with pytest.raises(
        LaunchError,
        match="refusing to start in the home directory .*CC_AUTH_SWITCH_ALLOW_HOME=1.*refuse_home_cwd: false",
    ):
        build_launch(PROFILE, ["-p", "hi"], home_context(tmp_path, tmp_path / cwd))


@pytest.mark.parametrize(
    "args",
    [["-p", "hi"], ["--resume", "00000000-0000-0000-0000-000000000000"], ["--continue"], ["--name", "--version"]],
)
def test_print_mode_and_resume_in_the_home_directory_are_refused_too(tmp_path, args):
    (tmp_path / "home").mkdir()

    with pytest.raises(LaunchError, match="refusing to start in the home directory"):
        build_launch(PROFILE, args, home_context(tmp_path, tmp_path / "home"))


@pytest.mark.parametrize("args", [["--version"], ["-v"], ["--help"], ["-h"], ["mcp", "list"], ["plugin", "list"]])
def test_subcommands_and_version_or_help_run_in_the_home_directory(tmp_path, args):
    (tmp_path / "home").mkdir()

    assert build_launch(PROFILE, args, home_context(tmp_path, tmp_path / "home")).argv == args


def test_a_subdirectory_of_home_starts(tmp_path):
    (tmp_path / "home/project").mkdir(parents=True)

    assert build_launch(PROFILE, ["-p", "hi"], home_context(tmp_path, tmp_path / "home/project")).argv[-2:] == [
        "-p",
        "hi",
    ]


def test_the_allow_home_variable_starts_once_and_is_not_passed_on(tmp_path):
    (tmp_path / "home").mkdir()

    launch = build_launch(
        PROFILE, ["-p", "hi"], home_context(tmp_path, tmp_path / "home", {"CC_AUTH_SWITCH_ALLOW_HOME": "1"})
    )

    assert launch.argv[-2:] == ["-p", "hi"]
    assert "CC_AUTH_SWITCH_ALLOW_HOME" not in launch.env


def test_the_allow_home_variable_needs_the_value_1(tmp_path):
    (tmp_path / "home").mkdir()

    with pytest.raises(LaunchError, match="refusing to start in the home directory"):
        build_launch(PROFILE, [], home_context(tmp_path, tmp_path / "home", {"CC_AUTH_SWITCH_ALLOW_HOME": "yes"}))


def test_a_profile_without_refuse_home_cwd_starts_in_the_home_directory(tmp_path):
    (tmp_path / "home").mkdir()
    profile = dataclasses.replace(PROFILE, refuse_home_cwd=False)

    assert build_launch(profile, ["-p", "hi"], home_context(tmp_path, tmp_path / "home")).argv[-2:] == ["-p", "hi"]


@pytest.mark.parametrize(
    ("top_level", "profile_value", "expected"),
    [(None, None, True), (False, None, False), (False, True, True), (True, False, False), (None, False, False)],
)
def test_refuse_home_cwd_defaults_to_true_and_the_profile_wins_over_the_top_level(top_level, profile_value, expected):
    config: dict[str, Any] = {"profiles": {"work": {} if profile_value is None else {"refuse_home_cwd": profile_value}}}
    if top_level is not None:
        config["refuse_home_cwd"] = top_level

    assert launcher.resolve_profile(config, {}, Path("/p"), "work").refuse_home_cwd is expected


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"refuse_home_cwd": "no", "profiles": {"work": {}}}, "'refuse_home_cwd' must be true or false, not 'no'"),
        (
            {"profiles": {"work": {"refuse_home_cwd": 0}}},
            "profile 'work': 'refuse_home_cwd' must be true or false, not 0",
        ),
    ],
)
def test_refuse_home_cwd_must_be_a_boolean(config, message):
    with pytest.raises(ConfigError, match=message):
        launcher.resolve_profile(config, {}, Path("/p"), "work")
