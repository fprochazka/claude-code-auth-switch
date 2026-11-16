import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Console scripts of the package. The sandbox reaches them through shims in its own bin directory,
# so a test can delete or replace one without touching the project environment.
TOOL_COMMANDS = ("claude-code-auth-switch", "claude-auth-switch-attribution")


@dataclass
class HelperResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def settings(self) -> dict | None:
        return json.loads(self.stdout) if self.stdout.strip() else None


class GitSandbox:
    """A fake home directory with its own global git config, isolated from the real user's config."""

    def __init__(self, root: Path):
        self.home = root / "home"
        self.home.mkdir()
        self.global_config = self.home / ".gitconfig"
        self.global_config.write_text("")
        self.tool_bin = self.home / "tool-bin"
        self.tool_bin.mkdir()
        for name in TOOL_COMMANDS:
            real = shutil.which(name)
            assert real is not None, f"{name} is not on PATH, run the tests with `uv run pytest`"
            shim = self.tool_bin / name
            shim.write_text(f'#!/bin/sh\nexec "{real}" "$@"\n')
            shim.chmod(0o755)
        self.env = {
            "PATH": f"{self.tool_bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "GIT_CONFIG_GLOBAL": str(self.global_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "LANG": "C.UTF-8",
        }

    def write(self, relative: str, content: str) -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def mkdir(self, relative: str) -> Path:
        path = self.home / relative
        path.mkdir(parents=True, exist_ok=True)
        return path

    def git_init(self, relative: str) -> Path:
        path = self.mkdir(relative)
        subprocess.run(["git", "init", "-q", str(path)], env=self.env, check=True)
        return path

    def run_install(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["claude-code-auth-switch", "install", *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
        )

    def run_helper(self, directory: Path) -> HelperResult:
        result = subprocess.run(
            ["claude-auth-switch-attribution", str(directory)],
            env=self.env,
            capture_output=True,
            text=True,
        )
        return HelperResult(result.returncode, result.stdout, result.stderr)


@pytest.fixture
def sandbox(tmp_path: Path) -> GitSandbox:
    return GitSandbox(tmp_path)
