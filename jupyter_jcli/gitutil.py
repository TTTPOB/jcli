"""Shared git repository helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git_root(cwd: Path | None = None) -> Path | None:
    """Return the repository root containing *cwd*.

    Returns None when git is missing from PATH, *cwd* is not inside a
    repository, or git fails for any other reason.  The caller decides how
    to report the failure.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(cwd if cwd is not None else Path.cwd()),
        )
    except (OSError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip())
