import json
import os
from pathlib import Path

import pytest


def snapshot(root: Path) -> dict[str, tuple]:
    """Every path below root with its type, content and mtime, so any change shows up."""
    result: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            stat = path.lstat()
            if path.is_symlink():
                result[str(path.relative_to(root))] = ("link", os.readlink(path), stat.st_mtime_ns)
            elif path.is_file():
                result[str(path.relative_to(root))] = ("file", path.read_bytes(), stat.st_mtime_ns, stat.st_mode)
            else:
                result[str(path.relative_to(root))] = ("dir", stat.st_mode)
    return result


@pytest.fixture
def foreign(sandbox):
    """A directory under profiles_dir that the config does not list, next to a managed profile."""
    source_dir = sandbox.mkdir(".claude")
    (source_dir / "settings.json").write_text(json.dumps({"enabledPlugins": {"a@shared": True}, "theme": "dark"}) + "\n")
    (source_dir / "CLAUDE.md").write_text("# Shared\n")
    sandbox.mkdir(".claude/skills")
    (sandbox.home / ".claude.json").write_text(json.dumps({"mcpServers": {"tool": {"command": "tool"}}}) + "\n")
    profiles = sandbox.home / "profiles"
    other = sandbox.mkdir("profiles/other")
    (other / "settings.json").write_text('{"theme": "light"}\n')
    (other / ".claude.json").write_text('{"mcpServers": {}}\n')
    (other / "CLAUDE.md").write_text("# Other\n")
    sandbox.mkdir("profiles/other/sessions")
    (other / "sessions" / "4242.json").write_text('{"pid": 4242}\n')
    config = sandbox.write(
        "auth-switch.yaml",
        f"source_dir: {source_dir}\n"
        f"bin_dir: {sandbox.home / 'bin'}\n"
        f"profiles_dir: {profiles}\n"
        "profiles:\n"
        "  work-team: {}\n",
    )
    return str(config), other


def test_install_and_force_sync_leave_an_unlisted_directory_alone(sandbox, foreign):
    config, other = foreign
    before = snapshot(other)

    sandbox.run_install("--config", config)
    (sandbox.home / "profiles" / "work-team" / "CLAUDE.md").write_text("# Local edit\n")
    sandbox.run_install("--config", config, "--sync", "--force")

    assert snapshot(other) == before
    assert (sandbox.home / "profiles" / "work-team" / ".auth-switch-backups").is_dir()
