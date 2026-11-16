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

    assert detail_lines(result.stdout) == ["  in sync, nothing changed", "  in sync, nothing changed", "  nothing to clean up"]
    assert "Backups of replaced files" not in result.stdout
    assert not list(profiles.glob(f"*/{BACKUPS}"))


@pytest.mark.parametrize("flags", [["--sync"], ["--sync", "--force"]])
def test_every_sync_that_replaces_a_file_keeps_its_own_backup(sandbox, synced, flags):
    config, source_dir, profiles = synced
    lines = [f"line {n}\n" for n in range(25)]
    (source_dir / "keybindings.json").write_text("".join(lines))
    (profiles / "work-team" / "keybindings.json").write_text("".join(lines))
    keybindings = profiles / "personal" / "keybindings.json"

    for run in range(25):
        # Only a removed line, so plain --sync replaces the file without reporting a divergence.
        keybindings.write_text("".join(lines[:run] + lines[run + 1:]))
        result = sandbox.run_install("--config", config, *flags)
        assert "never deletes them; remove them with rm" in result.stdout.splitlines()[-1]

    backups = sorted((profiles / "personal" / BACKUPS).iterdir())
    assert len(backups) == 25
    assert sorted(next(iter(set(lines) - set((b / "keybindings.json").read_text().splitlines(keepends=True)))) for b in backups) == sorted(lines)


def test_plain_install_backs_up_the_files_it_patches(sandbox, synced):
    config, source_dir, profiles = synced
    profile = profiles / "work-team"
    (profile / ".claude.json").write_text('{"numStartups": 3}\n')
    old_settings = (profile / "settings.json").read_text()
    (sandbox.home / ".claude.json").write_text(json.dumps({"mcpServers": {"tool": {"command": "tool"}}}) + "\n")
    sandbox.write("auth-switch.yaml", (sandbox.home / "auth-switch.yaml").read_text().replace("from profile", "changed"))

    result = sandbox.run_install("--config", config)

    [backup] = (profile / BACKUPS).iterdir()
    assert (backup / "settings.json").read_text() == old_settings
    assert (backup / ".claude.json").read_text() == '{"numStartups": 3}\n'
    assert "never deletes them; remove them with rm" in result.stdout.splitlines()[-1]
    assert not (profiles / "personal" / BACKUPS).exists()


def test_first_install_backs_up_nothing(sandbox, synced):
    config, source_dir, profiles = synced

    assert not list(profiles.glob(f"*/{BACKUPS}"))


@pytest.mark.parametrize("flags", [[], ["--sync"], ["--sync", "--force"]])
def test_a_run_without_changes_says_so_per_section(sandbox, synced, flags):
    config, source_dir, profiles = synced

    result = sandbox.run_install("--config", config, *flags)

    assert result.stdout == (
        "\nProfile: personal\n  in sync, nothing changed\n"
        "\nProfile: work-team\n  in sync, nothing changed\n"
        "\nCleanup:\n  nothing to clean up\n"
        "\nDone.\n"
    )


def test_a_profile_with_changes_gets_no_in_sync_line(sandbox, synced):
    config, source_dir, profiles = synced
    (profiles / "personal" / "CLAUDE.md").unlink()

    result = sandbox.run_install("--config", config)

    assert "\nProfile: personal\n  copied CLAUDE.md\n\nProfile: work-team\n  in sync, nothing changed\n" in result.stdout
