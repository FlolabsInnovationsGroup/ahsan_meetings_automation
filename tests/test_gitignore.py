"""Credentials and server data must never be committable. A merge once
dropped these .gitignore rules silently; this keeps them from going again."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

MUST_BE_IGNORED = [
    ".env",
    ".env.bak",
    "secrets/highlight-cutter.env",
    "secrets/highlight-cutter.pem",
    "any-key.pem",
    "deploy/caddy.env",
    "data/youtube_token.json",
    "auto-update.log",
]


@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_secret_paths_are_ignored(path):
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False)
    assert result.returncode == 0, f"{path} is not in .gitignore"


def test_env_example_stays_tracked():
    result = subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT, check=False)
    assert result.returncode == 1
