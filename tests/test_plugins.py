import json
from typing import Any

import pytest

MARKETPLACE = {"source": {"source": "github", "repo": "example/plugins"}}


@pytest.fixture
def plugin_setup(sandbox):
    source_dir = sandbox.mkdir(".claude")
    profiles = sandbox.home / "profiles"

    def write_source(enabled: dict[str, Any] | None, marketplaces: dict[str, Any] | None = None) -> None:
        data: dict[str, Any] = {"theme": "dark"}
        if enabled is not None:
            data["enabledPlugins"] = enabled
        if marketplaces is not None:
            data["extraKnownMarketplaces"] = marketplaces
        (source_dir / "settings.json").write_text(json.dumps(data, indent=2) + "\n")

    def write_config(work_team_plugins: str = "") -> str:
        return str(
            sandbox.write(
                "auth-switch.yaml",
                f"source_dir: {source_dir}\n"
                f"bin_dir: {sandbox.home / 'bin'}\n"
                f"profiles_dir: {profiles}\n"
                "profiles:\n"
                "  personal: {}\n"
                "  work-team:\n"
                "    description: team\n" + work_team_plugins,
            )
        )

    def settings(profile: str) -> dict[str, Any]:
        return json.loads((profiles / profile / "settings.json").read_text())

    return write_source, write_config, settings, profiles


def test_plain_install_copies_the_plugin_keys_into_existing_profiles(sandbox, plugin_setup):
    write_source, write_config, settings, profiles = plugin_setup
    write_source({"a@shared": True})
    config = write_config()
    sandbox.run_install("--config", config)
    write_source({"a@shared": True, "b@shared": True}, {"shared": MARKETPLACE})

    sandbox.run_install("--config", config)

    for profile in ("personal", "work-team"):
        assert settings(profile)["enabledPlugins"] == {"a@shared": True, "b@shared": True}
        assert settings(profile)["extraKnownMarketplaces"] == {"shared": MARKETPLACE}


def test_profile_overrides_turn_plugins_on_and_off(sandbox, plugin_setup):
    write_source, write_config, settings, profiles = plugin_setup
    write_source({"a@shared": True, "b@shared": True}, {"shared": MARKETPLACE})
    config = write_config("    plugins:\n      a@shared: false\n      c@shared: true\n")

    sandbox.run_install("--config", config)

    assert settings("work-team")["enabledPlugins"] == {"a@shared": False, "b@shared": True, "c@shared": True}
    assert settings("personal")["enabledPlugins"] == {"a@shared": True, "b@shared": True}


def test_plugins_dropped_from_the_source_are_removed_from_profiles(sandbox, plugin_setup):
    write_source, write_config, settings, profiles = plugin_setup
    write_source({"a@shared": True, "b@shared": True}, {"shared": MARKETPLACE})
    config = write_config()
    sandbox.run_install("--config", config)
    profile_settings = profiles / "personal" / "settings.json"
    profile_settings.write_text(
        json.dumps({**settings("personal"), "enabledPlugins": {"a@shared": True, "local@shared": True}})
    )
    write_source({"a@shared": True})

    sandbox.run_install("--config", config)

    assert settings("personal")["enabledPlugins"] == {"a@shared": True}
    assert "extraKnownMarketplaces" not in settings("personal")
    assert settings("personal")["theme"] == "dark"


@pytest.mark.parametrize(
    "plugins, message",
    [
        ("    plugins:\n      no-marketplace: true\n", 'expected "<plugin>@<marketplace>"'),
        ("    plugins:\n      a@b@c: true\n", 'expected "<plugin>@<marketplace>"'),
        ("    plugins:\n      '@shared': true\n", 'expected "<plugin>@<marketplace>"'),
        ("    plugins:\n      a@shared: 'yes'\n", "expected true or false"),
        ("    plugins:\n      - a@shared\n", "expected a map"),
    ],
)
def test_invalid_overrides_stop_the_install(sandbox, plugin_setup, plugins, message):
    write_source, write_config, settings, profiles = plugin_setup
    write_source({})

    result = sandbox.run_install("--config", write_config(plugins), check=False)

    assert result.returncode == 1
    assert message in result.stderr
    assert not profiles.exists()


def test_enabling_a_plugin_of_an_unknown_marketplace_warns(sandbox, plugin_setup):
    write_source, write_config, settings, profiles = plugin_setup
    write_source({}, {"declared": MARKETPLACE})
    sandbox.write(".claude/plugins/known_marketplaces.json", json.dumps({"installed": {}}))
    plugins = (
        "    plugins:\n      a@declared: true\n      b@installed: true\n      c@unknown: true\n      d@other: false\n"
    )

    result = sandbox.run_install("--config", write_config(plugins))

    assert "enables c@unknown" in result.stderr
    assert "a@declared" not in result.stderr and "b@installed" not in result.stderr and "d@other" not in result.stderr
    assert settings("work-team")["enabledPlugins"]["c@unknown"] is True


def test_sync_does_not_report_synced_plugin_keys_as_divergence(sandbox, plugin_setup):
    write_source, write_config, settings, profiles = plugin_setup
    write_source({"a@shared": True}, {"shared": MARKETPLACE})
    config = write_config("    plugins:\n      a@shared: false\n")
    sandbox.run_install("--config", config)
    write_source({"a@shared": True, "b@shared": True}, {"shared": MARKETPLACE})
    sandbox.run_install("--config", config)

    result = sandbox.run_install("--config", config, "--sync")

    assert "Diverged" not in result.stdout
    assert [line for line in result.stdout.splitlines() if line.startswith("  ")] == [
        "  in sync, nothing changed",
        "  in sync, nothing changed",
        "  nothing to clean up",
    ]
