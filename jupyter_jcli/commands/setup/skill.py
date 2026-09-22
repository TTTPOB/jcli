"""Install the bundled j-cli agent skill into a selected target directory."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath

_MARKER_NAME = ".j-cli-managed.json"
_MARKER_SCHEMA = 1
_MARKER_SOURCE = "jupyter-jcli:j-cli"
_RESOURCE_PARTS = ("skills", "j-cli")


class SkillError(RuntimeError):
    """Base error for bundled skill installation operations."""


class SkillConflictError(SkillError):
    """Raised when a target cannot be changed without touching user content."""


class SkillResourceError(SkillError):
    """Raised when the installed distribution lacks a valid bundled skill."""


@dataclass(frozen=True)
class _InstallPlan:
    files: Mapping[str, bytes]
    old_files: Mapping[str, str] | None


def _skill_resource():
    """Return the packaged skill tree without falling back to checkout paths."""
    return resources.files("jupyter_jcli").joinpath(*_RESOURCE_PARTS)


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_resource_files() -> dict[str, bytes]:
    root = _skill_resource()
    files: dict[str, bytes] = {}

    def visit(directory, parts: tuple[str, ...]) -> None:
        for child in sorted(directory.iterdir(), key=lambda child: child.name):
            relative_parts = (*parts, child.name)
            relative = PurePosixPath(*relative_parts).as_posix()
            is_symlink = getattr(child, "is_symlink", None)
            if is_symlink is not None and is_symlink():
                raise SkillResourceError(
                    f"Bundled j-cli skill contains a symbolic link: {relative}"
                )
            if child.is_dir():
                visit(child, relative_parts)
            elif child.is_file():
                files[relative] = child.read_bytes()
            else:
                raise SkillResourceError(
                    f"Bundled j-cli skill contains an unsupported entry: {relative}"
                )

    try:
        visit(root, ())
    except OSError as exc:
        raise SkillResourceError(
            f"Bundled j-cli skill resource is missing or unreadable: {exc}"
        ) from exc
    if "SKILL.md" not in files:
        raise SkillResourceError("Bundled j-cli skill resource has no SKILL.md")
    return files


def _marker_bytes(files: Mapping[str, bytes]) -> bytes:
    marker = {
        "schema": _MARKER_SCHEMA,
        "source": _MARKER_SOURCE,
        "files": {name: _hash(content) for name, content in sorted(files.items())},
    }
    return (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode()


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SkillConflictError("Managed skill marker contains an invalid file path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise SkillConflictError("Managed skill marker contains an invalid file path")
    normalized = path.as_posix()
    if normalized != value or normalized == _MARKER_NAME:
        raise SkillConflictError("Managed skill marker contains an invalid file path")
    return normalized


def _reject_symlinks(target: Path) -> None:
    def inspection_failed(error: OSError) -> None:
        raise SkillConflictError(f"Cannot inspect skill target: {target}") from error

    for root, directories, filenames in os.walk(
        target, followlinks=False, onerror=inspection_failed
    ):
        root_path = Path(root)
        for name in (*directories, *filenames):
            if (root_path / name).is_symlink():
                raise SkillConflictError(
                    f"Skill target contains a symbolic link: {root_path / name}"
                )


def _require_directory_ancestor(target: Path) -> None:
    ancestor = target.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if ancestor.exists() and not ancestor.is_dir():
        raise SkillConflictError(
            f"Skill target ancestor is not a directory: {ancestor}"
        )


def _load_managed_files(target: Path) -> dict[str, str] | None:
    _require_directory_ancestor(target)
    if target.is_symlink():
        raise SkillConflictError(f"Skill target is a symbolic link: {target}")
    if not target.exists():
        return None
    if not target.is_dir():
        raise SkillConflictError(f"Skill target is not a directory: {target}")

    _reject_symlinks(target)
    marker_path = target / _MARKER_NAME
    if not marker_path.is_file():
        raise SkillConflictError(f"Skill target is not managed by j-cli: {target}")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillConflictError(
            f"Managed skill marker is invalid: {marker_path}"
        ) from exc
    if not isinstance(marker, dict):
        raise SkillConflictError(f"Managed skill marker is invalid: {marker_path}")
    if marker.get("schema") != _MARKER_SCHEMA or marker.get("source") != _MARKER_SOURCE:
        raise SkillConflictError(f"Managed skill marker is invalid: {marker_path}")
    raw_files = marker.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise SkillConflictError(f"Managed skill marker is invalid: {marker_path}")

    managed: dict[str, str] = {}
    for raw_name, raw_digest in raw_files.items():
        name = _validate_relative_path(raw_name)
        if (
            not isinstance(raw_digest, str)
            or len(raw_digest) != 64
            or any(character not in "0123456789abcdef" for character in raw_digest)
        ):
            raise SkillConflictError(f"Managed skill marker is invalid: {marker_path}")
        managed[name] = raw_digest

    for name, expected_digest in managed.items():
        path = target.joinpath(*PurePosixPath(name).parts)
        try:
            matches = path.is_file() and _hash(path.read_bytes()) == expected_digest
        except OSError as exc:
            raise SkillConflictError(f"Cannot read managed skill file: {path}") from exc
        if not matches:
            raise SkillConflictError(
                f"Managed skill file was modified or removed: {path}"
            )
    return managed


def _check_new_file_conflicts(
    target: Path, files: Mapping[str, bytes], managed: Mapping[str, str]
) -> None:
    for name in files:
        relative = PurePosixPath(name)
        path = target.joinpath(*relative.parts)
        if path.exists() and name not in managed:
            raise SkillConflictError(
                f"Skill update would replace an unmanaged entry: {path}"
            )
        for parent in relative.parents:
            if parent == PurePosixPath("."):
                continue
            parent_path = target.joinpath(*parent.parts)
            if parent_path.exists() and not parent_path.is_dir():
                raise SkillConflictError(
                    f"Skill update requires replacing a non-directory entry: {parent_path}"
                )


def _plan_install(target: Path) -> _InstallPlan:
    files = _load_resource_files()
    managed = _load_managed_files(target)
    if managed is not None:
        _check_new_file_conflicts(target, files, managed)
    return _InstallPlan(files=files, old_files=managed)


def preflight_install_skill(target: Path) -> None:
    """Validate that installing to *target* is safe without changing files."""
    _plan_install(Path(target))


def preflight_remove_skill(target: Path) -> None:
    """Validate that removing the managed skill at *target* is safe."""
    _load_managed_files(Path(target))


def _file_mode(name: str, content: bytes) -> int:
    parts = PurePosixPath(name).parts
    if parts and parts[0] == "scripts" and content.startswith(b"#!"):
        return 0o755
    return 0o644


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _write_tree(target: Path, files: Mapping[str, bytes]) -> None:
    for name, content in files.items():
        path = target.joinpath(*PurePosixPath(name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(_file_mode(name, content))
    marker = target / _MARKER_NAME
    marker.write_bytes(_marker_bytes(files))
    marker.chmod(0o644)


def _install_new(target: Path, files: Mapping[str, bytes]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        _write_tree(staging, files)
        os.replace(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install_skill(target: Path) -> bool:
    """Install or safely update the bundled skill at final directory *target*.

    Return ``True`` when files changed and ``False`` when the managed install was
    already current. Never replaces an unmanaged directory or symbolic link.
    """
    target = Path(target)
    plan = _plan_install(target)
    marker = _marker_bytes(plan.files)
    if plan.old_files is None:
        _install_new(target, plan.files)
        return True

    current_marker = (target / _MARKER_NAME).read_bytes()
    old_names = set(plan.old_files)
    new_names = set(plan.files)
    changed = current_marker != marker or old_names != new_names
    for name, content in plan.files.items():
        path = target.joinpath(*PurePosixPath(name).parts)
        if not path.is_file() or path.read_bytes() != content:
            _atomic_write(path, content, _file_mode(name, content))
            changed = True

    obsolete_names = sorted(
        old_names - new_names, key=lambda item: item.count("/"), reverse=True
    )
    for name in obsolete_names:
        target.joinpath(*PurePosixPath(name).parts).unlink()
        changed = True
    if changed:
        _atomic_write(target / _MARKER_NAME, marker, 0o644)
        _remove_empty_directories(target)
    return changed


def _remove_empty_directories(target: Path) -> None:
    directories = sorted(
        (path for path in target.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def remove_skill(target: Path) -> bool:
    """Remove files belonging to a managed skill, preserving unrelated files."""
    target = Path(target)
    managed = _load_managed_files(target)
    if managed is None:
        return False

    for name in sorted(managed, key=lambda item: item.count("/"), reverse=True):
        target.joinpath(*PurePosixPath(name).parts).unlink()
    (target / _MARKER_NAME).unlink()
    _remove_empty_directories(target)
    try:
        target.rmdir()
    except OSError:
        pass
    return True
