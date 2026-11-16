import json
import shlex
import subprocess
import time

import pytest

HOOK = "claude-auth-switch-session-start"
SESSION_ID = "0b6f2c1e-1111-4222-8333-444455556666"


@pytest.fixture
def hook_env(sandbox):
    """A profile directory and a transcript path under its projects/ directory, as Claude Code passes them."""
    profile_dir = sandbox.mkdir("profiles/work-team")
    project_dir = sandbox.mkdir("profiles/work-team/projects/-home-user-devel-service")
    env = {**sandbox.env, "CLAUDE_CONFIG_DIR": str(profile_dir), "FP_CC_AUTH_SWITCH_PROFILE": "work-team"}
    payload = {
        "session_id": SESSION_ID,
        "transcript_path": str(project_dir / f"{SESSION_ID}.jsonl"),
        "cwd": "/home/user/devel/service",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    return env, payload, profile_dir, project_dir / SESSION_ID / "auth-switch-profile-starts.jsonl"


def run_hook(env: dict, stdin: str, argv: list[str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv or [HOOK], input=stdin, env=env, capture_output=True, text=True)


def read_lines(log) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_hook_appends_the_contract_line(hook_env):
    env, payload, profile_dir, log = hook_env
    before = int(time.time() * 1000)

    result = run_hook({**env, "CLAUDE_PID": "4242"}, json.dumps(payload))

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    [line] = read_lines(log)
    assert before <= line.pop("ts") <= int(time.time() * 1000)
    assert line == {
        "v": 1,
        "session_id": SESSION_ID,
        "pid": 4242,
        "source": "startup",
        "profile": "work-team",
        "config_dir": str(profile_dir),
        "cwd": "/home/user/devel/service",
    }


def test_hook_appends_one_line_per_start(hook_env):
    env, payload, profile_dir, log = hook_env

    run_hook({**env, "CLAUDE_PID": "4242"}, json.dumps(payload))
    run_hook({**env, "CLAUDE_PID": "4343"}, json.dumps({**payload, "source": "resume"}))

    assert [(line["pid"], line["source"]) for line in read_lines(log)] == [(4242, "startup"), (4343, "resume")]


def test_hook_finds_the_pid_from_the_parent_process_record(hook_env):
    env, payload, profile_dir, log = hook_env
    sessions = profile_dir / "sessions"
    sessions.mkdir()
    # The trailing `true` keeps bash from exec-ing the hook, so bash stays its parent.
    script = f'touch "{sessions}/$$.json"; echo $$; {HOOK}; true'

    result = run_hook(env, json.dumps(payload), ["bash", "-c", script])

    assert read_lines(log)[0]["pid"] == int(result.stdout)


def test_hook_without_pid_record_or_profile_env_writes_nulls(hook_env, sandbox):
    env, payload, profile_dir, log = hook_env

    run_hook(sandbox.env, json.dumps(payload))

    [line] = read_lines(log)
    assert (line["pid"], line["profile"], line["config_dir"]) == (None, None, None)


def test_hook_ignores_an_empty_transcript_path(hook_env):
    env, payload, profile_dir, log = hook_env

    result = run_hook(env, json.dumps({**payload, "transcript_path": ""}))

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert not log.parent.exists()
    assert not (profile_dir / "auth-switch-session-start.log").exists()


def test_hook_logs_errors_to_the_profile_dir_and_stays_silent(hook_env):
    env, payload, profile_dir, log = hook_env

    result = run_hook(env, "not json")

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert "JSONDecodeError" in (profile_dir / "auth-switch-session-start.log").read_text()


# Registration in the profiles' settings.json


FOREIGN_HOOK = {"matcher": "", "hooks": [{"type": "command", "command": "bash ~/.claude/scripts/agent-role-inject.sh"}]}


@pytest.fixture
def install_config(sandbox):
    source_dir = sandbox.mkdir(".claude")
    (source_dir / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [FOREIGN_HOOK]}, "theme": "dark"}, indent=2) + "\n")
    config = sandbox.write(
        "auth-switch.yaml",
        f"source_dir: {source_dir}\n"
        f"bin_dir: {sandbox.home / 'bin'}\n"
        f"profiles_dir: {sandbox.home / 'profiles'}\n"
        "profiles:\n"
        "  personal: {}\n"
        "  work-team:\n"
        "    attribution:\n"
        "      pr: from profile\n",
    )
    return config, source_dir


def our_group(sandbox) -> dict:
    return {"matcher": "", "hooks": [{"type": "command", "command": shlex.quote(str(sandbox.tool_bin / HOOK)), "timeout": 10}]}


def settings(sandbox, profile: str) -> dict:
    return json.loads((sandbox.home / "profiles" / profile / "settings.json").read_text())


def test_install_registers_the_hook_and_keeps_foreign_hooks(sandbox, install_config):
    config, source_dir = install_config

    sandbox.run_install("--config", str(config))

    for profile in ("personal", "work-team"):
        assert settings(sandbox, profile)["hooks"]["SessionStart"] == [FOREIGN_HOOK, our_group(sandbox)]
    assert "agent-role-inject" in (source_dir / "settings.json").read_text()
    assert HOOK not in (source_dir / "settings.json").read_text()


def test_install_is_idempotent_and_replaces_an_old_hook_path(sandbox, install_config):
    config, source_dir = install_config
    sandbox.run_install("--config", str(config))
    settings_path = sandbox.home / "profiles" / "personal" / "settings.json"
    data = json.loads(settings_path.read_text())
    data["hooks"]["SessionStart"].insert(0, {"matcher": "startup", "hooks": [{"type": "command", "command": f"/old/bin/{HOOK}"}]})
    settings_path.write_text(json.dumps(data, indent=2) + "\n")

    sandbox.run_install("--config", str(config))
    first = settings_path.read_text()
    result = sandbox.run_install("--config", str(config))

    assert settings(sandbox, "personal")["hooks"]["SessionStart"] == [FOREIGN_HOOK, our_group(sandbox)]
    assert settings_path.read_text() == first
    assert "settings.json" not in result.stdout


def test_install_creates_settings_json_when_the_source_has_none(sandbox, install_config):
    config, source_dir = install_config
    (source_dir / "settings.json").unlink()

    sandbox.run_install("--config", str(config))

    assert settings(sandbox, "personal") == {"hooks": {"SessionStart": [our_group(sandbox)]}}


def test_sync_does_not_report_the_hook_as_divergence(sandbox, install_config):
    config, source_dir = install_config
    sandbox.run_install("--config", str(config))

    result = sandbox.run_install("--config", str(config), "--sync")

    assert "Diverged" not in result.stdout
    assert "Done." in result.stdout
    assert settings(sandbox, "work-team")["hooks"]["SessionStart"] == [FOREIGN_HOOK, our_group(sandbox)]


def test_sync_still_reports_a_real_divergence(sandbox, install_config):
    config, source_dir = install_config
    sandbox.run_install("--config", str(config))
    settings_path = sandbox.home / "profiles" / "personal" / "settings.json"
    settings_path.write_text(settings_path.read_text().replace('"theme": "dark"', '"theme": "light"'))

    result = sandbox.run_install("--config", str(config), "--sync")

    assert "Profile 'personal' — settings.json" in result.stdout
    assert "Profile 'work-team'" not in result.stdout
