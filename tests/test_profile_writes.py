import json
import os
import stat

import pytest

from claude_code_auth_switch import installer
from claude_code_auth_switch.installer import ProfileBackup, write_profile_file


def leftovers(directory) -> list[str]:
    return [p.name for p in directory.rglob(f"*{installer.TEMP_MARKER}*")]


def test_identical_content_changes_nothing(tmp_path):
    target = tmp_path / "settings.json"
    target.write_bytes(b"{}\n")
    before = target.stat()
    backup = ProfileBackup(tmp_path, "20260101T000000Z")

    assert write_profile_file(target, b"{}\n", backup) is False

    assert target.stat().st_ino == before.st_ino and target.stat().st_mtime_ns == before.st_mtime_ns
    assert backup.directory is None and not (tmp_path / installer.BACKUPS_DIR_NAME).exists()
    assert leftovers(tmp_path) == []


def test_changed_content_is_backed_up_and_replaced_keeping_the_mode(tmp_path):
    target = tmp_path / ".claude.json"
    target.write_bytes(b'{"old": true}\n')
    target.chmod(0o600)
    backup = ProfileBackup(tmp_path, "20260101T000000Z")

    assert write_profile_file(target, b'{"new": true}\n', backup) is True

    assert target.read_bytes() == b'{"new": true}\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert (backup.directory / ".claude.json").read_bytes() == b'{"old": true}\n'
    assert leftovers(tmp_path) == []


def test_new_file_gets_the_requested_mode_and_no_backup(tmp_path):
    target = tmp_path / "CLAUDE.md"
    backup = ProfileBackup(tmp_path, "20260101T000000Z")

    assert write_profile_file(target, b"# New\n", backup, new_mode=0o640) is True

    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert backup.directory is None


@pytest.mark.parametrize("failing", ["fsync", "replace"])
def test_failure_mid_write_leaves_the_target_and_no_temp(tmp_path, monkeypatch, failing):
    target = tmp_path / "settings.json"
    target.write_bytes(b"{}\n")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(installer.os, failing, boom)
    with pytest.raises(OSError, match="disk full"):
        write_profile_file(target, b'{"new": true}\n', None)

    assert target.read_bytes() == b"{}\n"
    assert leftovers(tmp_path) == []


def test_repeated_installs_leave_no_backups_and_no_temp_files(sandbox):
    source_dir = sandbox.mkdir(".claude")
    (source_dir / "settings.json").write_text(json.dumps({"enabledPlugins": {"a@shared": True}}) + "\n")
    (source_dir / "CLAUDE.md").write_text("# Shared\n")
    (sandbox.home / ".claude.json").write_text(json.dumps({"mcpServers": {"tool": {"command": "tool"}}}) + "\n")
    profiles = sandbox.home / "profiles"
    config = sandbox.write(
        "auth-switch.yaml",
        f"source_dir: {source_dir}\nbin_dir: {sandbox.home / 'bin'}\nprofiles_dir: {profiles}\n"
        "profiles:\n  work-team:\n    attribution:\n      pr: team\n",
    )
    sandbox.run_install("--config", str(config))
    claude_json = profiles / "work-team" / ".claude.json"
    claude_json.write_text('{"numStartups": 1}\n')
    claude_json.chmod(0o600)
    sandbox.run_install("--config", str(config))
    assert stat.S_IMODE(claude_json.stat().st_mode) == 0o600
    [first_backup] = (profiles / "work-team" / installer.BACKUPS_DIR_NAME).iterdir()

    for flags in ([], ["--sync"], ["--sync", "--force"]):
        result = sandbox.run_install("--config", str(config), *flags)
        assert [line for line in result.stdout.splitlines() if line.startswith("  ")] == ["  in sync, nothing changed", "  nothing to clean up"]

    assert list((profiles / "work-team" / installer.BACKUPS_DIR_NAME).iterdir()) == [first_backup]
    assert leftovers(profiles) == []
