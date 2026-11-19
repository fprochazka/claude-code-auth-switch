"""How `start` puts the statusline tap in front of the user's own status line."""

import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from claude_code_auth_switch import launcher
from claude_code_auth_switch.config import ConfigError
from claude_code_auth_switch.launcher import (
    LaunchContext,
    Profile,
    StatusLineSources,
    build_launch,
    is_tap_command,
    local_settings_dir,
    read_status_line_sources,
)

PROFILE = Profile(name="work", config_dir=Path("/profiles/work"), capture_rate_limits=True)
TOOL = "/bin/cc-auth-switch"
USER_LINE = {"type": "command", "command": "bash ~/.claude/status line.sh", "padding": 2}


NO_SOURCES = StatusLineSources()


def context(sources=NO_SOURCES, attribution=None, user_settings=None, cwd="/work") -> LaunchContext:
    return LaunchContext(
        cwd=cwd,
        environ={},
        tmux_session=lambda pane: None,
        git_attribution=lambda cwd: attribution,
        read_settings=lambda value, cwd: user_settings if user_settings is not None else json.loads(value),
        status_line_sources=lambda config_dir, cwd: sources,
        tool_command=lambda: TOOL,
    )


def settings_of(argv: list[str]) -> dict[str, Any] | None:
    if "--settings" not in argv:
        return None
    settings: dict[str, Any] = json.loads(argv[argv.index("--settings") + 1])
    return settings


def test_the_tap_delegates_to_the_users_status_line_and_keeps_its_display_keys():
    sources = StatusLineSources((("user", USER_LINE),))

    launch = build_launch(PROFILE, ["-p", "hi"], context(sources, attribution={"attribution": {"pr": ""}}))

    assert launch.argv[-2:] == ["-p", "hi"]
    assert settings_of(launch.argv) == {
        "attribution": {"pr": ""},
        "statusLine": {
            "type": "command",
            "command": f"{TOOL} statusline --delegate 'bash ~/.claude/status line.sh'",
            "padding": 2,
        },
    }


def test_without_a_status_line_of_your_own_there_is_no_tap():
    # Any statusLine hides Claude Code's footer hints and needs workspace trust, so a session without one keeps none.
    assert settings_of(build_launch(PROFILE, [], context()).argv) is None
    attributed = settings_of(build_launch(PROFILE, [], context(attribution={"attribution": {"pr": ""}})).argv)
    assert attributed == {"attribution": {"pr": ""}}


def test_project_and_local_settings_win_over_the_user_settings_and_your_settings_over_all():
    sources = StatusLineSources(
        (
            ("user", {**USER_LINE, "refreshInterval": 5}),
            ("project", {"type": "command", "command": "project-line"}),
            ("local", {"type": "command", "command": "local-line", "hideVimModeIndicator": True}),
        )
    )

    from_files = settings_of(build_launch(PROFILE, [], context(sources)).argv)
    yours = settings_of(
        build_launch(
            PROFILE, ["--settings", '{"statusLine": {"type": "command", "command": "mine"}, "x": 1}'], context(sources)
        ).argv
    )

    assert from_files is not None and yours is not None
    assert from_files["statusLine"] == {
        "type": "command",
        "command": f"{TOOL} statusline --delegate local-line",
        "padding": 2,
        "refreshInterval": 5,
        "hideVimModeIndicator": True,
    }
    assert yours["statusLine"]["command"] == f"{TOOL} statusline --delegate mine"
    assert yours["x"] == 1


def test_setting_sources_leaves_out_the_files_claude_code_will_not_read():
    sources = StatusLineSources((("user", USER_LINE), ("project", {"type": "command", "command": "project-line"})))

    launch = build_launch(PROFILE, ["--setting-sources", "user,local"], context(sources))

    settings = settings_of(launch.argv)
    assert settings is not None
    assert settings["statusLine"]["command"] == f"{TOOL} statusline --delegate 'bash ~/.claude/status line.sh'"


def test_a_status_line_that_is_already_the_tap_is_not_delegated_to():
    sources = StatusLineSources((("user", {"type": "command", "command": "'/old bin/cc-auth-switch' statusline"}),))

    assert settings_of(build_launch(PROFILE, [], context(sources)).argv) is None
    assert is_tap_command("python -m claude_code_auth_switch statusline --delegate x")
    assert not is_tap_command("my-statusline")


def test_no_tap_when_managed_settings_decide_the_status_line_or_the_profile_opts_out():
    managed = StatusLineSources((("user", USER_LINE),), managed=True)

    assert settings_of(build_launch(PROFILE, [], context(managed)).argv) is None
    off = dataclasses.replace(PROFILE, capture_rate_limits=False)
    assert settings_of(build_launch(off, [], context(StatusLineSources((("user", USER_LINE),)))).argv) is None


def test_your_settings_file_that_cannot_be_read_skips_the_tap_and_passes_through(tmp_path):
    sources = StatusLineSources((("user", USER_LINE),))
    ctx = dataclasses.replace(context(sources), read_settings=launcher.read_settings_value, cwd=str(tmp_path))

    launch = build_launch(PROFILE, ["--settings", "missing.json", "-p", "hi"], ctx)

    assert launch.argv == ["--settings", "missing.json", "-p", "hi"]
    with pytest.raises(launcher.LaunchError, match="cannot read"):
        build_launch(
            PROFILE, ["--settings", "missing.json"], dataclasses.replace(ctx, git_attribution=lambda cwd: {"x": 1})
        )


def test_subcommands_and_version_get_no_tap():
    for args in (["mcp", "list"], ["--version"]):
        assert build_launch(PROFILE, args, context()).argv == args


@pytest.mark.parametrize(
    ("top_level", "profile_value", "expected"),
    [(None, None, True), (False, None, False), (False, True, True), (True, False, False)],
)
def test_capture_rate_limits_defaults_to_true_and_the_profile_wins(top_level, profile_value, expected):
    config: dict[str, Any] = {
        "profiles": {"work": {} if profile_value is None else {"capture_rate_limits": profile_value}}
    }
    if top_level is not None:
        config["capture_rate_limits"] = top_level

    assert launcher.resolve_profile(config, {}, Path("/p"), "work").capture_rate_limits is expected


def test_capture_rate_limits_must_be_a_boolean():
    with pytest.raises(ConfigError, match="profile 'work': 'capture_rate_limits' must be true or false, not 'yes'"):
        launcher.resolve_profile({"profiles": {"work": {"capture_rate_limits": "yes"}}}, {}, Path("/p"), "work")


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_local_settings_come_from_the_main_checkout_of_a_repository(tmp_path):
    home = tmp_path / "home"
    repo = home / "repo"
    (repo / "sub").mkdir(parents=True)
    git("init", "-q", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    git("worktree", "add", "-q", str(repo / ".worktrees" / "feature"), cwd=repo)
    worktree = repo / ".worktrees" / "feature"

    assert local_settings_dir(repo, home) == repo
    assert local_settings_dir(repo / "sub", home) == repo
    assert local_settings_dir(worktree, home) == repo
    assert local_settings_dir(tmp_path, home) == tmp_path
    assert local_settings_dir(home, home) == home


def test_the_settings_files_are_read_in_claude_codes_order(tmp_path, monkeypatch):
    home, config_dir, project = tmp_path / "home", tmp_path / "profile", tmp_path / "home" / "project"
    (project / ".claude").mkdir(parents=True)
    config_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (config_dir / "settings.json").write_text(json.dumps({"statusLine": USER_LINE}))
    (project / ".claude" / "settings.json").write_text("{broken")
    (project / ".claude" / "settings.local.json").write_text(json.dumps({"statusLine": {"command": "local"}}))
    managed = tmp_path / "managed"
    (managed / "managed-settings.d").mkdir(parents=True)
    monkeypatch.setitem(launcher.MANAGED_SETTINGS_DIRS, "linux", managed)

    sources = read_status_line_sources(config_dir, str(project), platform="linux")

    assert sources == StatusLineSources((("user", USER_LINE), ("local", {"command": "local"})), managed=False)
    (managed / "managed-settings.d" / "10-org.json").write_text(json.dumps({"allowManagedHooksOnly": True}))
    assert read_status_line_sources(config_dir, str(project), platform="linux").managed is True
