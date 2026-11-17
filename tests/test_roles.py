import dataclasses
import json
import os
import subprocess
from pathlib import Path

import pytest

from claude_code_auth_switch import launcher
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


@pytest.mark.parametrize("args", [["--version"], ["-v"], ["-p", "x", "--help"], ["-h"]])
def test_version_and_help_pass_through_without_role_or_prompt_check(args):
    inherited = {"TMUX": "/t", "TMUX_PANE": "%7", **dict.fromkeys(ROLE_ENV, "parent")}
    profile = dataclasses.replace(PROFILE, default_role="dispatcher")

    for role_args in ([], ["--agent-role", "worker"]):
        launch = build_launch(profile, [*role_args, *args], context(inherited, files=()), ROLES)
        assert launch.argv == args
        assert not any(key in launch.env for key in ROLE_ENV)


def test_help_after_double_dash_is_a_prompt_word():
    argv = build_launch(PROFILE, ["--", "-h"], context(), ROLES).argv

    assert "--model" in argv and argv[argv.index("--"):][:2] == ["--", "-h"]


def test_agent_role_inside_a_subcommand_belongs_to_the_subcommand():
    assert build_launch(PROFILE, ["--agent-role", "worker", "mcp", "add", "--agent-role", "x"], context(), ROLES).argv == ["mcp", "add", "--agent-role", "x"]


@pytest.mark.parametrize("args, message", [
    (["-p", "hi", "--agent-role", "worker"], "--agent-role must come before the arguments for claude"),
    (["-c", "--agent-role=worker"], "--agent-role must come before the arguments for claude"),
    (["--agent-role="], "--agent-role needs a role name"),
    (["--agent-role", "-p"], "--agent-role needs a role name"),
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


# Resume

SESSION = "0b6f2c1e-1111-4222-8333-444455556666"


def resume_context(recorded=None, newest=None, **kwargs) -> tuple[LaunchContext, list]:
    calls: list = []

    def recorded_start(config_dir, session_id, cwd):
        calls.append(("recorded", config_dir, session_id, cwd))
        return recorded

    def newest_start(config_dir, cwd):
        calls.append(("newest", config_dir, cwd))
        return newest

    return dataclasses.replace(context(**kwargs), recorded_start=recorded_start, newest_start=newest_start), calls


@pytest.mark.parametrize("args", [["--resume", SESSION], ["-r", SESSION], [f"--resume={SESSION}"], [f"-r{SESSION}"], ["--session-id", SESSION]])
def test_resume_reapplies_the_recorded_role(args):
    ctx, calls = resume_context(recorded={"v": 1, "role": "worker"})

    launch = build_launch(PROFILE, args, ctx, ROLES)

    assert launch.env["FP_CC_AUTH_SWITCH_ROLE"] == "worker"
    assert launch.argv == ["--add-dir", "/profile dir", "--system-prompt-file", "/roles/worker.md", *args, "--model", "sonnet"]
    assert (launch.notes, calls[0]) == ((), ("recorded", Path("/profiles/work"), SESSION, "/work"))


def test_resume_of_a_session_without_role_or_log_ignores_the_default_role():
    profile = dataclasses.replace(PROFILE, default_role="dispatcher")

    for recorded in ({"v": 1, "role": None}, {"v": 1}, None):
        launch = build_launch(profile, ["--resume", SESSION], resume_context(recorded=recorded)[0], ROLES)
        assert ("FP_CC_AUTH_SWITCH_ROLE" not in launch.env, launch.notes) == (True, ()), recorded


def test_session_id_of_a_new_session_keeps_the_default_role():
    profile = dataclasses.replace(PROFILE, default_role="worker")

    launch = build_launch(profile, ["--session-id", SESSION], resume_context(recorded=None)[0], ROLES)

    assert (launch.env["FP_CC_AUTH_SWITCH_ROLE"], "--system-prompt-snapshot" in launch.argv) == ("worker", False)


def test_resume_with_the_recorded_role_keeps_the_snapshot():
    launch = build_launch(PROFILE, ["--agent-role", "worker", "-r", SESSION], resume_context(recorded={"role": "worker"})[0], ROLES)

    assert ("--system-prompt-snapshot" in launch.argv, launch.notes) == (False, ())


@pytest.mark.parametrize("agent_role, recorded, label", [
    ("dispatcher", {"role": "worker"}, "which ran as role 'worker', with role 'dispatcher'"),
    ("none", {"role": "worker"}, "which ran as role 'worker', with no role"),
    ("worker", None, "which ran as no role, with role 'worker'"),
])
def test_resume_with_another_role_turns_the_snapshot_off(agent_role, recorded, label):
    launch = build_launch(PROFILE, ["--agent-role", agent_role, "--resume", SESSION], resume_context(recorded=recorded)[0], ROLES)

    assert launch.argv[launch.argv.index("--system-prompt-snapshot"):][:4] == ["--system-prompt-snapshot", "off", "--resume", SESSION]
    assert launch.env.get("FP_CC_AUTH_SWITCH_ROLE") == (None if agent_role == "none" else agent_role)
    assert launch.notes == (f"resuming session {SESSION}, {label}: --system-prompt-snapshot off sends the new system prompt, at the cost of prompt cache reuse for this run",)


def test_a_users_own_snapshot_flag_wins():
    launch = build_launch(PROFILE, ["--agent-role", "worker", "-r", SESSION, "--system-prompt-snapshot=on"], resume_context(recorded={"role": "dispatcher"})[0], ROLES)

    assert launch.argv.count("off") == 0 and launch.notes == ()


def test_resume_of_a_role_the_config_no_longer_defines_warns_and_resumes_without_a_role():
    launch = build_launch(PROFILE, ["--resume", SESSION], resume_context(recorded={"role": "retired"})[0], ROLES)

    assert "FP_CC_AUTH_SWITCH_ROLE" not in launch.env
    assert launch.notes == (f"warning: session {SESSION} ran as role 'retired', which the config no longer defines; resuming it without a role",)


def test_continue_gets_no_role_and_a_notice_when_the_newest_session_had_one():
    profile = dataclasses.replace(PROFILE, default_role="worker")

    launch = build_launch(profile, ["-c"], resume_context(newest={"role": "dispatcher"})[0], ROLES)
    quiet = build_launch(profile, ["--continue"], resume_context(newest={})[0], ROLES)

    assert "FP_CC_AUTH_SWITCH_ROLE" not in launch.env and "FP_CC_AUTH_SWITCH_ROLE" not in quiet.env
    assert launch.notes == ("--continue starts without a role; the newest session here ran as role 'dispatcher', add --agent-role dispatcher to keep it",)
    assert quiet.notes == ()


def test_continue_with_a_role_compares_with_the_newest_session():
    same = build_launch(PROFILE, ["--agent-role", "worker", "-c"], resume_context(newest={"role": "worker"})[0], ROLES)
    other = build_launch(PROFILE, ["--agent-role", "worker", "-c"], resume_context(newest=None)[0], ROLES)

    assert "--system-prompt-snapshot" not in same.argv
    assert other.notes == ("resuming the newest session here, which ran as no role, with role 'worker': --system-prompt-snapshot off sends the new system prompt, at the cost of prompt cache reuse for this run",)


@pytest.mark.parametrize("args", [["--resume"], ["-r", "-p", "x"], ["--resume", "my session name"], ["--from-pr", "12"]])
def test_picker_gets_no_automatic_role_and_no_notice_but_an_explicit_role_turns_the_snapshot_off(args):
    ctx, calls = resume_context(recorded={"role": "worker"}, newest={"role": "worker"})
    profile = dataclasses.replace(PROFILE, default_role="worker")

    plain = build_launch(profile, args, ctx, ROLES)
    explicit = build_launch(profile, ["--agent-role", "worker", *args], ctx, ROLES)

    assert ("FP_CC_AUTH_SWITCH_ROLE" not in plain.env, plain.notes, calls) == (True, (), [])
    assert explicit.env["FP_CC_AUTH_SWITCH_ROLE"] == "worker" and "--system-prompt-snapshot" in explicit.argv
    assert explicit.notes[0].startswith("resuming the picked session with role 'worker':")


def test_resume_target_ignores_arguments_after_double_dash():
    assert launcher.resume_target(["--", "--resume", SESSION]) is None
    assert launcher.resume_target(["--session-id", SESSION, "--resume", "abc"]) == ("picker", None)


# Start log lookup


def write_log(path: Path, *lines: str) -> None:
    path.parent.mkdir(parents=True)
    path.write_text("".join(f"{line}\n" for line in lines))


def test_recorded_start_reads_the_newest_valid_line_from_any_project(tmp_path):
    log = tmp_path / "projects/-elsewhere" / SESSION / "auth-switch-profile-starts.jsonl"
    write_log(log, json.dumps({"v": 1, "role": "worker"}), json.dumps({"v": 1, "role": "dispatcher"}), "{broken")

    assert launcher.recorded_start_of(tmp_path, SESSION, "/work")["role"] == "dispatcher"
    assert launcher.recorded_start_of(tmp_path, "1b6f2c1e-1111-4222-8333-444455556666", "/work") is None


def test_newest_start_follows_claudes_project_dir_naming(tmp_path):
    cwd = "/home/u/devel/my_repo.git"
    project = tmp_path / "projects" / "-home-u-devel-my-repo-git"
    project.mkdir(parents=True)
    old, new = "1" * 8 + SESSION[8:], SESSION
    for index, session in enumerate((old, new)):
        (project / f"{session}.jsonl").write_text("{}\n")
        os.utime(project / f"{session}.jsonl", (1000 + index, 1000 + index))
    write_log(project / old / "auth-switch-profile-starts.jsonl", json.dumps({"role": "worker"}))

    assert launcher.newest_start_in(tmp_path, cwd) == {}
    write_log(project / new / "auth-switch-profile-starts.jsonl", json.dumps({"role": "dispatcher"}))
    assert launcher.newest_start_in(tmp_path, cwd)["role"] == "dispatcher"
    assert launcher.newest_start_in(tmp_path, "/nowhere") is None


def test_long_project_dir_names_match_by_prefix(tmp_path):
    cwd = "/" + "a" * 250
    project = tmp_path / "projects" / ("-" + "a" * 199 + "-k3j2h1")
    write_log(project / SESSION / "auth-switch-profile-starts.jsonl", json.dumps({"role": "worker"}))
    (project / f"{SESSION}.jsonl").write_text("{}\n")

    assert launcher.newest_start_in(tmp_path, cwd) == {"role": "worker"}


def test_launcher_resume_reapplies_and_switches_the_role(role_launch, sandbox):
    run, config, prompt = role_launch
    config.write_text(config.read_text().replace("    default_role: dispatcher\n", "") + "  worker: {}\n")
    log = sandbox.home / "profiles/work/projects/-elsewhere" / SESSION / "auth-switch-profile-starts.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"v": 1, "session_id": SESSION, "role": "dispatcher"}) + "\n")

    reapplied = run("--resume", SESSION)
    switched = run("--agent-role", "worker", "--resume", SESSION)

    assert json.loads(reapplied.stdout)["argv"] == ["--append-system-prompt-file", str(prompt), "--resume", SESSION, "--model", "opus"]
    assert reapplied.stderr == ""
    assert json.loads(switched.stdout) == {
        "argv": ["--system-prompt-snapshot", "off", "--resume", SESSION],
        "env": {"FP_CC_AUTH_SWITCH_PROFILE": "work", "FP_CC_AUTH_SWITCH_ROLE": "worker", "FP_CC_AUTH_SWITCH_ROLE_PROMPT": None, "FP_CC_AUTH_SWITCH_ROLE_PROMPT_MODE": None, "ROLE_ONLY": None},
    }
    assert switched.stderr == f"cc-auth-switch start: resuming session {SESSION}, which ran as role 'dispatcher', with role 'worker': --system-prompt-snapshot off sends the new system prompt, at the cost of prompt cache reuse for this run\n"
