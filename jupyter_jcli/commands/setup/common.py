"""Shared types and helpers for setup commands."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.output import emit, emit_error

GlobalProbe = tuple[Path, Callable[[Path], bool | None]]


class Scope(str, Enum):
    """Target scope for settings files written by setup commands."""

    USER = "user"
    PROJECT = "project"
    LOCAL = "local"


class Component(str, Enum):
    """User-facing setup components."""

    SKILL = "skill"
    HOOK = "hook"
    TOOL = "tool"


ALL_COMPONENTS = frozenset(Component)


@dataclass(frozen=True)
class NativeSetupResult:
    """Outcome of one native-hook host's selected component operations."""

    host_name: str
    components: frozenset[Component]
    remove: bool
    hook_path: Path
    skill_path: Path
    tool_result: str
    hook_changed: bool = False
    removed_hooks: int = 0
    skill_changed: bool = False
    ignore_changed: bool = False

    @property
    def changed(self) -> bool:
        return (
            self.hook_changed
            or self.skill_changed
            or self.ignore_changed
            or self.tool_result in {"installed", "removed"}
        )

    @property
    def removed(self) -> int:
        return (
            self.removed_hooks
            + (1 if self.remove and self.skill_changed else 0)
            + (1 if self.tool_result == "removed" else 0)
        )


def selected_components(values: Iterable[str]) -> frozenset[Component]:
    selected = frozenset(Component(value) for value in values)
    return selected or ALL_COMPONENTS


def only_option(function):
    """Add the repeatable component selector shared by host setup commands."""
    return click.option(
        "--only",
        "only",
        type=click.Choice([component.value for component in Component]),
        multiple=True,
        help="Operate only on this component; repeat to select more than one.",
    )(function)


def force_option(function):
    """Add the shared explicit conflict-takeover option."""
    return click.option(
        "--force",
        is_flag=True,
        default=False,
        help="Replace conflicting selected components during installation.",
    )(function)


def validate_force(force: bool, remove: bool, use_json: bool) -> None:
    """Reject force removal before any selected component can be changed."""
    if force and remove:
        emit_error(
            "FORCE_WITH_REMOVE",
            "--force cannot be combined with --remove",
            use_json,
        )


def validate_skill_dir(
    components: frozenset[Component], skill_dir: Path | None, use_json: bool
) -> None:
    if skill_dir is not None and Component.SKILL not in components:
        emit_error(
            "SKILL_DIR_WITHOUT_SKILL",
            "--skill-dir requires selecting the skill component",
            use_json,
        )


def preflight_skill(
    target: Path,
    components: frozenset[Component],
    remove: bool,
    use_json: bool,
    force: bool = False,
) -> None:
    if Component.SKILL not in components:
        return
    from .skill import SkillError, preflight_install_skill, preflight_remove_skill

    try:
        if remove:
            preflight_remove_skill(target)
        else:
            preflight_install_skill(target, force=force)
    except (SkillError, OSError) as exc:
        emit_error("SKILL_SETUP_FAILED", str(exc), use_json)


def apply_skill(
    target: Path,
    components: frozenset[Component],
    remove: bool,
    use_json: bool,
    force: bool = False,
) -> bool:
    if Component.SKILL not in components:
        return False
    from .skill import SkillError, install_skill, remove_skill

    try:
        if remove:
            return remove_skill(target)
        return install_skill(target, force=force)
    except (SkillError, OSError) as exc:
        emit_error("SKILL_SETUP_FAILED", str(exc), use_json)


def resolve_skill_target(root: Path, override: Path | None) -> Path:
    """Resolve a host's skill target with the shared override semantics."""
    if override is not None:
        root = override.expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        root = root.resolve()
    return root / "j-cli"


def update_skill_ignore(
    target: Path,
    scope: Scope,
    remove: bool,
    components: frozenset[Component],
) -> bool:
    """Update the exact local ignore block shared by every skill target."""
    if Component.SKILL not in components or scope == Scope.USER:
        return False
    return update_managed_gitignore(
        target.parent,
        {Component.SKILL: ["/j-cli/"]},
        components,
        scope == Scope.LOCAL and not remove,
    )


def emit_native_setup_result(result: NativeSetupResult, use_json: bool) -> None:
    """Emit the stable result contract shared by native hook and MCP hosts."""
    if result.remove and Component.SKILL in result.components and not use_json:
        click.echo(
            f"Note: removing shared skill {result.skill_path} affects every host "
            "that discovers it.",
            err=True,
        )
    emit(
        {
            "status": ResponseStatus.OK if result.changed else ResponseStatus.NOOP,
            "components": sorted(component.value for component in result.components),
            "path": str(result.hook_path),
            "hook_path": str(result.hook_path),
            "skill_path": (
                str(result.skill_path) if Component.SKILL in result.components else None
            ),
            "tool_result": result.tool_result,
            "removed": result.removed,
            "_human": (
                f"{'Removed' if result.remove else 'Updated'} "
                f"{result.host_name} components: "
                f"{', '.join(sorted(c.value for c in result.components))}"
                if result.changed
                else (
                    f"Nothing to remove: {result.hook_path} does not exist or has "
                    "no selected managed components."
                    if result.remove
                    else (
                        f"{result.host_name} selected components are already "
                        "up to date."
                    )
                )
            ),
        },
        use_json,
    )


def path_exists(path: Path) -> bool:
    """Return whether a path entry exists, including a broken symlink."""
    return path.exists() or path.is_symlink()


def warn_global_conflicts(
    scope: Scope,
    components: frozenset[Component],
    targets: Mapping[Component, Path],
    probes: Mapping[Component, Iterable[GlobalProbe]],
) -> None:
    """Warn about selected user-scope integrations shadowing this install."""
    if scope == Scope.USER:
        return
    for component in components:
        target = targets.get(component)
        if target is None:
            continue
        expanded_target = target.expanduser()
        target_location = (expanded_target.parent.resolve(), expanded_target.name)
        for path, inspect in probes.get(component, ()):
            candidate = path.expanduser()
            candidate_location = (candidate.parent.resolve(), candidate.name)
            if candidate_location == target_location:
                continue
            try:
                present = inspect(candidate)
            except (OSError, ValueError, TypeError):
                present = None
            if present is None:
                click.echo(
                    f"warning: could not inspect global {component.value} integration "
                    f"at {candidate}; continuing with target {target}",
                    err=True,
                )
            elif present:
                click.echo(
                    f"warning: global {component.value} integration exists at "
                    f"{candidate}; continuing with target {target}",
                    err=True,
                )


def component_ignore_dirs(
    scope: Scope,
    components: frozenset[Component],
    host_directory: Path | None,
    skill_target: Path,
) -> list[Path]:
    if scope == Scope.USER:
        return []
    directories = []
    if host_directory is not None and components & {Component.HOOK, Component.TOOL}:
        directories.append(host_directory)
    if Component.SKILL in components:
        directories.append(skill_target.parent)
    return list(dict.fromkeys(directories))


def is_git_tracked(path: Path) -> bool:
    """Return whether a path or anything below it is tracked by its repository."""
    target = path.expanduser().resolve()
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe.is_file():
        probe = probe.parent
    try:
        repository = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    if repository.returncode != 0:
        return False
    repo_root = Path(repository.stdout.strip()).resolve()
    try:
        relative = target.relative_to(repo_root)
    except ValueError:
        return False
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--", relative.as_posix()],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def preflight_local_untracked(paths: Iterable[Path], use_json: bool) -> None:
    """Reject local setup when selected project-backed files are already tracked."""
    tracked = [path for path in paths if is_git_tracked(path)]
    if tracked:
        rendered = ", ".join(str(path) for path in tracked)
        emit_error(
            "LOCAL_TARGET_TRACKED",
            "Cannot make tracked configuration local by adding .gitignore entries: "
            f"{rendered}. Untrack it explicitly first; j-cli will not run git rm.",
            use_json,
        )


def preflight_gitignore(directories: Iterable[Path], use_json: bool) -> None:
    """Validate managed ignore targets before component writes."""
    for directory in directories:
        path = directory / ".gitignore"
        try:
            if path.exists():
                if not path.is_file():
                    raise OSError("target is not a regular file")
                path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            emit_error("GITIGNORE_INVALID", f"{path}: {exc}", use_json)


def update_managed_gitignore(
    directory: Path,
    component_entries: dict[Component, Iterable[str]],
    selected: frozenset[Component],
    enable: bool,
) -> bool:
    """Update exact j-cli-owned ignore blocks while preserving all user content."""
    path = directory / ".gitignore"
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    text = before
    for component in selected:
        entries = tuple(dict.fromkeys(component_entries.get(component, ())))
        if not entries:
            continue
        start = f"# >>> j-cli local {component.value} >>>"
        end = f"# <<< j-cli local {component.value} <<<"
        pattern = re.compile(
            rf"(?m)^{re.escape(start)}\n.*?^{re.escape(end)}(?:\n|$)", re.DOTALL
        )
        block = "\n".join([start, *entries, end, ""])
        if enable and block in text:
            continue
        text = pattern.sub("", text)
        if enable:
            if text and not text.endswith("\n"):
                text += "\n"
            if text and not text.endswith("\n\n"):
                text += "\n"
            text += block

    if text == before:
        return False
    if text:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    elif path.exists():
        path.unlink()
    return True
