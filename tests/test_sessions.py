import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_code_auth_switch import sessions

SETTLE = 0.02


def write(path: Path, content: str, mtime: float | None = None) -> Path:
    path.write_text(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def layout(tmp_path):
    """A profile with a real sessions/ directory and an empty shared target."""
    profile_dir = tmp_path / "profiles" / "work-team"
    live = profile_dir / "sessions"
    live.mkdir(parents=True, mode=0o700)
    target = tmp_path / "home" / ".claude" / "sessions"
    return profile_dir, live, target


@pytest.mark.parametrize("atomic_swap", [True, False], ids=["exchange", "two-renames"])
def test_migration_replaces_the_directory_with_a_symlink_and_keeps_a_backup(layout, monkeypatch, atomic_swap):
    profile_dir, live, target = layout
    if not atomic_swap:
        monkeypatch.setattr(sessions, "exchange", lambda a, b: (_ for _ in ()).throw(sessions.SwapUnsupported("test")))
    write(live / "100.json", '{"pid": 100}', mtime=1_700_000_000)
    write(live / "100.abc.key", "key")

    sessions.ensure_shared("work-team", profile_dir, target, settle=SETTLE)

    assert live.is_symlink() and os.readlink(live) == str(target)
    assert (live / "100.json").read_text() == '{"pid": 100}'
    assert (target / "100.json").stat().st_mtime == 1_700_000_000
    [backup] = profile_dir.glob("sessions.bak-*")
    assert sorted(p.name for p in backup.iterdir()) == ["100.abc.key", "100.json"]
    assert not (profile_dir / "sessions.swap").exists()


def test_migration_keeps_a_newer_copy_in_the_target(layout):
    profile_dir, live, target = layout
    target.mkdir(parents=True)
    write(live / "100.json", "old", mtime=1_700_000_000)
    write(target / "100.json", "new", mtime=1_700_000_100)

    sessions.ensure_shared("work-team", profile_dir, target, settle=SETTLE)

    assert (target / "100.json").read_text() == "new"


def test_rerun_is_a_no_op(layout, capsys):
    profile_dir, live, target = layout
    sessions.ensure_shared("work-team", profile_dir, target, settle=SETTLE)
    capsys.readouterr()

    sessions.ensure_shared("work-team", profile_dir, target, settle=SETTLE)

    assert capsys.readouterr().out == ""
    assert len(list(profile_dir.glob("sessions.bak-*"))) == 1


def test_missing_directory_becomes_a_symlink_without_backup(layout):
    profile_dir, live, target = layout
    live.rmdir()

    sessions.ensure_shared("work-team", profile_dir, target, settle=SETTLE)

    assert live.is_symlink() and target.is_dir()
    assert list(profile_dir.glob("sessions.bak-*")) == []


def test_symlink_elsewhere_is_skipped_with_a_warning(layout, tmp_path, capsys):
    profile_dir, live, target = layout
    live.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    live.symlink_to(elsewhere)

    sessions.ensure_shared("work-team", profile_dir, target, settle=SETTLE)

    assert os.readlink(live) == str(elsewhere)
    assert "not sharing sessions" in capsys.readouterr().out


def test_leftover_swap_symlink_to_the_target_is_removed(layout):
    profile_dir, live, target = layout
    target.mkdir(parents=True)
    (profile_dir / "sessions.swap").symlink_to(target)

    sessions.ensure_shared("work-team", profile_dir, target, settle=SETTLE)

    assert live.is_symlink()
    assert not (profile_dir / "sessions.swap").is_symlink()


def test_leftover_swap_directory_stops_the_migration(layout):
    profile_dir, live, target = layout
    (profile_dir / "sessions.swap").mkdir()

    with pytest.raises(sessions.SessionsError, match="interrupted migration"):
        sessions.ensure_shared("work-team", profile_dir, target, settle=SETTLE)

    assert not live.is_symlink()


def in_place_writer(path: Path, stop_at: float, result: dict) -> None:
    """Rewrite a file in place by path, like Claude Code's fs.writeFile of sessions/<pid>.json."""
    seq = 0
    while time.monotonic() < stop_at:
        seq += 1
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps({"seq": seq}).encode())
        finally:
            os.close(fd)
        result["last"] = seq


@pytest.mark.parametrize("trial", range(20))
def test_migration_loses_no_write_of_an_in_place_writer(tmp_path, trial):
    profile_dir = tmp_path / "work-team"
    live = profile_dir / "sessions"
    live.mkdir(parents=True)
    target = tmp_path / "shared"
    for i in range(30):
        write(live / f"{1000 + i}.json", "{}")
    record = live / "4242.json"
    write(record, json.dumps({"seq": 0}))
    result: dict = {"last": 0}
    # The writer's last write falls somewhere around the swap, then it stays idle like an idle session.
    stop_at = time.monotonic() + random.Random(trial).uniform(0, 0.004)
    writer = threading.Thread(target=in_place_writer, args=(record, stop_at, result))

    writer.start()
    sessions.migrate(profile_dir, target, settle=0.05)
    writer.join()

    assert json.loads(record.read_text()) == {"seq": result["last"]}


# Rollback


@pytest.fixture
def sleeper():
    """Start processes that stand in for live claude sessions, each with its own environment."""
    started: list[subprocess.Popen] = []

    def start(env: dict[str, str]) -> int:
        process = subprocess.Popen(["sleep", "30"], env={"PATH": os.environ["PATH"], **env})
        started.append(process)
        return process.pid

    yield start
    for process in started:
        process.kill()
        process.wait()


def dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def write_start_log(profile_dir: Path, session_id: str, pid: int, config_dir: Path) -> None:
    session_dir = profile_dir / "projects" / "-home-user-devel-service" / session_id
    session_dir.mkdir(parents=True)
    line = {"v": 1, "ts": 1, "session_id": session_id, "pid": pid, "source": "startup", "profile": None, "config_dir": str(config_dir), "cwd": "/"}
    (session_dir / "auth-switch-profile-starts.jsonl").write_text(json.dumps(line) + "\n")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/<pid>/environ")
def test_rollback_moves_back_only_the_records_of_this_profile(tmp_path, sleeper):
    profiles = tmp_path / "profiles"
    mine, other = profiles / "work-team", profiles / "personal"
    target = tmp_path / "home" / ".claude" / "sessions"
    (mine / "sessions").mkdir(parents=True)
    (other / "sessions").mkdir(parents=True)
    (mine / "projects").mkdir()

    # Known by the start log only: the process environment says otherwise and must lose.
    by_log = sleeper({"CLAUDE_CONFIG_DIR": str(other)})
    write_start_log(mine, "sid-log", by_log, mine)
    # Known by the process environment only.
    by_environ = sleeper({"CLAUDE_CONFIG_DIR": str(mine)})
    of_other = sleeper({"CLAUDE_CONFIG_DIR": str(other)})
    of_plain_claude = sleeper({})
    ended = dead_pid()
    for pid, session_id in ((by_log, "sid-log"), (by_environ, "sid-env"), (of_other, "sid-other"), (of_plain_claude, "sid-plain")):
        write(mine / "sessions" / f"{pid}.json", json.dumps({"pid": pid, "sessionId": session_id}))
    write(mine / "sessions" / f"{by_environ}.abc.key", "key")
    # Known by the migration backup only, and no longer running.
    write(mine / "sessions" / f"{ended}.json", json.dumps({"pid": ended, "sessionId": "sid-ended"}))

    sessions.ensure_shared("work-team", mine, target, settle=SETTLE)
    sessions.ensure_shared("personal", other, target, settle=SETTLE)
    owned = sessions.rollback("work-team", mine, target, settle=SETTLE)

    live = mine / "sessions"
    assert live.is_dir() and not live.is_symlink()
    assert sorted(p.name for p in live.iterdir()) == sorted([f"{by_log}.json", f"{by_environ}.json", f"{by_environ}.abc.key"])
    assert sorted(p.name for p in target.iterdir()) == sorted([f"{of_other}.json", f"{of_plain_claude}.json"])
    assert owned.dead == {f"{ended}.json"}
    assert (other / "sessions").is_symlink()


def test_rollback_refuses_a_profile_that_does_not_share(layout):
    profile_dir, live, target = layout

    with pytest.raises(sessions.SessionsError, match="nothing to roll back"):
        sessions.rollback("work-team", profile_dir, target, settle=SETTLE)


# Through the installer


@pytest.fixture
def sharing_config(sandbox):
    source_dir = sandbox.mkdir(".claude")
    (source_dir / "settings.json").write_text("{}\n")
    profiles = sandbox.home / "profiles"
    write(sandbox.mkdir("profiles/work-team/sessions") / "100.json", '{"pid": 100}')

    def config(share: str) -> str:
        return str(sandbox.write(
            "auth-switch.yaml",
            f"source_dir: {source_dir}\n"
            f"bin_dir: {sandbox.home / 'bin'}\n"
            f"profiles_dir: {profiles}\n"
            "profiles:\n"
            "  work-team:\n"
            f"    share_sessions: {share}\n"
            "  personal: {}\n",
        ))

    return config, profiles


def test_install_shares_sessions_only_for_profiles_that_ask(sandbox, sharing_config):
    config, profiles = sharing_config

    result = sandbox.run_install("--config", config("true"))

    assert (profiles / "work-team" / "sessions").is_symlink()
    assert (sandbox.home / ".claude" / "sessions" / "100.json").exists()
    assert not (profiles / "personal" / "sessions").exists()
    assert "cc-auth-switch rollback-sessions work-team" in result.stdout


def test_turning_the_flag_off_warns_and_rollback_undoes_the_sharing(sandbox, sharing_config):
    config, profiles = sharing_config
    sandbox.run_install("--config", config("true"))

    warned = sandbox.run_install("--config", config("false"))
    rolled_back = subprocess.run(
        ["cc-auth-switch", "rollback-sessions", "--config", config("false"), "work-team"],
        env=sandbox.env, capture_output=True, text=True,
    )

    assert "still shared" in warned.stdout
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert not (profiles / "work-team" / "sessions").is_symlink()


def test_install_rejects_a_non_boolean_flag(sandbox, sharing_config):
    config, profiles = sharing_config

    result = sandbox.run_install("--config", config("'yes please'"), check=False)

    assert result.returncode == 1
    assert "share_sessions" in result.stderr
    assert not (profiles / "work-team" / "sessions").is_symlink()
