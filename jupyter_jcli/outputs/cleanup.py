"""Retention cleanup for workspace output runs."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_RUNS = 50
RETENTION_DAYS_ENV = "JCLI_OUTPUT_RETENTION_DAYS"
MAX_RUNS_ENV = "JCLI_OUTPUT_MAX_RUNS"


@dataclass
class CleanupResult:
    """Structured cleanup outcome including preserved uncertain state."""

    root: str
    dry_run: bool
    deleted_runs: list[str] = field(default_factory=list)
    would_delete_runs: list[str] = field(default_factory=list)
    retained_runs: list[str] = field(default_factory=list)
    skipped_runs: list[dict[str, str]] = field(default_factory=list)
    failed_runs: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": "ok" if not self.failed_runs else "partial",
            "root": self.root,
            "dry_run": self.dry_run,
            "deleted_runs": self.deleted_runs,
            "would_delete_runs": self.would_delete_runs,
            "retained_runs": self.retained_runs,
            "skipped_runs": self.skipped_runs,
            "failed_runs": self.failed_runs,
        }


def retention_settings(
    *, days: int | None = None, max_runs: int | None = None
) -> tuple[int, int]:
    """Resolve positive CLI overrides, then positive environment overrides."""
    return (
        _positive_setting(days, RETENTION_DAYS_ENV, DEFAULT_RETENTION_DAYS),
        _positive_setting(max_runs, MAX_RUNS_ENV, DEFAULT_MAX_RUNS),
    )


def cleanup_outputs(
    *,
    cwd: str | Path | None = None,
    days: int | None = None,
    max_runs: int | None = None,
    dry_run: bool = False,
    protected_run_ids: set[str] | None = None,
    now: float | None = None,
) -> CleanupResult:
    """Delete complete expired run groups while preserving uncertain state."""
    retention_days, retention_max_runs = retention_settings(
        days=days, max_runs=max_runs
    )
    workspace = (Path.cwd() if cwd is None else Path(cwd)).resolve()
    root = workspace / ".j-cli" / "outputs"
    result = CleanupResult(root=str(root), dry_run=dry_run)
    protected = protected_run_ids or set()
    current_time = time.time() if now is None else now
    if not root.exists():
        return result
    if root.is_symlink() or not root.is_dir():
        result.skipped_runs.append(
            {"path": str(root), "reason": "output root is not a real directory"}
        )
        return result

    complete_runs: list[tuple[float, Path, set[Path]]] = []
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            result.skipped_runs.append(
                {"path": str(entry), "reason": "unknown or linked output entry"}
            )
            continue
        inspected = _inspect_complete_run(root, entry)
        if isinstance(inspected, str):
            result.skipped_runs.append({"path": str(entry), "reason": inspected})
            continue
        created_at, known_files = inspected
        complete_runs.append((created_at, entry, known_files))

    complete_runs.sort(key=lambda item: item[0], reverse=True)
    cutoff = current_time - retention_days * 86_400
    for position, (created_at, run_dir, known_files) in enumerate(complete_runs):
        run_id = run_dir.name
        should_delete = created_at < cutoff or position >= retention_max_runs
        if run_id in protected:
            result.retained_runs.append(str(run_dir))
            continue
        if not should_delete:
            result.retained_runs.append(str(run_dir))
            continue
        if dry_run:
            result.would_delete_runs.append(str(run_dir))
            continue
        try:
            for path in sorted(known_files, reverse=True):
                path.unlink()
            run_dir.rmdir()
        except OSError as error:
            result.failed_runs.append({"path": str(run_dir), "reason": str(error)})
        else:
            result.deleted_runs.append(str(run_dir))
    return result


def _inspect_complete_run(root: Path, run_dir: Path) -> tuple[float, set[Path]] | str:
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return "incomplete or state-unknown run retained"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        created_at = float(manifest["created_at"])
        outputs = manifest["outputs"]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return "invalid or state-unknown manifest retained"
    if manifest.get("status") != "complete" or not isinstance(outputs, list):
        return "incomplete or state-unknown run retained"

    known_files = {manifest_path}
    for output in outputs:
        data = output.get("data", {}) if isinstance(output, dict) else {}
        if not isinstance(data, dict):
            continue
        for reference in data.values():
            if not isinstance(reference, dict) or reference.get("type") != "file":
                continue
            path = Path(str(reference.get("path", "")))
            if path.is_symlink() or not path.is_absolute():
                return "linked or relative payload reference retained"
            resolved = path.resolve()
            if resolved.parent != run_dir or not resolved.is_file():
                return "payload reference escapes or is missing"
            known_files.add(resolved)

    try:
        actual_files = set(run_dir.iterdir())
    except OSError:
        return "run contents could not be inspected"
    if any(path.is_symlink() or not path.is_file() for path in actual_files):
        return "unknown or linked run contents retained"
    if actual_files != known_files:
        return "unknown run files retained"
    if run_dir.resolve().parent != root.resolve():
        return "run directory escapes output root"
    return created_at, known_files


def _positive_setting(value: int | None, env_name: str, default: int) -> int:
    if value is not None:
        if value <= 0:
            raise ValueError(f"{env_name} override must be positive")
        return value
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError as error:
        raise ValueError(f"{env_name} must be a positive integer") from error
    if parsed <= 0:
        raise ValueError(f"{env_name} must be a positive integer")
    return parsed
