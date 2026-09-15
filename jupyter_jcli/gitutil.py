"""Shared git repository helpers."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_NON_REPO_RE = re.compile(
    r"^fatal: not a git repository "
    r"(?:\(or any of the parent directories\): \.git|"
    r"\(or any parent up to mount point .+\)\n?"
    r"(?:Stopping at filesystem boundary "
    r"\(GIT_DISCOVERY_ACROSS_FILESYSTEM not set\)\.)?)$"
)


@dataclass(frozen=True)
class GitRootResult:
    """Structured result of looking up a repository root."""

    root: Path | None
    error: BaseException | None = None


def resolve_git_root(cwd: Path | None = None) -> GitRootResult:
    """Resolve a repository root while distinguishing absence from failure."""
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(cwd if cwd is not None else Path.cwd()),
            env=env,
        )
    except (OSError, FileNotFoundError) as exc:
        return GitRootResult(None, exc)
    if proc.returncode == 0:
        root = proc.stdout.strip()
        if root:
            return GitRootResult(Path(root))
        return GitRootResult(
            None, RuntimeError("git returned an empty repository root")
        )

    stderr = (getattr(proc, "stderr", "") or "").strip()
    # Git uses status 128 for no repository, but other 128 failures are real
    # errors (for example malformed command-line configuration).
    if proc.returncode == 128 and _NON_REPO_RE.fullmatch(stderr):
        return GitRootResult(None)
    detail = stderr or f"git rev-parse failed with status {proc.returncode}"
    return GitRootResult(None, RuntimeError(detail))


def git_root(cwd: Path | None = None) -> Path | None:
    """Return the repository root containing *cwd*.

    This compatibility wrapper preserves the historical fail-open API. Hook
    entry points use :func:`resolve_git_root` when they must report failures.
    """
    return resolve_git_root(cwd).root
