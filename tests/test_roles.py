import json
import os
import subprocess
from pathlib import Path

import pytest

from claude_code_auth_switch.config import ConfigError
from claude_code_auth_switch.launcher import LaunchContext, LaunchError, Profile, build_launch, resolve_profile
from claude_code_auth_switch.roles import Role, RolePrompt, resolve_roles

ROLE_ENV = ("FP_CC_AUTH_SWITCH_ROLE", "FP_CC_AUTH_SWITCH_ROLE_PROMPT", "FP_CC_AUTH_SWITCH_ROLE_PROMPT_MODE")

FULL_ROLE = {
    "description": "Runs workers",
    "prompt": {"file": "/roles/dispatcher.md", "mode": "append"},
    "model": "opus",
    "effort": "high",
    "permission_mode": "auto",
    "autocompact": 250000,
    "settings": {"autoCompactEnabled": True, "permissions": {"allow": ["Read"]}},
    "tools": ["Bash", "Read"],
    "allowed_tools": ["Bash(git:*)"],
    "disallowed_tools": ["WebFetch"],
    "add_dirs": ["/role dir"],
    "plugin_dirs": ["/plugins/agent-roles"],
    "mcp_config": ["/mcp.json"],
    "strict_mcp_config": True,
    "agents": {"reviewer": {"description": "Reviews", "prompt": "Review"}},
    "env": {"ROLE_ONLY": "1", "SHARED": "role", "NUMBER": 3},
    "extra_args": ["--verbose"],
}


def roles(**config) -> dict[str, Role]:
    return resolve_roles({"roles": config})


def context(environ=None, attribution=None, files=("/roles/dispatcher.md", "/roles/worker.md")) -> LaunchContext:
    return LaunchContext(
        cwd="/work",
        environ=environ or {},
        tmux_session=lambda pane: None,
        git_attribution=lambda cwd: attribution,
        is_file=lambda path: path in files,
    )


PROFILE = Profile(name="work", config_dir=Path("/profiles/work"), env={"SHARED": "profile", "PROFILE_ONLY": "1"}, add_dirs=(Path("/profile dir"),), model="sonnet")
ROLES = roles(dispatcher=FULL_ROLE, worker={"prompt": {"file": "/roles/worker.md", "mode": "replace"}}, hooked={"prompt": {"file": "/roles/worker.md", "mode": "none"}}, bare={})


# Config validation


def test_every_key_of_the_plan_example_resolves():
    role = ROLES["dispatcher"]

    assert role.prompt == RolePrompt(file="/roles/dispatcher.md", mode="append")
    assert (role.autocompact, role.tools, role.add_dirs, role.strict_mcp_config) == ("250000", ("Bash", "Read"), ("/role dir",), True)
    assert json.loads(role.agents) == FULL_ROLE["agents"]
    assert role.env == {"ROLE_ONLY": "1", "SHARED": "role", "NUMBER": "3"}


def test_empty_role_and_missing_section_resolve():
    assert roles(worker=None)["worker"] == Role(name="worker")
    assert resolve_roles({"profiles": {}}) == {}


def test_prompt_mode_defaults_to_append_and_expands_home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/me")

    assert roles(worker={"prompt": {"file": "~/roles/worker.md"}})["worker"].prompt == RolePrompt(file="/home/me/roles/worker.md", mode="append")


@pytest.mark.parametrize("role_config, message", [
    ({"modle": "opus"}, "role 'r': unknown key 'modle'"),
    ("opus", "role 'r': must be a mapping"),
    ({"model": 3}, "role 'r': 'model' must be a non-empty string"),
    ({"tools": "Bash"}, "role 'r': 'tools' must be a list of non-empty strings"),
    ({"extra_args": ["--x", 1]}, "role 'r': 'extra_args' must be a list"),
    ({"strict_mcp_config": "yes"}, "role 'r': 'strict_mcp_config' must be true or false"),
    ({"autocompact": True}, "role 'r': 'autocompact' must be a number of tokens or 'auto'"),
    ({"settings": ["x"]}, "role 'r': 'settings' must be a mapping"),
    ({"agents": 1}, "role 'r': 'agents' must be"),
    ({"env": {"A": ["x"]}}, "role 'r': env variable 'A' must have a string or number value"),
    ({"prompt": "/r.md"}, "role 'r': 'prompt' must be a mapping"),
    ({"prompt": {"file": "roles/r.md"}}, "role 'r': 'prompt.file' must be an absolute path"),
    ({"prompt": {"mode": "append"}}, "role 'r': 'prompt.file' must be the absolute path"),
    ({"prompt": {"file": "/r.md", "mode": "prepend"}}, "role 'r': 'prompt.mode' must be one of append, replace, none"),
    ({"prompt": {"file": "/r.md", "path": "/x"}}, "role 'r': 'prompt' has unknown key 'path'"),
    ({"model": "op\0us"}, "role 'r': 'model' contains a NUL character"),
    ({"extra_args": ["--x\0"]}, "role 'r': an entry of 'extra_args' contains a NUL character"),
    ({"plugin_dirs": ["/p\0"]}, "role 'r': an entry of 'plugin_dirs' contains a NUL character"),
    ({"mcp_config": ["/m\0"]}, "role 'r': an entry of 'mcp_config' contains a NUL character"),
    ({"autocompact": "1\0"}, "role 'r': 'autocompact' contains a NUL character"),
    ({"agents": "/a\0.json"}, "role 'r': 'agents' contains a NUL character"),
    ({"prompt": {"file": "/r\0.md"}}, "role 'r': 'prompt.file' contains a NUL character"),
    ({"env": {"A-B": "1"}}, "role 'r': env variable name 'A-B' is not a valid shell variable name"),
    ({"env": {"A": "x\0"}}, "role 'r': env value of 'A' contains a NUL character"),
])
def test_invalid_role_names_the_role_and_the_key(role_config, message):
    with pytest.raises(ConfigError, match=message.replace("(", r"\(")):
        roles(r=role_config)


def test_roles_must_be_a_mapping_and_none_is_reserved():
    with pytest.raises(ConfigError, match="'roles' must be a mapping"):
        resolve_roles({"roles": ["dispatcher"]})
    with pytest.raises(ConfigError, match="role name 'none' is reserved"):
        roles(none={})
    with pytest.raises(ConfigError, match="role name 1 must be a non-empty string"):
        resolve_roles({"roles": {1: {}}})


def test_default_role_must_name_a_defined_role():
    config = {"profiles": {"work": {"default_role": "dispatcher"}}, "roles": {"worker": {}}}

    with pytest.raises(ConfigError, match="profile 'work': default_role 'dispatcher' is not defined"):
        resolve_profile(config, {}, Path("/p"), "work")
    assert resolve_profile({**config, "roles": {"dispatcher": {}}}, {}, Path("/p"), "work").default_role == "dispatcher"


# Launch


def test_role_flags_follow_the_profile_and_come_before_the_user_arguments():
    launch = build_launch(PROFILE, ["--agent-role", "dispatcher", "-p", "do it"], context(attribution={"attribution": {"pr": ""}}), ROLES)

    assert launch.argv == [
        "--add-dir", "/profile dir", "--add-dir=/role dir",
        "--settings", json.dumps({"attribution": {"pr": ""}, "autoCompactEnabled": True, "permissions": {"allow": ["Read"]}}),
        "--effort", "high", "--permission-mode", "auto", "--autocompact", "250000", "--agents", json.dumps(FULL_ROLE["agents"]),
        "--tools=Bash", "--tools=Read", "--allowed-tools=Bash(git:*)", "--disallowed-tools=WebFetch",
        "--plugin-dir=/plugins/agent-roles", "--mcp-config=/mcp.json", "--strict-mcp-config",
        "--append-system-prompt-file", "/roles/dispatcher.md", "--verbose",
        "-p", "do it", "--model", "opus",
    ]
    assert launch.notes == ()


def test_user_flags_win_over_the_role_and_list_flags_concatenate():
    args = ["--agent-role=dispatcher", "--model", "haiku", "--effort=low", "--allowed-tools", "Edit", "--add-dir", "/mine", "--agents", "/a.json"]

    argv = build_launch(PROFILE, args, context(), ROLES).argv

    assert "opus" not in argv and "sonnet" not in argv and "high" not in argv
    assert argv.count("--effort") == 0 and argv.count("--agents") == 1
    assert ["--allowed-tools=Bash(git:*)", "--allowed-tools", "Edit"] == [arg for arg in argv if arg.startswith("--allowed-tools") or arg == "Edit"]
    assert argv[-9:] == args[1:]


def test_user_settings_are_merged_over_the_role_settings():
    argv = build_launch(PROFILE, ["--agent-role", "dispatcher", "--settings", '{"autoCompactEnabled": false, "permissions": {"allow": ["Bash"]}}'], context(), ROLES).argv

    assert json.loads(argv[argv.index("--settings") + 1]) == {"autoCompactEnabled": False, "permissions": {"allow": ["Read", "Bash"]}}
    assert argv.count("--settings") == 1


def test_role_env_is_merged_over_the_profile_and_role_variables_are_set():
    env = build_launch(PROFILE, ["--agent-role", "dispatcher"], context(), ROLES).env

    assert {key: env[key] for key in ("SHARED", "PROFILE_ONLY", "ROLE_ONLY", "NUMBER")} == {"SHARED": "role", "PROFILE_ONLY": "1", "ROLE_ONLY": "1", "NUMBER": "3"}
    assert [env[key] for key in ROLE_ENV] == ["dispatcher", "/roles/dispatcher.md", "append"]


@pytest.mark.parametrize("role, flag, mode", [("worker", ["--system-prompt-file", "/roles/worker.md"], "replace"), ("hooked", [], "none")])
def test_prompt_modes(role, flag, mode):
    launch = build_launch(PROFILE, ["--agent-role", role], context(), ROLES)

    assert launch.argv == ["--add-dir", "/profile dir", *flag, "--model", "sonnet"]
    assert [launch.env[key] for key in ROLE_ENV] == [role, "/roles/worker.md", mode]


def test_a_users_own_prompt_flag_wins_and_the_hooks_get_mode_none():
    launch = build_launch(PROFILE, ["--agent-role", "worker", "--system-prompt", "mine"], context(), ROLES)

    assert "--system-prompt-file" not in launch.argv
    assert launch.env["FP_CC_AUTH_SWITCH_ROLE_PROMPT_MODE"] == "none"
    assert "replaces the prompt of role 'worker'" in launch.notes[0]


def test_inherited_role_variables_are_cleared_without_a_role():
    inherited = {"PATH": "/bin", **dict.fromkeys(ROLE_ENV, "parent")}

    for args in ([], ["--agent-role", "none"], ["--agent-role", "bare"], ["mcp", "list"]):
        env = build_launch(PROFILE, args, context(inherited), ROLES).env
        assert {key: env.get(key) for key in ROLE_ENV} == {"FP_CC_AUTH_SWITCH_ROLE": "bare" if "bare" in args else None, "FP_CC_AUTH_SWITCH_ROLE_PROMPT": None, "FP_CC_AUTH_SWITCH_ROLE_PROMPT_MODE": None}, args


def test_default_role_applies_without_agent_role_and_none_turns_it_off():
    profile = Profile(name="work", config_dir=Path("/p"), default_role="worker")

    assert build_launch(profile, [], context(), ROLES).env["FP_CC_AUTH_SWITCH_ROLE"] == "worker"
    assert build_launch(profile, ["--agent-role", "dispatcher"], context(), ROLES).env["FP_CC_AUTH_SWITCH_ROLE"] == "dispatcher"
    none = build_launch(profile, ["--agent-role", "none"], context(), ROLES)
    assert ("FP_CC_AUTH_SWITCH_ROLE" not in none.env, none.argv) == (True, [])


def test_agent_role_never_reaches_claude_and_subcommands_get_no_role():
    launch = build_launch(PROFILE, ["--agent-role", "dispatcher", "mcp", "list"], context(), ROLES)

    assert (launch.argv, "FP_CC_AUTH_SWITCH_ROLE" in launch.env) == (["mcp", "list"], False)


def test_arguments_after_double_dash_stay_with_claude():
    assert build_launch(PROFILE, ["--", "--agent-role", "x"], context(), ROLES).argv == ["--add-dir", "/profile dir", "--", "--agent-role", "x", "--model", "sonnet"]


@pytest.mark.parametrize("args, message", [
    (["--agent-role", "ghost"], r"--agent-role: role 'ghost' is not defined in the config \(defined roles: bare, dispatcher, hooked, worker\)"),
    (["--agent-role"], "--agent-role needs a role name, or 'none'"),
])
def test_unknown_or_missing_agent_role_stops_the_launch(args, message):
    with pytest.raises(LaunchError, match=message):
        build_launch(PROFILE, args, context(), ROLES)


def test_missing_prompt_file_stops_the_launch_with_the_path():
    with pytest.raises(LaunchError, match="role 'dispatcher': the prompt file /roles/dispatcher.md does not exist"):
        build_launch(PROFILE, ["--agent-role", "dispatcher"], context(files=()), ROLES)


# Through the launcher


FAKE_CLAUDE = """\
#!/usr/bin/env python3
import json, os, sys
print(json.dumps({"argv": sys.argv[1:], "env": {key: os.environ.get(key) for key in ("FP_CC_AUTH_SWITCH_PROFILE", "FP_CC_AUTH_SWITCH_ROLE", "FP_CC_AUTH_SWITCH_ROLE_PROMPT", "FP_CC_AUTH_SWITCH_ROLE_PROMPT_MODE", "ROLE_ONLY")}}))
"""


@pytest.fixture
def role_launch(sandbox):
    prompt = sandbox.write("roles/dispatcher.md", "You dispatch.\n")
    source_dir = sandbox.mkdir(".claude")
    (source_dir / "settings.json").write_text("{}\n")
    config = sandbox.write(
        "auth-switch.yaml",
        f"source_dir: {source_dir}\nbin_dir: {sandbox.home / 'bin'}\nprofiles_dir: {sandbox.home / 'profiles'}\n"
        "profiles:\n  work:\n    default_role: dispatcher\n"
        f"roles:\n  dispatcher:\n    prompt: {{file: {prompt}, mode: append}}\n    model: opus\n    env: {{ROLE_ONLY: '1'}}\n",
    )
    sandbox.run_install("--config", str(config))
    fake_bin = sandbox.mkdir("fake-bin")
    (fake_bin / "claude").write_text(FAKE_CLAUDE)
    (fake_bin / "claude").chmod(0o755)
    env = {**sandbox.env, "PATH": f"{fake_bin}{os.pathsep}{sandbox.env['PATH']}", "FP_CC_AUTH_SWITCH_ROLE": "inherited"}

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(sandbox.home / "bin/claude-work"), *args], cwd=sandbox.home, env=env, capture_output=True, text=True)

    return run, config, prompt


def test_launcher_applies_the_default_role(role_launch):
    run, config, prompt = role_launch

    result = run("-p", "hi")

    assert json.loads(result.stdout) == {
        "argv": ["--append-system-prompt-file", str(prompt), "-p", "hi", "--model", "opus"],
        "env": {"FP_CC_AUTH_SWITCH_PROFILE": "work", "FP_CC_AUTH_SWITCH_ROLE": "dispatcher", "FP_CC_AUTH_SWITCH_ROLE_PROMPT": str(prompt), "FP_CC_AUTH_SWITCH_ROLE_PROMPT_MODE": "append", "ROLE_ONLY": "1"},
    }
    assert json.loads(run("--agent-role", "none").stdout)["env"]["FP_CC_AUTH_SWITCH_ROLE"] is None


def test_launcher_refuses_an_unknown_role_and_a_missing_prompt_file(role_launch):
    run, config, prompt = role_launch

    unknown = run("--agent-role", "ghost")
    prompt.unlink()
    missing = run()

    assert (unknown.returncode, unknown.stdout) == (2, "")
    assert "role 'ghost' is not defined" in unknown.stderr
    assert (missing.returncode, missing.stdout) == (2, "")
    assert f"the prompt file {prompt} does not exist" in missing.stderr


def test_install_and_start_refuse_an_invalid_role(role_launch, sandbox):
    run, config, prompt = role_launch
    config.write_text(config.read_text().replace("model: opus", "modle: opus"))

    install = sandbox.run_install("--config", str(config), check=False)
    start = run()

    for result in (install, start):
        assert result.returncode == 1 and result.stdout.count("argv") == 0
        assert f"{config}: role 'dispatcher': unknown key 'modle'" in result.stderr
