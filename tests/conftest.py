import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "git_attribution.py"
INSTALL = REPO_ROOT / "install.py"


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
        self.env = {
            "PATH": os.environ["PATH"],
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

    def run_helper(self, directory: Path) -> HelperResult:
        result = subprocess.run(
            [sys.executable, str(HELPER), str(directory)],
            env=self.env,
            capture_output=True,
            text=True,
        )
        return HelperResult(result.returncode, result.stdout, result.stderr)


@pytest.fixture
def sandbox(tmp_path: Path) -> GitSandbox:
    return GitSandbox(tmp_path)
