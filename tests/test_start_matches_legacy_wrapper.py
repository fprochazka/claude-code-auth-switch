"""`cc-auth-switch start` must launch Claude exactly as the bash wrappers of earlier versions did.

The test installs the launchers, writes the legacy wrappers for the same config next to them, runs both
with a fake `claude` that prints its arguments and environment, and compares the output.
"""

import json
import os
import subprocess
from unittest import mock

import pytest

from claude_code_auth_switch.config import load_config, load_secrets, merge_secret_env, pinned_models_to_env
from legacy_wrapper import generate_wrapper_script

FAKE_CLAUDE = """\
#!/usr/bin/env python3
import json, os, sys
print(json.dumps({"argv": sys.argv[1:], "env": dict(sorted(os.environ.items()))}))
"""

FAKE_TMUX = """\
#!/bin/sh
[ -n "${FAKE_TMUX_FAIL:-}" ] && exit 1
[ "$*" = "display-message -p -t $TMUX_PANE #S" ] || exit 2
printf '%s\\n' "$FAKE_TMUX_SESSION"
"""

PROFILES = """\
profiles:
  plain:
    description: no model
  modeled:
    description: model and add_dirs
    model: sonnet
    add_dirs:
      - ~/devel/projects/company
      - ~/other dir
  vertex:
    description: env, pinned models and secrets
    pinned_models:
      sonnet: claude-sonnet-4-5-20250929
      opus: claude-opus-4-8
    env:
      CLAUDE_CODE_USE_VERTEX: "1"
      ANTHROPIC_VERTEX_PROJECT_ID: my-project
      ANTHROPIC_DEFAULT_OPUS_MODEL: claude-opus-override
      CLOUD_ML_REGION: europe-west1
      MAX_THINKING_TOKENS: 1024
  attributed:
    description: profile attribution and a model
    model: opus
    attribution:
      commit: profile commit
"""

# A user's --settings is left out: start merges it with the attribution, where the legacy wrapper dropped the attribution.
ARGUMENTS = [
    [], ["-p", "hello"], ["--model", "opus"], ["--model=opus", "-p", "x"],
    ["--resume"], ["-r", "abc"], ["--name=mine"], ["-c"], ["mcp", "list"], ["--add-dir", "/x", "-p", "two words"], ["-p", "--model"],
]

# Inherited variables that both must drop, replace, or pass through unchanged.
INHERITED = {"CLAUDE_CODE_ENTRYPOINT": "cli", "ANTHROPIC_API_KEY": "leak", "CLAUDE_CONFIG_DIR": "/wrong", "FP_CC_AUTH_SWITCH_PROFILE": "wrong",
             "_": "/usr/bin/env", "OLDPWD": "/somewhere", "SHLVL": "3", "TERM": "xterm-256color", "UNRELATED_TOKEN": "a b $c `d`"}


@pytest.fixture(scope="module")
def launchers(tmp_path_factory):
    from conftest import GitSandbox

    sandbox = GitSandbox(tmp_path_factory.mktemp("legacy"))
    source_dir = sandbox.mkdir(".claude")
    (source_dir / "settings.json").write_text("{}\n")
    bin_dir, legacy_bin = sandbox.home / "bin", sandbox.mkdir("legacy-bin")
    profiles_dir = sandbox.home / "profiles"
    config_path = sandbox.write("auth-switch.yaml", f"source_dir: {source_dir}\nbin_dir: {bin_dir}\nprofiles_dir: {profiles_dir}\n{PROFILES}")
    sandbox.write("auth-switch.secrets.yaml", 'profiles:\n  vertex:\n    env:\n      OTEL_EXPORTER_OTLP_HEADERS: "dd-api-key=secret value"\n')
    sandbox.run_install("--config", str(config_path))

    # The legacy installer's computation of a wrapper's inputs.
    config = load_config(config_path)
    secrets = load_secrets(sandbox.home / "auth-switch.secrets.yaml", config)
    for name, profile in config["profiles"].items():
        env = merge_secret_env(name, profile.get("env", {}) or {}, secrets.get(name, {}))
        env = {**pinned_models_to_env(name, profile.get("pinned_models", {}) or {}), **env}
        script = legacy_bin / f"claude-{name}"
        # The legacy installer expanded ~ in add_dirs with the HOME it ran under.
        with mock.patch.dict(os.environ, {"HOME": str(sandbox.home)}):
            script.write_text(generate_wrapper_script(
                name, profiles_dir / name, profile.get("description", ""), env, profile.get("add_dirs", []) or [],
                profile.get("model") or None, sandbox.tool_bin / "cc-auth-switch",
            ))
        script.chmod(0o755)

    fake_bin = sandbox.mkdir("fake-bin")
    for name, content in (("claude", FAKE_CLAUDE), ("tmux", FAKE_TMUX)):
        (fake_bin / name).write_text(content)
        (fake_bin / name).chmod(0o755)
    sandbox.write(".gitconfig", '[includeIf "gitdir:~/devel/projects/company/"]\n    path = ~/devel/projects/company/.gitconfig\n')
    sandbox.write("devel/projects/company/.gitconfig", '[claude-code "attribution"]\n    commit = "Line one\\n\\nCo-Authored-By: Claude"\n    sessionUrl = false\n')
    places = {
        "company repository in tmux": (sandbox.git_init("devel/projects/company/service"), {"TMUX": "/tmp/tmux-1000/default,1,0", "TMUX_PANE": "%12", "FAKE_TMUX_SESSION": "owner/repo"}),
        "personal repository": (sandbox.git_init("devel/personal/project"), {}),
        "company directory, tmux failing": (sandbox.home / "devel/projects/company", {"TMUX": "/tmp/x,1,0", "TMUX_PANE": "%3", "FAKE_TMUX_FAIL": "1"}),
    }
    env = {**sandbox.env, **INHERITED, "PATH": f"{fake_bin}{os.pathsep}{sandbox.env['PATH']}"}
    return bin_dir, legacy_bin, env, places


def launch(bin_dir, profile, args, cwd, env) -> dict:
    result = subprocess.run([str(bin_dir / f"claude-{profile}"), *args], cwd=cwd, env=env, capture_output=True, text=True)
    return {"returncode": result.returncode, "stderr": result.stderr, "stdout": json.loads(result.stdout)}


@pytest.mark.parametrize("profile", ["plain", "modeled", "vertex", "attributed"])
@pytest.mark.parametrize("place", ["company repository in tmux", "personal repository", "company directory, tmux failing"])
def test_start_launches_claude_like_the_legacy_wrapper(launchers, profile, place):
    bin_dir, legacy_bin, env, places = launchers
    cwd, place_env = places[place]
    for args in ARGUMENTS:
        launched = launch(bin_dir, profile, args, cwd, {**env, **place_env})
        legacy = launch(legacy_bin, profile, args, cwd, {**env, **place_env})
        # Every variable must match, apart from `_`: bash sets it to the last argument of the previous
        # command, and the legacy wrapper ran many commands before its exec.
        new_env, legacy_env = launched["stdout"].pop("env"), legacy["stdout"].pop("env")
        new_env.pop("_", None), legacy_env.pop("_", None)
        assert launched == legacy, args
        assert {key: (new_env.get(key), legacy_env.get(key)) for key in new_env.keys() | legacy_env.keys() if new_env.get(key) != legacy_env.get(key)} == {}, args
        assert new_env["FP_CC_AUTH_SWITCH_PROFILE"] == profile
