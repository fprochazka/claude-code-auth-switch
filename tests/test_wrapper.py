import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT

FAKE_CLAUDE = """\
#!/usr/bin/env python3
import json, sys
print(json.dumps(sys.argv[1:]))
"""

COMPANY_INCLUDE = """\
[includeIf "gitdir:~/devel/projects/company/"]
    path = ~/devel/projects/company/.gitconfig
"""

COMPANY_ATTRIBUTION = """\
[claude-code "attribution"]
    commit = "Line one\\n\\nCo-Authored-By: Claude <noreply@example.com>"
    sessionUrl = false
"""


@pytest.fixture
def installed(sandbox):
    """Run the installer against a throwaway config and return (bin_dir, env for running the wrappers)."""
    source_dir = sandbox.mkdir(".claude")
    (source_dir / "settings.json").write_text("{}\n")
    bin_dir = sandbox.home / "bin"
    profiles_dir = sandbox.home / "profiles"
    config = sandbox.write(
        "auth-switch.yaml",
        f"source_dir: {source_dir}\n"
        f"bin_dir: {bin_dir}\n"
        f"profiles_dir: {profiles_dir}\n"
        "profiles:\n"
        "  plain:\n"
        "    description: no model\n"
        "  modeled:\n"
        "    description: with model and add_dirs\n"
        "    model: sonnet\n"
        "    add_dirs:\n"
        "      - ~/devel/projects/company\n",
    )
    sandbox.run_install("--config", str(config))

    fake_bin = sandbox.mkdir("fake-bin")
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(FAKE_CLAUDE)
    fake_claude.chmod(0o755)

    sandbox.write(".gitconfig", COMPANY_INCLUDE)
    sandbox.write("devel/projects/company/.gitconfig", COMPANY_ATTRIBUTION)

    env = {**sandbox.env, "PATH": f"{fake_bin}{os.pathsep}{sandbox.env['PATH']}"}
    return bin_dir, env


def run_wrapper(bin_dir: Path, env: dict, profile: str, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(bin_dir / f"claude-{profile}"), *args], cwd=cwd, env=env, capture_output=True, text=True)


def settings_from_argv(argv: list[str]) -> list[dict]:
    return [json.loads(argv[i + 1]) for i, arg in enumerate(argv) if arg == "--settings"]


EXPECTED = {"attribution": {"commit": "Line one\n\nCo-Authored-By: Claude <noreply@example.com>", "sessionUrl": False}}


def test_wrappers_call_the_helper_by_absolute_path_and_pass_bash_syntax_check(installed, sandbox):
    bin_dir, env = installed
    for profile in ("plain", "modeled"):
        assert f'auth_switch="{sandbox.tool_bin / "cc-auth-switch"}"' in (bin_dir / f"claude-{profile}").read_text()
    for profile in ("plain", "modeled"):
        subprocess.run(["bash", "-n", str(bin_dir / f"claude-{profile}")], check=True)


@pytest.mark.parametrize("profile", ["plain", "modeled"])
def test_wrapper_passes_settings_in_company_directory(installed, sandbox, profile):
    bin_dir, env = installed
    repo = sandbox.git_init("devel/projects/company/service")

    result = run_wrapper(bin_dir, env, profile, repo, "--resume")

    assert result.returncode == 0, result.stderr
    argv = json.loads(result.stdout)
    assert settings_from_argv(argv) == [EXPECTED]
    assert argv[-1] in ("--resume", "sonnet")


def test_wrapper_passes_settings_outside_repo(installed, sandbox):
    bin_dir, env = installed
    company = sandbox.mkdir("devel/projects/company")

    argv = json.loads(run_wrapper(bin_dir, env, "plain", company).stdout)

    assert argv == ["--settings", json.dumps(EXPECTED, ensure_ascii=False)]


def test_wrapper_without_matching_config_passes_no_settings(installed, sandbox):
    bin_dir, env = installed
    personal = sandbox.git_init("devel/personal/project")

    result = run_wrapper(bin_dir, env, "plain", personal, "-p", "hello")

    assert json.loads(result.stdout) == ["-p", "hello"]
    assert result.stderr == ""


def test_subcommand_passthrough_gets_no_settings(installed, sandbox):
    bin_dir, env = installed
    repo = sandbox.git_init("devel/projects/company/service")

    argv = json.loads(run_wrapper(bin_dir, env, "modeled", repo, "mcp", "list").stdout)

    assert argv == ["mcp", "list"]


@pytest.mark.parametrize("subcommand", [
    "mcp", "auth", "doctor", "install", "setup-token", "update", "upgrade", "agents", "auto-mode", "plugin", "plugins",
    "attach", "logs", "respawn", "rm", "stop", "kill", "purge", "import", "gateway", "ultrareview",
])
def test_subcommands_get_neither_model_nor_add_dir(installed, sandbox, subcommand):
    bin_dir, env = installed

    argv = json.loads(run_wrapper(bin_dir, env, "modeled", sandbox.home, subcommand, "arg").stdout)

    assert argv == [subcommand, "arg"]


@pytest.mark.parametrize("user_flag", [["--settings", "/tmp/own.json"], ["--settings=/tmp/own.json"]])
def test_user_settings_flag_replaces_the_wrapper_settings(installed, sandbox, user_flag):
    bin_dir, env = installed
    repo = sandbox.git_init("devel/projects/company/service")

    argv = json.loads(run_wrapper(bin_dir, env, "plain", repo, *user_flag).stdout)

    assert argv == user_flag


def test_missing_helper_warns_and_launches(installed, sandbox):
    bin_dir, env = installed
    (sandbox.tool_bin / "cc-auth-switch").unlink()
    repo = sandbox.git_init("devel/projects/company/service")

    result = run_wrapper(bin_dir, env, "plain", repo, "--resume")

    assert result.returncode == 0
    assert json.loads(result.stdout) == ["--resume"]
    assert "launching without git config attribution" in result.stderr


def test_failing_helper_warns_and_launches(installed, sandbox):
    bin_dir, env = installed
    helper = sandbox.tool_bin / "cc-auth-switch"
    helper.write_text("#!/bin/sh\necho partial\nexit 3\n")
    repo = sandbox.git_init("devel/projects/company/service")

    result = run_wrapper(bin_dir, env, "plain", repo, "--resume")

    assert result.returncode == 0
    assert json.loads(result.stdout) == ["--resume"]
    assert "failed, launching without git config attribution" in result.stderr


def test_install_fails_when_the_helper_is_not_on_path(installed, sandbox):
    (sandbox.tool_bin / "cc-auth-switch").unlink()
    env = {**sandbox.env, "PATH": f"{sandbox.tool_bin}{os.pathsep}/usr/bin{os.pathsep}/bin"}

    result = subprocess.run(
        [sys.executable, "-m", "claude_code_auth_switch", "install", "--config", str(sandbox.home / "auth-switch.yaml")],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode == 1
    assert "cc-auth-switch is not on PATH" in result.stderr


def test_install_py_stub_points_at_the_command():
    result = subprocess.run([sys.executable, str(REPO_ROOT / "install.py")], capture_output=True, text=True)

    assert result.returncode == 1
    assert "cc-auth-switch install" in result.stderr


LEGACY_HELPER = REPO_ROOT / "tests" / "fixtures" / "legacy-claude-auth-switch-attribution"


def test_install_removes_the_helper_that_install_py_copied(installed, sandbox):
    bin_dir, env = installed
    helper = bin_dir / "claude-auth-switch-attribution"
    helper.write_bytes(LEGACY_HELPER.read_bytes())

    result = sandbox.run_install("--config", str(sandbox.home / "auth-switch.yaml"))

    assert not helper.exists()
    assert f"removed old {helper}" in result.stdout


def test_install_keeps_files_that_are_not_clearly_ours(installed, sandbox):
    bin_dir, env = installed
    edited = bin_dir / "claude-auth-switch-attribution"
    edited.write_text(LEGACY_HELPER.read_text() + "# local change\n")
    foreign = bin_dir / "claude-code-auth-switch"
    foreign.write_text("#!/bin/sh\n")

    result = sandbox.run_install("--config", str(sandbox.home / "auth-switch.yaml"))

    assert edited.exists() and foreign.exists()
    assert f"warning: left {edited} in place" in result.stdout
    assert f"warning: left {foreign} in place" in result.stdout


def test_install_removes_entry_points_of_an_older_uv_install(installed, sandbox):
    bin_dir, env = installed
    tool_bin = sandbox.mkdir("uv-tools/claude-code-auth-switch/bin")
    for name in ("claude-auth-switch-attribution", "claude-auth-switch-session-start", "claude-code-auth-switch"):
        (bin_dir / name).symlink_to(tool_bin / name)

    sandbox.run_install("--config", str(sandbox.home / "auth-switch.yaml"))

    assert sorted(p.name for p in bin_dir.iterdir()) == ["claude-modeled", "claude-plain"]
