"""Small helpers for concise human-facing command output."""

from pathlib import Path


def display_path(path: str | Path) -> str:
    """Show paths inside the current directory relatively, others absolutely."""
    resolved = Path(path).expanduser().resolve(strict=False)
    cwd = Path.cwd().resolve()
    try:
        return str(resolved.relative_to(cwd)) or "."
    except ValueError:
        return str(resolved)
