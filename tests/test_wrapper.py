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


def test_launchers_exec_start_by_absolute_path_and_pass_bash_syntax_check(installed, sandbox):
    bin_dir, env = installed
    for profile in ("plain", "modeled"):
        launcher = bin_dir / f"claude-{profile}"
        command = f'exec {sandbox.tool_bin / "cc-auth-switch"} start --config {sandbox.home / "auth-switch.yaml"} {profile} "$@"'
        assert launcher.read_text().splitlines()[-1] == command
        subprocess.run(["bash", "-n", str(launcher)], check=True)


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


@pytest.mark.parametrize(
    "subcommand",
    [
        "mcp",
        "auth",
        "doctor",
        "install",
        "setup-token",
        "update",
        "upgrade",
        "agents",
        "auto-mode",
        "plugin",
        "plugins",
        "attach",
        "logs",
        "respawn",
        "rm",
        "stop",
        "kill",
        "purge",
        "import",
        "gateway",
        "ultrareview",
    ],
)
def test_subcommands_get_neither_model_nor_add_dir(installed, sandbox, subcommand):
    bin_dir, env = installed

    argv = json.loads(run_wrapper(bin_dir, env, "modeled", sandbox.home, subcommand, "arg").stdout)

    assert argv == [subcommand, "arg"]


@pytest.mark.parametrize("user_flag", [["--settings", "{own}"], ["--settings={own}"]])
def test_user_settings_flag_is_merged_over_the_git_config_attribution(installed, sandbox, user_flag):
    bin_dir, env = installed
    repo = sandbox.git_init("devel/projects/company/service")
    own = sandbox.write("own.json", '{"attribution": {"sessionUrl": true}, "model": "opus"}')

    argv = json.loads(run_wrapper(bin_dir, env, "plain", repo, *(arg.format(own=own) for arg in user_flag)).stdout)

    assert argv == [
        "--settings",
        json.dumps(
            {"attribution": {**EXPECTED["attribution"], "sessionUrl": True}, "model": "opus"}, ensure_ascii=False
        ),
    ]


def test_unreadable_user_settings_stops_the_launch(installed, sandbox):
    bin_dir, env = installed
    repo = sandbox.git_init("devel/projects/company/service")

    result = run_wrapper(bin_dir, env, "plain", repo, "--settings", "missing.json")

    assert (result.returncode, result.stdout) == (2, "")
    assert "--settings: cannot read" in result.stderr and "not starting Claude for profile 'plain'" in result.stderr


def test_missing_tool_stops_the_launch(installed, sandbox):
    bin_dir, env = installed
    (sandbox.tool_bin / "cc-auth-switch").unlink()

    result = run_wrapper(bin_dir, env, "plain", sandbox.home, "-p", "hello")

    assert result.returncode == 127
    assert result.stdout == ""


def assert_launch_refused(result: subprocess.CompletedProcess, *expected: str) -> None:
    assert result.returncode != 0
    assert result.stdout == "", "claude must not start"
    for text in expected:
        assert text in result.stderr


def test_unparsable_config_stops_the_launch(installed, sandbox):
    bin_dir, env = installed
    config = sandbox.home / "auth-switch.yaml"
    config.write_text(config.read_text() + "  broken: [\n")

    assert_launch_refused(
        run_wrapper(bin_dir, env, "plain", sandbox.home),
        f"cannot read {config}",
        "not starting Claude for profile 'plain'",
    )


@pytest.mark.parametrize(
    "profile_yaml, error",
    [
        ("    env: [A, B]\n", "profile 'modeled': 'env' must be a mapping"),
        ("    add_dirs: ~/single\n", "profile 'modeled': 'add_dirs' must be a list of paths"),
        ("    pinned_models:\n      gpt: x\n", "unknown pinned_models class 'gpt'"),
        ("    env:\n      A=B: x\n", "profile 'modeled': env variable name 'A=B' is not a valid shell variable name"),
        ("    env:\n      1X: x\n", "profile 'modeled': env variable name '1X' is not a valid shell variable name"),
        ('    env:\n      A: "a\\0b"\n', "profile 'modeled': env value of 'A' contains a NUL character"),
        ('    add_dirs:\n      - "/a\\0b"\n', "profile 'modeled': an 'add_dirs' entry contains a NUL character"),
    ],
)
def test_invalid_profile_stops_the_launch(installed, sandbox, profile_yaml, error):
    bin_dir, env = installed
    config = sandbox.home / "auth-switch.yaml"
    config.write_text(config.read_text() + profile_yaml)

    assert_launch_refused(run_wrapper(bin_dir, env, "modeled", sandbox.home), error, str(config))


def test_install_refuses_an_env_name_that_cannot_reach_claude(installed, sandbox):
    config = sandbox.home / "auth-switch.yaml"
    config.write_text(config.read_text() + "    env:\n      A-B: x\n")

    result = sandbox.run_install("--config", str(config), check=False)

    assert result.returncode == 1
    assert (
        f"Error: {config}: profile 'modeled': env variable name 'A-B' is not a valid shell variable name"
        in result.stderr
    )


def test_removed_profile_stops_the_launch(installed, sandbox):
    bin_dir, env = installed
    config = sandbox.home / "auth-switch.yaml"
    config.write_text(config.read_text().split("  modeled:")[0])

    assert_launch_refused(
        run_wrapper(bin_dir, env, "modeled", sandbox.home), f"{config}: profile 'modeled' is not defined"
    )


def test_missing_config_stops_the_launch(installed, sandbox):
    bin_dir, env = installed
    config = sandbox.home / "auth-switch.yaml"
    config.unlink()

    assert_launch_refused(run_wrapper(bin_dir, env, "plain", sandbox.home), f"{config}: the file does not exist")


def test_secrets_file_that_git_would_publish_stops_the_launch(installed, sandbox):
    bin_dir, env = installed
    sandbox.git_init(".")
    secrets = sandbox.write("auth-switch.secrets.yaml", 'profiles:\n  plain:\n    env:\n      KEY: "value"\n')

    assert_launch_refused(
        run_wrapper(bin_dir, env, "plain", sandbox.home),
        f"Secrets file {secrets} is inside a git repository but is not ignored",
        str(sandbox.home / "auth-switch.yaml"),
    )


def test_invalid_secrets_file_stops_the_launch(installed, sandbox):
    bin_dir, env = installed
    secrets = sandbox.write("auth-switch.secrets.yaml", "profiles:\n  plain:\n    model: opus\n")

    assert_launch_refused(
        run_wrapper(bin_dir, env, "plain", sandbox.home), f"{secrets}: profile 'plain' may only contain 'env'"
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files regardless of their mode")
def test_unreadable_secrets_file_stops_the_launch(installed, sandbox):
    bin_dir, env = installed
    secrets = sandbox.write("auth-switch.secrets.yaml", "profiles: {}\n")
    secrets.chmod(0)

    assert_launch_refused(run_wrapper(bin_dir, env, "plain", sandbox.home), f"cannot read {secrets}")


def test_config_changes_apply_without_reinstalling(installed, sandbox):
    bin_dir, env = installed
    config = sandbox.home / "auth-switch.yaml"
    config.write_text(config.read_text().replace("model: sonnet", "model: opus"))

    assert json.loads(run_wrapper(bin_dir, env, "modeled", sandbox.home, "-p", "x").stdout)[-4:] == [
        "--model",
        "opus",
        "-p",
        "x",
    ]


def test_install_fails_when_the_helper_is_not_on_path(installed, sandbox):
    (sandbox.tool_bin / "cc-auth-switch").unlink()
    env = {**sandbox.env, "PATH": f"{sandbox.tool_bin}{os.pathsep}/usr/bin{os.pathsep}/bin"}

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "claude_code_auth_switch",
            "install",
            "--config",
            str(sandbox.home / "auth-switch.yaml"),
        ],
        env=env,
        capture_output=True,
        text=True,
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


FAKE_TMUX = """\
#!/bin/sh
[ -n "${FAKE_TMUX_FAIL:-}" ] && exit 1
[ "$*" = "display-message -p -t $TMUX_PANE #S" ] || exit 2
printf '%s\\n' "$FAKE_TMUX_SESSION"
"""


@pytest.fixture
def in_tmux(installed, sandbox):
    bin_dir, env = installed
    fake_tmux = sandbox.home / "fake-bin" / "tmux"
    fake_tmux.write_text(FAKE_TMUX)
    fake_tmux.chmod(0o755)
    return bin_dir, {**env, "TMUX": "/tmp/tmux-1000/default,1,0", "TMUX_PANE": "%12", "FAKE_TMUX_SESSION": "owner/repo"}


@pytest.mark.parametrize("profile", ["plain", "modeled"])
def test_wrapper_names_the_session_after_the_tmux_session_and_pane(in_tmux, sandbox, profile):
    bin_dir, env = in_tmux

    result = run_wrapper(bin_dir, env, profile, sandbox.home, "-p", "hello")

    assert result.returncode == 0, result.stderr
    argv = json.loads(result.stdout)
    assert argv[argv.index("--name") + 1] == "owner/repo-p12"


def test_wrapper_outside_tmux_passes_no_name(installed, sandbox):
    bin_dir, env = installed

    assert "--name" not in json.loads(run_wrapper(bin_dir, env, "plain", sandbox.home).stdout)


@pytest.mark.parametrize(
    "user_args",
    [
        ["-n", "mine"],
        ["--name", "mine"],
        ["--name=mine"],
        ["-r"],
        ["--resume"],
        ["--resume", "abc"],
        ["--resume=abc"],
        ["-c"],
        ["--continue"],
        ["--session-id", "abc"],
        ["--session-id=abc"],
        ["--fork-session"],
        ["--from-pr", "7"],
        ["--from-pr=7"],
    ],
)
def test_wrapper_passes_no_name_when_the_session_already_has_or_resumes_one(in_tmux, sandbox, user_args):
    bin_dir, env = in_tmux

    argv = json.loads(run_wrapper(bin_dir, env, "plain", sandbox.home, *user_args).stdout)

    assert argv == user_args


def test_wrapper_launches_without_name_when_tmux_fails(in_tmux, sandbox):
    bin_dir, env = in_tmux

    result = run_wrapper(bin_dir, {**env, "FAKE_TMUX_FAIL": "1"}, "plain", sandbox.home, "-p", "hello")

    assert (result.returncode, json.loads(result.stdout)) == (0, ["-p", "hello"])


def test_subcommands_get_no_name(in_tmux, sandbox):
    bin_dir, env = in_tmux

    assert json.loads(run_wrapper(bin_dir, env, "modeled", sandbox.home, "mcp", "list").stdout) == ["mcp", "list"]


@pytest.mark.parametrize("name", ["123", "a/b", "''"])
def test_install_refuses_a_profile_name_that_is_no_usable_string_before_writing_anything(sandbox, name):
    bin_dir = sandbox.home / "bin"
    config = sandbox.write(
        "auth-switch.yaml",
        f"source_dir: {sandbox.mkdir('.claude')}\nbin_dir: {bin_dir}\nprofiles_dir: {sandbox.home / 'profiles'}\nprofiles:\n  first: {{}}\n  {name}: {{}}\n",
    )

    result = sandbox.run_install("--config", str(config), check=False)

    assert result.returncode == 1
    assert f"Error: {config}: profile name" in result.stderr
    assert "Traceback" not in result.stderr
    assert not bin_dir.exists() and not (sandbox.home / "profiles").exists()


def run_start(sandbox, env, cwd, *args):
    return subprocess.run(["cc-auth-switch", "start", *args], cwd=cwd, env=env, capture_output=True, text=True)


def test_start_accepts_config_with_equals_sign(installed, sandbox):
    bin_dir, env = installed

    result = run_start(
        sandbox, env, sandbox.home, f"--config={sandbox.home / 'auth-switch.yaml'}", "modeled", "-p", "x"
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)[-4:] == ["--model", "sonnet", "-p", "x"]


def test_start_reads_a_relative_config_and_its_secrets_from_the_current_directory(installed, sandbox):
    bin_dir, env = installed
    sandbox.git_init(".")
    config_dir = sandbox.mkdir("config")
    (config_dir / "auth-switch.yaml").write_text((sandbox.home / "auth-switch.yaml").read_text())
    (config_dir / "auth-switch.secrets.yaml").write_text('profiles:\n  plain:\n    env:\n      SECRET: "value"\n')
    # Anchored to config/, so git must be asked about the file by its real path.
    (config_dir / ".gitignore").write_text("/auth-switch.secrets.yaml\n")

    result = run_start(
        sandbox,
        {**env, "PWD": str(sandbox.home)},
        sandbox.home,
        "--config",
        "config/auth-switch.yaml",
        "plain",
        "-p",
        "x",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["-p", "x"]
