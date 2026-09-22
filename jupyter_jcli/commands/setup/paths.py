"""Canonical environment-backed paths shared by setup components."""

import os
from pathlib import Path


def codex_home() -> Path:
    """Return the effective Codex configuration directory."""
    return (
        Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        .expanduser()
        .resolve()
    )
