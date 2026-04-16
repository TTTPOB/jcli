"""Wrap `git merge-file` for three-way text merge."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MergeResult:
    text: str            # merged content (with conflict markers if any)
    has_conflict: bool   # True iff exit_code > 0
    conflict_count: int  # exit_code value (number of conflict hunks)


def merge_three_way(
    base: str,
    ours: str,
    theirs: str,
    ours_label: str = "py (current)",
    base_label: str = "py (HEAD)",
    theirs_label: str = "ipynb (current)",
) -> MergeResult:
    """Run `git merge-file --stdout --diff3` on three temp files.

    Conflict markers use the provided labels so the agent can identify sources:
        <<<<<<< py (current)   — ours
        ||||||| py (HEAD)      — base (diff3 style)
        =======
        >>>>>>> ipynb (current) — theirs

    Falls back to a synthetic conflict block if git binary is unavailable.
    """
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ours_file = tmp / "ours.py"
            base_file = tmp / "base.py"
            theirs_file = tmp / "theirs.py"
            ours_file.write_text(ours, encoding="utf-8")
            base_file.write_text(base, encoding="utf-8")
            theirs_file.write_text(theirs, encoding="utf-8")

            proc = subprocess.run(
                [
                    "git", "merge-file", "--stdout", "--diff3",
                    "-L", ours_label,
                    "-L", base_label,
                    "-L", theirs_label,
                    str(ours_file), str(base_file), str(theirs_file),
                ],
                capture_output=True,
                check=False,
            )
            exit_code = proc.returncode

            if exit_code < 0:
                return _fallback_merge(base, ours, theirs, ours_label, base_label, theirs_label)

            return MergeResult(
                text=proc.stdout.decode("utf-8"),
                has_conflict=exit_code > 0,
                conflict_count=exit_code,
            )
    except (OSError, FileNotFoundError):
        return _fallback_merge(base, ours, theirs, ours_label, base_label, theirs_label)


def _fallback_merge(
    base: str,
    ours: str,
    theirs: str,
    ours_label: str,
    base_label: str,
    theirs_label: str,
) -> MergeResult:
    """Synthetic conflict block when git binary is unavailable."""
    def _ensure_newline(s: str) -> str:
        return s if s.endswith("\n") else s + "\n"

    text = (
        f"<<<<<<< {ours_label}\n"
        + _ensure_newline(ours)
        + f"||||||| {base_label}\n"
        + _ensure_newline(base)
        + "=======\n"
        + _ensure_newline(theirs)
        + f">>>>>>> {theirs_label}\n"
    )
    return MergeResult(text=text, has_conflict=True, conflict_count=1)
