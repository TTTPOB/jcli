"""Native DSH adapter installation for ``j-cli setup dsh``."""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import tempfile
from importlib import metadata, resources
from pathlib import Path
from typing import Any

import click
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import CliContext, pass_ctx
from jupyter_jcli.output import emit, emit_error

from .common import Scope
from .hooks import _remove_dsh_hooks

_DSH_ROW_ID = "jcli-hooks"
_DSH_MARKER_START = "# >>> jcli managed (dsh hooks) >>>"
_DSH_MARKER_END = "# <<< jcli managed (dsh hooks) <<<"
_DSH_MARKER_RE = re.compile(
    rf"(?ms)^{re.escape(_DSH_MARKER_START)}\n.*?^{re.escape(_DSH_MARKER_END)}\n?"
)
_DSH_PLUGIN_MARKER = "// Managed by j-cli setup dsh."
_DSH_PLUGIN_RESOURCE = "dsh_plugin.ts"
_DSH_VERSION_RE = re.compile(r"^// j-cli version .*$")


@click.command("dsh")
@click.option(
    "--local",
    "scope",
    flag_value=Scope.LOCAL.value,
    default=True,
    help=(
        "Write ./.dsh/cordis.yml and ./.dsh/plugins/jcli.ts (default; "
        "alias for --project)."
    ),
)
@click.option(
    "--proj",
    "--project",
    "scope",
    flag_value=Scope.PROJECT.value,
    help="Write workspace DSH files (project scope).",
)
@click.option(
    "--global",
    "--user",
    "scope",
    flag_value=Scope.USER.value,
    help="Write $DSH_HOME/cordis.patch.yml and its native plugin (global scope).",
)
@click.option(
    "--remove",
    is_flag=True,
    default=False,
    help="Remove only j-cli managed DSH rows, adapters, and legacy entries.",
)
@pass_ctx
def dsh(ctx: CliContext, scope: str, remove: bool) -> None:
    """Install or remove the native j-cli adapter for DSH."""
    scope_value = Scope(scope)
    if scope_value == Scope.LOCAL:
        click.echo(
            "Note: dsh-workspace-overlay has no separate local layer; "
            "--local writes workspace .dsh files like --project.",
            err=True,
        )
    config_path, plugin_path = _resolve_paths(scope_value)
    _install_or_remove(scope_value, config_path, plugin_path, remove, ctx)


def _resolve_paths(scope: Scope) -> tuple[Path, Path]:
    """Return the DSH composition path and native TypeScript adapter path."""
    if scope == Scope.USER:
        dsh_home = Path(os.environ.get("DSH_HOME") or "~/.dsh").expanduser().resolve()
        return dsh_home / "cordis.patch.yml", dsh_home / "plugins" / "jcli.ts"

    workspace_dsh = Path.cwd().resolve() / ".dsh"
    return workspace_dsh / "cordis.yml", workspace_dsh / "plugins" / "jcli.ts"


def _legacy_hooks_path(scope: Scope) -> Path:
    """Return the legacy bridge settings path for migration/removal."""
    if scope == Scope.USER:
        dsh_home = Path(os.environ.get("DSH_HOME") or "~/.dsh").expanduser().resolve()
        return dsh_home / "jcli-hooks.json"
    return Path.cwd().resolve() / ".dsh" / "jcli-hooks.json"


def _scope_label(scope: Scope) -> str:
    return "global" if scope == Scope.USER else "project"


def _yaml_quote(value: str) -> str:
    """Quote a filesystem path without introducing YAML escape semantics."""
    return "'" + value.replace("'", "''") + "'"


def _managed_block(scope: Scope, plugin_path: Path) -> str:
    """Build the marked YAML row for one DSH composition layer."""
    module_name = (
        "./plugins/jcli.ts" if scope != Scope.USER else str(plugin_path.resolve())
    )
    row = [
        f"- id: {_DSH_ROW_ID}",
        f"  name: {_yaml_quote(module_name)}",
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
        # Keep the first managed block's position and remove duplicate blocks
        # without allowing paths in the replacement to affect regex syntax.
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
    """Read and validate the legacy bridge settings file."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        raw = json.loads(text)
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
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


def _prune_hook_settings(settings: dict[str, Any]) -> None:
    """Drop empty legacy hook containers without changing user values."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        if not hooks.get(event):
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)


def _plugin_resource_source(use_json: bool) -> str:
    """Load and validate the packaged native adapter before any file write."""
    try:
        source = (
            resources.files("jupyter_jcli")
            .joinpath(_DSH_PLUGIN_RESOURCE)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        emit_error(
            "DSH_PLUGIN_RESOURCE_MISSING",
            f"jupyter_jcli/{_DSH_PLUGIN_RESOURCE}: {exc}",
            use_json,
        )

    if not _has_plugin_header(source):
        emit_error(
            "DSH_PLUGIN_INVALID",
            f"jupyter_jcli/{_DSH_PLUGIN_RESOURCE}: missing managed header",
            use_json,
        )
    return source


def _versioned_plugin_source(source: str, use_json: bool) -> str:
    """Add a generated package-version comment directly after the header."""
    try:
        package_version = metadata.version("jupyter-jcli")
    except metadata.PackageNotFoundError as exc:
        emit_error(
            "DSH_PLUGIN_VERSION_UNAVAILABLE",
            f"jupyter-jcli distribution metadata is unavailable: {exc}",
            use_json,
        )

    lines = source.splitlines(keepends=True)
    version_line = f"// j-cli version {package_version}\n"
    if len(lines) > 1 and _DSH_VERSION_RE.match(lines[1].rstrip("\r\n")):
        lines[1] = version_line
    else:
        lines.insert(1, version_line)
    return "".join(lines)


def _has_plugin_header(source: str) -> bool:
    """Recognize the exact managed header, including a header-only file."""
    first_line = source.splitlines()[0] if source.splitlines() else ""
    return first_line == _DSH_PLUGIN_MARKER


def _validate_plugin_conflict(path: Path, use_json: bool) -> None:
    if not path.exists():
        return
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as exc:
        emit_error("DSH_PLUGIN_READ_FAILED", f"{path}: {exc}", use_json)
    if not _has_plugin_header(current):
        emit_error(
            "DSH_PLUGIN_CONFLICT",
            f"Refusing to overwrite non-j-cli native adapter: {path}",
            use_json,
        )


def _validate_plugin_remove(path: Path, use_json: bool) -> None:
    if not path.exists():
        return
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as exc:
        emit_error("DSH_PLUGIN_READ_FAILED", f"{path}: {exc}", use_json)
    if not _has_plugin_header(current):
        emit_error(
            "DSH_PLUGIN_NOT_MANAGED",
            f"Refusing to remove non-j-cli native adapter: {path}",
            use_json,
        )


def _atomic_replace(path: Path, text: str) -> None:
    """Replace a text file atomically using a sibling temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _json_text(settings: dict[str, Any]) -> str:
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def _legacy_cleanup(settings: dict[str, Any]) -> tuple[dict[str, Any], int, bool]:
    """Remove legacy bridge entries and report remaining user content."""
    cleaned = copy.deepcopy(settings)
    removed = _remove_dsh_hooks(cleaned)
    _prune_hook_settings(cleaned)
    return cleaned, removed, bool(cleaned)


def _legacy_changed(path: Path, before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Treat an existing empty object/file as removable legacy state."""
    return path.exists() and (before != after or not after)


def _warn_legacy_user_content(path: Path, has_user_content: bool) -> None:
    if has_user_content:
        click.echo(
            f"warning: preserved user content in legacy {path}; the native DSH "
            "adapter does not execute custom legacy hooks, so configure them separately",
            err=True,
        )


def _remove_config_block(
    config_path: Path, config_text: str, use_json: bool
) -> tuple[str, bool]:
    existing_root = _parse_yaml(config_path, config_text, use_json)
    config_after = _remove_marked_blocks(config_text)
    if config_text != config_after:
        remaining_root = _parse_yaml(config_path, config_after, use_json)
        if remaining_root is None and (
            config_after.strip() or existing_root is not None
        ):
            # Replace the first managed block in place so comments and an
            # explicit document marker remain valid without moving rows.
            config_after = _replace_or_append_block(config_text, "[]\n", existing_root)
        _parse_yaml(config_path, config_after, use_json)
    return config_after, config_after != config_text


def _warn_duplicate_scope(scope: Scope) -> None:
    """Warn when workspace and global native rows may both run."""
    if scope == Scope.USER:
        other = Path.cwd().resolve() / ".dsh" / "cordis.yml"
    else:
        other, _ = _resolve_paths(Scope.USER)
    if other.exists() and _DSH_MARKER_RE.search(_read_text(other)):
        click.echo(
            f"warning: {other} also contains the j-cli DSH row; both scopes may run duplicate hooks",
            err=True,
        )


def _install_or_remove(
    scope: Scope,
    config_path: Path,
    plugin_path: Path,
    remove: bool,
    ctx: CliContext,
) -> None:
    config_text = _read_text(config_path)
    legacy_path = _legacy_hooks_path(scope)
    legacy_before = _read_hook_json(legacy_path, ctx.use_json)
    legacy_after, removed_legacy, has_user_content = _legacy_cleanup(legacy_before)

    if remove:
        _validate_plugin_remove(plugin_path, ctx.use_json)
        config_after, config_changed = _remove_config_block(
            config_path, config_text, ctx.use_json
        )
        plugin_changed = plugin_path.exists()
        legacy_changed = _legacy_changed(legacy_path, legacy_before, legacy_after)
        if not config_changed and not plugin_changed and not legacy_changed:
            _warn_legacy_user_content(legacy_path, has_user_content)
            emit(
                {
                    "status": ResponseStatus.NOOP,
                    "scope": _scope_label(scope),
                    "config_path": str(config_path),
                    "plugin_path": str(plugin_path),
                    "legacy_path": str(legacy_path),
                    "_human": f"Nothing to remove for DSH at {config_path}.",
                },
                ctx.use_json,
            )
            return

        try:
            if config_changed:
                if config_after.strip():
                    _write_text(config_path, config_after)
                elif config_path.exists():
                    config_path.unlink()
            if plugin_changed:
                plugin_path.unlink()
            if legacy_changed:
                if legacy_after:
                    _write_text(legacy_path, _json_text(legacy_after))
                elif legacy_path.exists():
                    legacy_path.unlink()
        except OSError as exc:
            emit_error("DSH_WRITE_FAILED", str(exc), ctx.use_json)
        _warn_legacy_user_content(legacy_path, has_user_content)
        emit(
            {
                "status": ResponseStatus.OK,
                "scope": _scope_label(scope),
                "config_path": str(config_path),
                "plugin_path": str(plugin_path),
                "legacy_path": str(legacy_path),
                "removed": (1 if config_changed else 0)
                + (1 if plugin_changed else 0)
                + removed_legacy,
                "_human": f"Removed j-cli native DSH adapter from {config_path}.",
            },
            ctx.use_json,
        )
        return

    # Preflight every input and generated output before touching the filesystem.
    source = _versioned_plugin_source(
        _plugin_resource_source(ctx.use_json), ctx.use_json
    )
    _validate_plugin_conflict(plugin_path, ctx.use_json)
    existing_root = _parse_yaml(config_path, config_text, ctx.use_json)
    unmanaged_text = _remove_marked_blocks(config_text)
    _validate_unmanaged_ids(config_path, unmanaged_text, ctx.use_json)
    config_after = _replace_or_append_block(
        config_text, _managed_block(scope, plugin_path), existing_root
    )
    _parse_yaml(config_path, config_after, ctx.use_json)
    plugin_changed = not plugin_path.exists() or _read_text(plugin_path) != source
    config_changed = config_after != config_text
    legacy_changed = _legacy_changed(legacy_path, legacy_before, legacy_after)

    # Install the adapter before declaring it in Cordis. This ensures a row that
    # DSH can observe always points at a readable native module.
    try:
        if plugin_changed:
            _atomic_replace(plugin_path, source)
        if config_changed:
            _write_text(config_path, config_after)
        if legacy_changed:
            if legacy_after:
                _write_text(legacy_path, _json_text(legacy_after))
            elif legacy_path.exists():
                legacy_path.unlink()
    except OSError as exc:
        emit_error("DSH_WRITE_FAILED", str(exc), ctx.use_json)

    _warn_legacy_user_content(legacy_path, has_user_content)
    status = (
        ResponseStatus.OK
        if plugin_changed or config_changed or legacy_changed
        else ResponseStatus.NOOP
    )
    emit(
        {
            "status": status,
            "scope": _scope_label(scope),
            "config_path": str(config_path),
            "plugin_path": str(plugin_path),
            "legacy_path": str(legacy_path),
            "_human": (
                f"Wrote native j-cli DSH adapter to {plugin_path} and row to {config_path}"
                if status == ResponseStatus.OK
                else f"Native j-cli DSH adapter is already up to date at {plugin_path}"
            ),
        },
        ctx.use_json,
    )
    _warn_duplicate_scope(scope)
