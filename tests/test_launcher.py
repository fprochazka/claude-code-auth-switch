import subprocess
import sys
from pathlib import Path

import pytest

from claude_code_auth_switch import launcher
from claude_code_auth_switch.installer import MARKER, cleanup_stale_scripts, generate_launcher_script
from claude_code_auth_switch.launcher import LaunchContext, Profile, build_launch, merge_settings, parse_args
from legacy_wrapper import generate_wrapper_script

PROFILE = Profile(name="work", config_dir=Path("/profiles/work"), env={"A": "$HOME `x` \"q\""}, add_dirs=(Path("/repo dir"),), model="sonnet")


def context(environ=None, attribution=None, tmux="sess") -> tuple[LaunchContext, list]:
    calls: list = []

    def tmux_session(pane):
        calls.append(("tmux", pane))
        return tmux

    def git_attribution(cwd):
        calls.append(("git", cwd))
        return attribution

    return LaunchContext(cwd="/work", environ=environ or {}, tmux_session=tmux_session, git_attribution=git_attribution), calls


def test_build_launch_orders_arguments_like_the_bash_wrapper():
    ctx, _ = context({"TMUX": "/t", "TMUX_PANE": "%7"}, {"attribution": {"pr": ""}})

    launch = build_launch(PROFILE, ["-p", "hi"], ctx)

    assert launch.argv == ["--add-dir", "/repo dir", "--settings", '{"attribution": {"pr": ""}}', "--name", "sess-p7", "-p", "hi", "--model", "sonnet"]


def test_build_launch_clears_inherited_claude_and_anthropic_variables_and_keeps_values_literal():
    ctx, _ = context({"CLAUDE_X": "1", "ANTHROPIC_API_KEY": "k", "CLAUDE_CONFIG_DIR": "/wrong", "PATH": "/bin", "FP_CC_AUTH_SWITCH_PROFILE": "other"})

    env = build_launch(PROFILE, [], ctx).env

    assert env == {"PATH": "/bin", "CLAUDE_CONFIG_DIR": "/profiles/work", "FP_CC_AUTH_SWITCH_PROFILE": "work", "A": "$HOME `x` \"q\""}


def test_profile_env_wins_over_the_variables_the_launcher_sets():
    profile = Profile(name="work", config_dir=Path("/p"), env={"CLAUDE_CONFIG_DIR": "/custom"})

    assert build_launch(profile, [], context()[0]).env["CLAUDE_CONFIG_DIR"] == "/custom"


def test_user_settings_skip_the_attribution_lookup_and_resume_skips_tmux():
    ctx, calls = context({"TMUX": "/t", "TMUX_PANE": "%7"}, {"attribution": {"pr": ""}})

    launch = build_launch(PROFILE, ["--settings=/s.json", "--resume=abc"], ctx)

    assert calls == []
    assert launch.argv == ["--add-dir", "/repo dir", "--settings=/s.json", "--resume=abc", "--model", "sonnet"]


def test_subcommand_gets_only_the_environment():
    ctx, calls = context({"TMUX": "/t", "TMUX_PANE": "%7"}, {"attribution": {"pr": ""}})

    launch = build_launch(PROFILE, ["mcp", "list"], ctx)

    assert (launch.argv, calls, launch.env["CLAUDE_CONFIG_DIR"]) == (["mcp", "list"], [], "/profiles/work")


def test_merge_settings_merges_nested_objects_and_later_layers_win():
    assert merge_settings({"attribution": {"commit": "a", "pr": "b"}, "x": 1}, None, {"attribution": {"pr": "c"}, "x": {"y": 2}}) == {
        "attribution": {"commit": "a", "pr": "c"}, "x": {"y": 2},
    }


@pytest.mark.parametrize("argv, expected", [
    (["work"], (None, "work", [])),
    (["work", "--config", "x", "-p"], (None, "work", ["--config", "x", "-p"])),
    (["--config", "/c.yaml", "work", "--model", "opus"], (Path("/c.yaml"), "work", ["--model", "opus"])),
    (["--config=/c.yaml", "work"], (Path("/c.yaml"), "work", [])),
])
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
    (tmp_path / "claude-gone").write_text(generate_launcher_script("gone", "", Path("/bin/cc-auth-switch"), Path("/c.yaml")))
    (tmp_path / "claude-kept").write_text(generate_launcher_script("kept", "", Path("/bin/cc-auth-switch"), Path("/c.yaml")))
    (tmp_path / "claude-foreign").write_text("#!/bin/sh\nexec claude\n")

    cleanup_stale_scripts(tmp_path, {"claude-kept"})

    assert sorted(p.name for p in tmp_path.iterdir()) == ["claude-foreign", "claude-kept"]
    assert MARKER in legacy


def test_launcher_quotes_paths_and_names():
    script = generate_launcher_script("work", "d", Path("/my tools/cc-auth-switch"), Path("/cfg dir/a.yaml"))

    assert script.splitlines()[-1] == "exec '/my tools/cc-auth-switch' start --config '/cfg dir/a.yaml' work \"$@\""


def test_start_does_not_load_the_installer(sandbox):
    # start runs on every launch, so it pays for yaml but not for the installer and the sessions migration.
    config = sandbox.write("auth-switch.yaml", f"profiles_dir: {sandbox.home / 'profiles'}\nprofiles:\n  work: {{}}\n")
    script = (
        "import os, sys\n"
        "from claude_code_auth_switch.cli import main\n"
        "def fake_exec(file, args, env):\n"
        "    print(sorted(m for m in sys.modules if m.endswith(('.installer', '.sessions'))), args[1:])\n"
        "    sys.exit(0)\n"
        "os.execvpe = fake_exec\n"
        "main(['start', '--config', sys.argv[1], 'work', '-p', 'hi'])\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(config)], env=sandbox.env, capture_output=True, text=True, cwd=sandbox.home)

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
    assert f"git did not answer within 0.3 seconds while checking whether secrets file {secrets}" in capsys.readouterr().err


def test_attribution_is_dropped_when_git_does_not_answer(hanging, monkeypatch, capsys):
    from claude_code_auth_switch import git_attribution

    monkeypatch.setattr(git_attribution, "GIT_TIMEOUT_SECONDS", 0.3)

    assert launcher.git_attribution_of(str(hanging)) is None
    assert "cannot read attribution from git config: git did not answer within 0.3 seconds" in capsys.readouterr().err


def test_session_name_is_dropped_when_tmux_does_not_answer(hanging, monkeypatch):
    monkeypatch.setattr(launcher, "TMUX_TIMEOUT_SECONDS", 0.3)

    assert launcher.tmux_session_of("%1") is None
