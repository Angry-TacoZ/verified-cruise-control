"""Unit tests for real-world intake/validation (PR #5).

All tests are offline and use synthetic data in tmp_path only.
No provider calls, no credentials, no real photographs.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Private intake paths that must never be committed. The register and
# every associated private evidence directory live under these.
MUST_BE_IGNORED = [
    "evals/realworld/private/",
    "evals/realworld/private/register.json",
    "evals/realworld/private/holdout-freeze.json",
    "evals/realworld/photos/some-photo.jpg",
    "evals/realworld/manifest.pilot.json",
]


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_private_intake_paths_are_gitignored():
    """Every private intake path must be ignored by git.

    Fails if any listed path is not ignored, so a missing .gitignore
    rule is caught before private material can be staged.
    """
    missing = []
    for rel in MUST_BE_IGNORED:
        result = _git("check-ignore", "-q", rel)
        if result.returncode != 0:
            missing.append(rel)
    assert not missing, f"not gitignored (would be committable): {missing}"


def test_no_tracked_private_files():
    """No private evidence may already be tracked.

    .gitignore does not protect tracked files, so the private
    register directory and the photos overlay must hold no tracked
    files other than committed placeholder .gitignore files.
    """
    result = _git("ls-files", "evals/realworld/private", "evals/realworld/photos")
    assert result.returncode == 0, result.stderr
    tracked = [
        line
        for line in result.stdout.splitlines()
        if line and not line.endswith(".gitignore")
    ]
    assert tracked == [], f"tracked private files must be removed: {tracked}"
