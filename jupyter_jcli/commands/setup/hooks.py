"""Shared native JSON hook backend for setup host adapters."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jupyter_jcli.output import emit_error

_MANAGED_KEY = "_jcli_managed"


@dataclass(frozen=True)
class HookBlock:
    """One managed native hook entry and its upgrade aliases."""

    event: str
    matcher: str
    platforms: frozenset[str]
    command: str
    managed_value: str
    legacy: frozenset[str] = frozenset()


MANAGED_BLOCKS = (
    HookBlock(
        "PreToolUse",
        "Bash",
        frozenset({"claude", "codex"}),
        "j-cli _hooks notebook-exec-guard{platform_flag}",
        "notebook-exec-guard",
        frozenset({"nbconvert-guard"}),
    ),
    HookBlock(
        "PreToolUse",
        "Edit|Write",
        frozenset({"claude", "codex"}),
        "j-cli _hooks pair-drift-guard-pre{platform_flag}",
        "pair-drift-guard-pre",
        frozenset({"pair-drift-guard"}),
    ),
    HookBlock(
        "PreToolUse",
        "NotebookEdit",
        frozenset({"claude"}),
        "j-cli _hooks notebook-edit-guard{platform_flag}",
        "notebook-edit-guard",
        frozenset({"pair-drift-guard-notebook"}),
    ),
    HookBlock(
        "PostToolUse",
        "Edit|Write",
        frozenset({"claude", "codex"}),
        "j-cli _hooks pair-drift-guard-post{platform_flag}",
        "pair-drift-guard-post",
    ),
    HookBlock(
        "PreToolUse",
        "Bash",
        frozenset({"claude", "codex"}),
        "j-cli _hooks python-run-guard{platform_flag}",
        "python-run-guard",
    ),
)


def _managed_values(blocks: tuple[HookBlock, ...]) -> frozenset[str]:
    return frozenset(
        value
        for block in blocks
        for value in ({block.managed_value} | set(block.legacy))
    )


ALL_MANAGED_VALUES = _managed_values(MANAGED_BLOCKS)
# The retired DSH bridge used the Bash and Edit/Write guards, not NotebookEdit.
DSH_MANAGED_VALUES = _managed_values(
    tuple(block for block in MANAGED_BLOCKS if block.matcher in {"Bash", "Edit|Write"})
)


def load_settings(path: Path, use_json: bool) -> dict[str, Any]:
    """Load a native hook settings object, accepting missing and empty files."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        settings = json.loads(text)
    except json.JSONDecodeError as exc:
        emit_error("SETTINGS_INVALID", f"{path}: {exc}", use_json)
    if not isinstance(settings, dict):
        emit_error("SETTINGS_INVALID", f"{path}: expected a JSON object", use_json)
    return settings


def write_settings(path: Path, settings: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def merge_hook(settings: dict[str, Any], block: HookBlock, platform: str) -> None:
    """Merge one managed block while preserving unrelated native hooks."""
    platform_flag = {
        "codex": " --platform codex",
        "dsh": " --platform dsh",
    }.get(platform, "")
    entry = {
        "type": "command",
        "command": block.command.replace("{platform_flag}", platform_flag),
        _MANAGED_KEY: block.managed_value,
    }
    managed_values = frozenset({block.managed_value}) | block.legacy
    hooks_map = settings.setdefault("hooks", {})
    event_list = hooks_map.setdefault(block.event, [])

    placed = False
    for group in event_list:
        if not isinstance(group, dict) or group.get("matcher") != block.matcher:
            continue
        updated = []
        for current in group.get("hooks", []):
            if (
                isinstance(current, dict)
                and current.get(_MANAGED_KEY) in managed_values
            ):
                if not placed:
                    updated.append(entry)
                    placed = True
            else:
                updated.append(current)
        group["hooks"] = updated
    if not placed:
        event_list.append({"matcher": block.matcher, "hooks": [entry]})


def remove_managed_hooks(
    settings: dict[str, Any], managed_values: frozenset[str] = ALL_MANAGED_VALUES
) -> int:
    """Remove selected j-cli entries and empty hook groups."""
    hooks_map = settings.get("hooks")
    if not isinstance(hooks_map, dict):
        return 0
    removed = 0
    for event in list(hooks_map):
        groups = hooks_map.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            entries = group.get("hooks", [])
            if not isinstance(entries, list):
                kept_groups.append(group)
                continue
            kept_entries = [
                entry
                for entry in entries
                if not (
                    isinstance(entry, dict)
                    and entry.get(_MANAGED_KEY) in managed_values
                )
            ]
            removed += len(entries) - len(kept_entries)
            if kept_entries:
                kept_groups.append({**group, "hooks": kept_entries})
        hooks_map[event] = kept_groups
    prune_hook_settings(settings)
    return removed


def install_or_remove_hooks(
    platform: str,
    path: Path,
    remove: bool,
    use_json: bool,
    force: bool = False,
) -> tuple[bool, int]:
    """Apply one host's managed native hooks and report changes/removals."""
    if remove:
        if not path.exists():
            return False, 0
        settings = load_settings(path, use_json)
        removed = remove_managed_hooks(settings)
        if removed:
            if settings:
                write_settings(path, settings)
            else:
                path.unlink()
        return bool(removed), removed

    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.read_text(encoding="utf-8") if path.exists() else None
    settings = load_settings(path, use_json)
    for block in MANAGED_BLOCKS:
        if platform in block.platforms:
            merge_hook(settings, block, platform)
    rendered = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    if rendered == before and not (force and path.is_symlink()):
        return False, 0
    if force and path.is_symlink():
        path.unlink()
    path.write_text(rendered, encoding="utf-8")
    return True, 0


def managed_hooks_present(path: Path) -> bool:
    """Return whether a JSON file contains any current or legacy managed hook."""
    if not path.exists():
        return False
    settings = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise TypeError("expected a JSON object")
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError("hooks must be an object")
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise TypeError("hook event must be an array")
        for group in groups:
            if not isinstance(group, dict):
                continue
            entries = group.get("hooks", [])
            if not isinstance(entries, list):
                raise TypeError("hook group must contain an array")
            if any(
                isinstance(entry, dict)
                and entry.get(_MANAGED_KEY) in ALL_MANAGED_VALUES
                for entry in entries
            ):
                return True
    return False


def remove_dsh_hooks(settings: dict[str, Any]) -> int:
    """Remove legacy DSH bridge entries from Claude-shaped settings."""
    return remove_managed_hooks(settings, DSH_MANAGED_VALUES)


def load_dsh_legacy_settings(path: Path, use_json: bool) -> dict[str, Any]:
    """Read and validate a legacy DSH bridge settings file."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text) if text.strip() else {}
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        emit_error("DSH_HOOKS_INVALID", f"{path}: {exc}", use_json)
    if not isinstance(raw, dict):
        emit_error(
            "DSH_HOOKS_INVALID",
            f"{path}: top-level value must be an object",
            use_json,
        )
    hooks = raw.get("hooks")
    if "hooks" in raw and not isinstance(hooks, dict):
        emit_error("DSH_HOOKS_INVALID", f"{path}: hooks must be an object", use_json)
    if isinstance(hooks, dict):
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                emit_error(
                    "DSH_HOOKS_INVALID",
                    f"{path}: hooks.{event} must be an array",
                    use_json,
                )
            for group in groups:
                if (
                    isinstance(group, dict)
                    and "hooks" in group
                    and not isinstance(group["hooks"], list)
                ):
                    emit_error(
                        "DSH_HOOKS_INVALID",
                        f"{path}: hooks.{event} group hooks must be an array",
                        use_json,
                    )
    return raw


def prune_hook_settings(settings: dict[str, Any]) -> None:
    """Drop empty native hook containers without changing user values."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        if not hooks.get(event):
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)


def clean_dsh_legacy_settings(
    settings: dict[str, Any],
) -> tuple[dict[str, Any], int, bool]:
    """Remove DSH bridge entries and report whether user content remains."""
    cleaned = copy.deepcopy(settings)
    removed = remove_dsh_hooks(cleaned)
    return cleaned, removed, bool(cleaned)
