"""DSH workspace/global hook bridge installation."""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

import click
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import CliContext, pass_ctx
from jupyter_jcli.output import emit, emit_error

from .common import Scope
from .hooks import _install_dsh_hooks, _remove_dsh_hooks

_DSH_ROW_ID = "jcli-hooks"
_DSH_PLUGIN_NAME = "@deepseek-ai/dsh-hooks-claude-code"
_DSH_MARKER_START = "# >>> jcli managed (dsh hooks) >>>"
_DSH_MARKER_END = "# <<< jcli managed (dsh hooks) <<<"
_DSH_MARKER_RE = re.compile(
    rf"(?ms)^{re.escape(_DSH_MARKER_START)}\n.*?^{re.escape(_DSH_MARKER_END)}\n?"
)


@click.command("dsh")
@click.option(
    "--local",
    "scope",
    flag_value=Scope.LOCAL.value,
    default=True,
    help=(
        "Write ./.dsh/cordis.yml (default; alias for --project because "
        "the overlay has no separate local layer)."
    ),
)
@click.option(
    "--proj",
    "--project",
    "scope",
    flag_value=Scope.PROJECT.value,
    help="Write ./.dsh/cordis.yml (project scope).",
)
@click.option(
    "--global",
    "--user",
    "scope",
    flag_value=Scope.USER.value,
    help="Write $DSH_HOME/cordis.patch.yml (global Host scope).",
)
@click.option(
    "--remove",
    is_flag=True,
    default=False,
    help="Remove only j-cli managed DSH rows and hook entries.",
)
@pass_ctx
def dsh(ctx: CliContext, scope: str, remove: bool) -> None:
    """Install the j-cli hook bridge for DSH workspace or Host composition."""
    scope_value = Scope(scope)
    if scope_value == Scope.LOCAL:
        click.echo(
            "Note: dsh-workspace-overlay has no separate local layer; "
            "--local writes to ./.dsh/cordis.yml like --project.",
            err=True,
        )
    config_path, hooks_path = _resolve_paths(scope_value)
    _install_or_remove(scope_value, config_path, hooks_path, remove, ctx)


def _resolve_paths(scope: Scope) -> tuple[Path, Path]:
    """Return the DSH composition path and its adjacent hook JSON path."""
    if scope == Scope.USER:
        dsh_home = Path(os.environ.get("DSH_HOME") or "~/.dsh").expanduser().resolve()
        return dsh_home / "cordis.patch.yml", dsh_home / "jcli-hooks.json"

    workspace_dsh = Path.cwd().resolve() / ".dsh"
    return workspace_dsh / "cordis.yml", workspace_dsh / "jcli-hooks.json"


def _scope_label(scope: Scope) -> str:
    return "global" if scope == Scope.USER else "project"


def _yaml_quote(value: str) -> str:
    """Quote a filesystem path without introducing YAML escape semantics."""
    return "'" + value.replace("'", "''") + "'"


def _managed_block(scope: Scope, hooks_path: Path) -> str:
    """Build the marked YAML block for one DSH composition layer."""
    row = [
        f"- id: {_DSH_ROW_ID}",
        f"  name: {_yaml_quote(_DSH_PLUGIN_NAME)}",
        "  config:",
        f"    configPath: {_yaml_quote(str(hooks_path))}",
    ]
    if scope == Scope.USER:
        row = ["- insert:"] + [f"    {line}" for line in row]
    return "\n".join([_DSH_MARKER_START, *row, _DSH_MARKER_END, ""])


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _parse_yaml(path: Path, text: str, use_json: bool) -> Node | None:
    """Parse one YAML document without constructing or rewriting user values."""
    try:
        documents = list(yaml.compose_all(text))
    except yaml.YAMLError as exc:
        emit_error("DSH_CONFIG_INVALID", f"{path}: {exc}", use_json)

    if len(documents) > 1:
        emit_error(
            "DSH_CONFIG_INVALID",
            f"{path}: multiple YAML documents are not supported",
            use_json,
        )
    root = documents[0] if documents else None
    # PyYAML represents an explicit empty document (`---`) as a null scalar
    # with an empty value; treat that as an empty composition, but keep an
    # explicit `null` scalar invalid.
    if (
        isinstance(root, ScalarNode)
        and root.tag == "tag:yaml.org,2002:null"
        and root.value == ""
    ):
        root = None
    if root is not None and not isinstance(root, SequenceNode):
        emit_error(
            "DSH_CONFIG_INVALID",
            f"{path}: top-level value must be a YAML sequence",
            use_json,
        )
    if isinstance(root, SequenceNode) and any(
        not isinstance(item, MappingNode) for item in root.value
    ):
        emit_error(
            "DSH_CONFIG_INVALID",
            f"{path}: every top-level entry must be a mapping",
            use_json,
        )
    return root


def _mapping_value(node: MappingNode, key: str) -> Node | None:
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            return value_node
    return None


def _row_ids(root: Node | None) -> list[str]:
    """Read row ids from top-level rows and nested insert rows."""
    if not isinstance(root, SequenceNode):
        return []

    ids: list[str] = []
    for item in root.value:
        if not isinstance(item, MappingNode):
            continue
        row_id = _mapping_value(item, "id")
        if isinstance(row_id, ScalarNode):
            ids.append(row_id.value)
        inserted = _mapping_value(item, "insert")
        if isinstance(inserted, SequenceNode):
            for child in inserted.value:
                if not isinstance(child, MappingNode):
                    continue
                child_id = _mapping_value(child, "id")
                if isinstance(child_id, ScalarNode):
                    ids.append(child_id.value)
    return ids


def _marked_matches(text: str) -> list[re.Match[str]]:
    return list(_DSH_MARKER_RE.finditer(text))


def _remove_marked_blocks(text: str) -> str:
    matches = _marked_matches(text)
    if not matches:
        return text
    pieces: list[str] = []
    cursor = 0
    for match in matches:
        pieces.append(text[cursor : match.start()])
        cursor = match.end()
    pieces.append(text[cursor:])
    return "".join(pieces)


def _replace_or_append_block(text: str, block: str, root: Node | None) -> str:
    matches = _marked_matches(text)
    if matches:
        # Keep the first managed block's position and remove any duplicate
        # managed blocks without letting backslashes in paths affect regex replacement.
        pieces: list[str] = []
        cursor = 0
        for index, match in enumerate(matches):
            pieces.append(text[cursor : match.start()])
            if index == 0:
                pieces.append(block)
            cursor = match.end()
        pieces.append(text[cursor:])
        return "".join(pieces)

    # PyYAML marks the exact span of a flow-style empty sequence (`[]`), so the
    # generated row can replace it while comments before and after stay bytewise.
    if isinstance(root, SequenceNode) and not root.value and root.flow_style:
        start = root.start_mark.index
        end = root.end_mark.index
        prefix = text[:start]
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        return prefix + block + text[end:]
    if not text:
        return block
    separator = "" if text.endswith("\n") else "\n"
    return text + separator + block


def _validate_unmanaged_ids(path: Path, text: str, use_json: bool) -> Node | None:
    root = _parse_yaml(path, text, use_json)
    if _DSH_ROW_ID in _row_ids(root):
        emit_error(
            "DSH_CONFIG_CONFLICT",
            f"{path}: unmanaged row id '{_DSH_ROW_ID}' already exists",
            use_json,
        )
    return root


def _read_hook_json(path: Path, use_json: bool) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        emit_error("DSH_HOOKS_INVALID", f"{path}: {exc}", use_json)
    if not isinstance(raw, dict):
        emit_error(
            "DSH_HOOKS_INVALID", f"{path}: top-level value must be an object", use_json
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


def _json_text(settings: dict[str, Any]) -> str:
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def _prune_hook_settings(settings: dict[str, Any]) -> None:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        if not hooks.get(event):
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)


def _warn_duplicate_scope(scope: Scope, config_path: Path) -> None:
    """Warn when the current workspace and global DSH rows may both run."""
    if scope == Scope.USER:
        workspace_config = Path.cwd().resolve() / ".dsh" / "cordis.yml"
        other = workspace_config
    else:
        global_config, _ = _resolve_paths(Scope.USER)
        other = global_config
    if other.exists() and _DSH_MARKER_RE.search(_read_text(other)):
        click.echo(
            f"warning: {other} also contains the j-cli DSH row; both scopes may run duplicate hooks",
            err=True,
        )


def _install_or_remove(
    scope: Scope,
    config_path: Path,
    hooks_path: Path,
    remove: bool,
    ctx: CliContext,
) -> None:
    config_text = _read_text(config_path)
    hooks_before = _read_hook_json(hooks_path, ctx.use_json)

    if remove:
        existing_root = _parse_yaml(config_path, config_text, ctx.use_json)
        config_after = _remove_marked_blocks(config_text)
        if config_text != config_after:
            remaining_root = _parse_yaml(config_path, config_after, ctx.use_json)
            if remaining_root is None and (
                config_after.strip() or existing_root is not None
            ):
                # Replace the first managed block in place so comments and an
                # explicit document marker remain valid without moving rows.
                config_after = _replace_or_append_block(
                    config_text, "[]\n", existing_root
                )
            _parse_yaml(config_path, config_after, ctx.use_json)

        hooks_after = copy.deepcopy(hooks_before)
        removed_hooks = _remove_dsh_hooks(hooks_after)
        _prune_hook_settings(hooks_after)
        hooks_changed = hooks_after != hooks_before
        config_changed = config_after != config_text
        if not config_changed and not hooks_changed:
            emit(
                {
                    "status": ResponseStatus.NOOP,
                    "scope": _scope_label(scope),
                    "config_path": str(config_path),
                    "hooks_path": str(hooks_path),
                    "_human": f"Nothing to remove for DSH at {config_path}.",
                },
                ctx.use_json,
            )
            return

        if config_changed:
            if config_after.strip():
                config_path.write_text(config_after, encoding="utf-8")
            else:
                config_path.unlink()
        if hooks_changed:
            if hooks_after:
                hooks_path.write_text(_json_text(hooks_after), encoding="utf-8")
            else:
                hooks_path.unlink()
        emit(
            {
                "status": ResponseStatus.OK,
                "scope": _scope_label(scope),
                "config_path": str(config_path),
                "hooks_path": str(hooks_path),
                "removed": removed_hooks + (1 if config_changed else 0),
                "_human": f"Removed j-cli DSH hooks from {config_path} and {hooks_path}.",
            },
            ctx.use_json,
        )
        return

    # Validate the existing file before replacing a managed block, then remove
    # that block so its row id cannot collide with the row this command owns.
    existing_root = _parse_yaml(config_path, config_text, ctx.use_json)
    unmanaged_text = _remove_marked_blocks(config_text)
    _validate_unmanaged_ids(config_path, unmanaged_text, ctx.use_json)
    config_after = _replace_or_append_block(
        config_text, _managed_block(scope, hooks_path), existing_root
    )
    _parse_yaml(config_path, config_after, ctx.use_json)

    hooks_after = copy.deepcopy(hooks_before)
    _install_dsh_hooks(hooks_after)
    hooks_after_text = _json_text(hooks_after)
    hooks_before_text = _read_text(hooks_path)
    config_changed = config_after != config_text
    hooks_changed = hooks_after_text != hooks_before_text

    # All parsing and generation is complete before creating either directory.
    if config_changed:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(config_after, encoding="utf-8")
    if hooks_changed:
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_path.write_text(hooks_after_text, encoding="utf-8")

    status = (
        ResponseStatus.OK if config_changed or hooks_changed else ResponseStatus.NOOP
    )
    emit(
        {
            "status": status,
            "scope": _scope_label(scope),
            "config_path": str(config_path),
            "hooks_path": str(hooks_path),
            "_human": (
                f"Wrote DSH j-cli hooks to {config_path} and {hooks_path}"
                if status == ResponseStatus.OK
                else f"DSH j-cli hooks are already up to date at {config_path}"
            ),
        },
        ctx.use_json,
    )
    _warn_duplicate_scope(scope, config_path)
