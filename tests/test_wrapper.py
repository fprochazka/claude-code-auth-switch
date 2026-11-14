import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import INSTALL

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
    """Run install.py against a throwaway config and return (bin_dir, env for running the wrappers)."""
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
    subprocess.run([sys.executable, str(INSTALL), "--config", str(config)], env=sandbox.env, check=True, capture_output=True)

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


def test_helper_is_installed_and_wrappers_pass_bash_syntax_check(installed):
    bin_dir, env = installed
    helper = bin_dir / "claude-auth-switch-attribution"
    assert os.access(helper, os.X_OK)
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


@pytest.mark.parametrize("user_flag", [["--settings", "/tmp/own.json"], ["--settings=/tmp/own.json"]])
def test_user_settings_flag_replaces_the_wrapper_settings(installed, sandbox, user_flag):
    bin_dir, env = installed
    repo = sandbox.git_init("devel/projects/company/service")

    argv = json.loads(run_wrapper(bin_dir, env, "plain", repo, *user_flag).stdout)

    assert argv == user_flag


def test_missing_helper_warns_and_launches(installed, sandbox):
    bin_dir, env = installed
    (bin_dir / "claude-auth-switch-attribution").unlink()
    repo = sandbox.git_init("devel/projects/company/service")

    result = run_wrapper(bin_dir, env, "plain", repo, "--resume")

    assert result.returncode == 0
    assert json.loads(result.stdout) == ["--resume"]
    assert "launching without git config attribution" in result.stderr


def test_failing_helper_warns_and_launches(installed, sandbox):
    bin_dir, env = installed
    helper = bin_dir / "claude-auth-switch-attribution"
    helper.write_text("#!/bin/sh\necho partial\nexit 3\n")
    repo = sandbox.git_init("devel/projects/company/service")

    result = run_wrapper(bin_dir, env, "plain", repo, "--resume")

    assert result.returncode == 0
    assert json.loads(result.stdout) == ["--resume"]
    assert "failed, launching without git config attribution" in result.stderr


def test_reinstall_keeps_the_helper(installed, sandbox):
    bin_dir, env = installed
    config = sandbox.home / "auth-switch.yaml"

    subprocess.run([sys.executable, str(INSTALL), "--config", str(config)], env=sandbox.env, check=True, capture_output=True)

    assert (bin_dir / "claude-auth-switch-attribution").exists()
