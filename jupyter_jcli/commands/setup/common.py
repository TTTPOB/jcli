"""Shared types and helpers for setup commands."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from enum import Enum
from pathlib import Path

import click

from jupyter_jcli.output import emit_error


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


def is_git_tracked(path: Path, root: Path | None = None) -> bool:
    """Return whether a path or anything below it is tracked by its repository."""
    target = path.expanduser().resolve()
    probe = (root or target).expanduser().resolve()
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
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--", relative.as_posix()],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
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
