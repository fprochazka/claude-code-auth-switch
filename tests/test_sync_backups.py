import json

import pytest

BACKUPS = ".auth-switch-backups"


@pytest.fixture
def synced(sandbox):
    """Install two profiles from a source directory with every kind of copied file."""
    source_dir = sandbox.mkdir(".claude")
    (source_dir / "settings.json").write_text(json.dumps({"theme": "dark", "plansDirectory": "~/plans"}, indent=2) + "\n")
    (source_dir / "keybindings.json").write_text("[]\n")
    (source_dir / "CLAUDE.md").write_text("# Shared instructions\n")
    sandbox.mkdir(".claude/skills")
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
    sandbox.run_install("--config", str(config))
    return str(config), source_dir, sandbox.home / "profiles"


def detail_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("  ")]


def test_force_sync_backs_up_the_profile_files_it_replaces(sandbox, synced):
    config, source_dir, profiles = synced
    profile = profiles / "work-team"
    (profile / "CLAUDE.md").write_text("# Local edit\n")
    settings = json.loads((profile / "settings.json").read_text())
    (profile / "settings.json").write_text(json.dumps({**settings, "permissions": {"allow": ["Bash(ls)"]}}, indent=2) + "\n")
    edited_settings = (profile / "settings.json").read_text()

    result = sandbox.run_install("--config", config, "--sync", "--force")

    [backup] = (profile / BACKUPS).iterdir()
    assert sorted(p.name for p in backup.iterdir()) == ["CLAUDE.md", "settings.json"]
    assert (backup / "CLAUDE.md").read_text() == "# Local edit\n"
    assert (backup / "settings.json").read_text() == edited_settings
    assert (profile / "CLAUDE.md").read_text() == "# Shared instructions\n"
    assert "permissions" not in json.loads((profile / "settings.json").read_text())
    assert f"  backed up CLAUDE.md to {backup / 'CLAUDE.md'}" in result.stdout
    assert f"  backed up settings.json to {backup / 'settings.json'}" in result.stdout
    assert not (profiles / "personal" / BACKUPS).exists()
    assert sorted(p.name for p in profile.glob("*.md")) == ["CLAUDE.md"]


def test_force_sync_without_changes_is_quiet_and_backs_up_nothing(sandbox, synced):
    config, source_dir, profiles = synced
    sandbox.run_install("--config", config, "--sync", "--force")

    result = sandbox.run_install("--config", config, "--sync", "--force")

    assert detail_lines(result.stdout) == []
    assert not list(profiles.glob(f"*/{BACKUPS}"))


def test_force_sync_keeps_the_newest_backups(sandbox, synced):
    config, source_dir, profiles = synced
    backups = profiles / "personal" / BACKUPS
    for day in range(1, 26):
        (backups / f"202601{day:02d}T000000Z").mkdir(parents=True)
    (profiles / "personal" / "keybindings.json").write_text("[1]\n")

    sandbox.run_install("--config", config, "--sync", "--force")

    kept = sorted(p.name for p in backups.iterdir())
    assert len(kept) == 20
    assert kept[0] == "20260107T000000Z"
    assert (backups / kept[-1] / "keybindings.json").read_text() == "[1]\n"
