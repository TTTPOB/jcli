"""Native DSH adapter installation for ``j-cli setup dsh``."""

from __future__ import annotations

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

from .common import (
    Component,
    Scope,
    apply_skill,
    component_ignore_dirs,
    force_option,
    only_option,
    path_exists,
    preflight_gitignore,
    preflight_local_untracked,
    preflight_skill,
    resolve_skill_target,
    selected_components,
    update_managed_gitignore,
    update_skill_ignore,
    validate_force,
    validate_skill_dir,
    warn_global_conflicts,
)
from .hooks import clean_dsh_legacy_settings, load_dsh_legacy_settings

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
    help=("Write workspace paths and gitignore selected files (default)."),
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
@only_option
@force_option
@click.option("--skill-dir", type=click.Path(path_type=Path, file_okay=False))
@click.option(
    "--remove", is_flag=True, default=False, help="Remove selected components."
)
@pass_ctx
def dsh(
    ctx: CliContext,
    scope: str,
    only: tuple[str, ...],
    force: bool,
    skill_dir: Path | None,
    remove: bool,
) -> None:
    """Install DSH skill plus independent native hook and tool capabilities."""
    components = selected_components(only)
    validate_force(force, remove, ctx.use_json)
    validate_skill_dir(components, skill_dir, ctx.use_json)
    scope_value = Scope(scope)
    if scope_value == Scope.LOCAL and not ctx.use_json:
        click.echo(
            "Note: DSH has no separate local layer; project paths are managed with exact .gitignore entries.",
            err=True,
        )
    config_path, plugin_path = _resolve_paths(scope_value)
    skill_target = _skill_target(scope_value, skill_dir)
    if not remove:
        _warn_global_dsh_conflicts(
            scope_value, components, config_path, plugin_path, skill_target
        )
    preflight_skill(skill_target, components, remove, ctx.use_json, force)
    local_paths: list[Path] = []
    if components & {Component.HOOK, Component.TOOL}:
        local_paths.extend([config_path, plugin_path])
    if Component.SKILL in components:
        local_paths.append(skill_target)
    if scope_value == Scope.LOCAL:
        preflight_local_untracked(local_paths, ctx.use_json)
    ignore_dirs = component_ignore_dirs(
        scope_value, components, config_path.parent, skill_target
    )
    preflight_gitignore(ignore_dirs, ctx.use_json)
    plugin_result = {"changed": False, "removed": 0, "capabilities": []}
    try:
        if components & {Component.HOOK, Component.TOOL}:
            plugin_result = _install_or_remove(
                scope_value,
                config_path,
                plugin_path,
                remove,
                ctx,
                components,
                force,
            )
        skill_changed = apply_skill(
            skill_target, components, remove, ctx.use_json, force
        )
        ignore_changed = _update_ignores(
            scope_value, components, remove, config_path, plugin_path, skill_target
        )
    except OSError as exc:
        emit_error("DSH_WRITE_FAILED", str(exc), ctx.use_json)
    changed = bool(plugin_result["changed"] or skill_changed or ignore_changed)
    if Component.SKILL in components and remove and not ctx.use_json:
        click.echo(
            f"Note: removing shared skill {skill_target} affects every host that discovers it.",
            err=True,
        )
    emit(
        {
            "status": ResponseStatus.OK if changed else ResponseStatus.NOOP,
            "components": sorted(component.value for component in components),
            "config_path": str(config_path),
            "plugin_path": str(plugin_path),
            "skill_path": str(skill_target) if Component.SKILL in components else None,
            "capabilities": plugin_result["capabilities"],
            "removed": int(plugin_result["removed"])
            + (1 if remove and skill_changed else 0),
            "_human": (
                f"{'Removed' if remove else 'Updated'} DSH components: {', '.join(sorted(c.value for c in components))}"
                if changed
                else (
                    f"Nothing to remove for DSH at {config_path}."
                    if remove
                    else f"Native j-cli DSH adapter is already up to date at {plugin_path}"
                )
            ),
        },
        ctx.use_json,
    )


def _global_dsh_config_capability(path: Path, component: Component) -> bool | None:
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
        root = yaml.compose(text)
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    if root is not None and (
        not isinstance(root, SequenceNode)
        or any(not isinstance(item, MappingNode) for item in root.value)
    ):
        return None
    matches = _marked_matches(text)
    if not matches:
        return _DSH_ROW_ID in _row_ids(root)
    try:
        loaded = yaml.safe_load(matches[0].group(0))
        row = loaded[0]
        if "insert" in row:
            row = row["insert"][0]
        config = row["config"]
    except (yaml.YAMLError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(config, dict):
        return None
    key = f"{component.value}s"
    if set(config) & {"hooks", "tools"} != {"hooks", "tools"} or any(
        not isinstance(config.get(name), bool) for name in ("hooks", "tools")
    ):
        return None
    return config[key]


def _global_dsh_plugin_is_foreign(path: Path) -> bool | None:
    if not path_exists(path):
        return False
    if not path.exists():
        return True
    try:
        return not _has_plugin_header(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None


def _warn_global_dsh_conflicts(
    scope: Scope,
    components: frozenset[Component],
    config_target: Path,
    plugin_target: Path,
    skill_target: Path,
) -> None:
    dsh_home = Path(os.environ.get("DSH_HOME") or "~/.dsh").expanduser().resolve()
    agents_home = (
        Path(os.environ.get("DSH_AGENTS_HOME") or Path.home() / ".agents")
        .expanduser()
        .resolve()
    )
    global_config = dsh_home / "cordis.patch.yml"
    global_plugin = dsh_home / "plugins" / "jcli.ts"
    hook_probes = [
        (
            global_config,
            lambda path: _global_dsh_config_capability(path, Component.HOOK),
        ),
        (global_plugin, _global_dsh_plugin_is_foreign),
    ]
    tool_probes = [
        (
            global_config,
            lambda path: _global_dsh_config_capability(path, Component.TOOL),
        ),
        (global_plugin, _global_dsh_plugin_is_foreign),
    ]
    warn_global_conflicts(
        scope,
        components,
        {
            Component.SKILL: skill_target,
            Component.HOOK: config_target,
            Component.TOOL: plugin_target,
        },
        {
            Component.SKILL: [
                (agents_home / "skills" / "j-cli", path_exists),
                (dsh_home / "skills" / "j-cli", path_exists),
            ],
            Component.HOOK: hook_probes,
            Component.TOOL: tool_probes,
        },
    )


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


def _managed_block(
    scope: Scope, plugin_path: Path, capabilities: frozenset[Component]
) -> str:
    """Build the marked YAML row for one DSH composition layer."""
    module_name = (
        "./plugins/jcli.ts" if scope != Scope.USER else str(plugin_path.resolve())
    )
    row = [
        f"- id: {_DSH_ROW_ID}",
        f"  name: {_yaml_quote(module_name)}",
        "  config:",
        f"    hooks: {str(Component.HOOK in capabilities).lower()}",
        f"    tools: {str(Component.TOOL in capabilities).lower()}",
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
    if isinstance(root, SequenceNode) and root.flow_style:
        start = root.start_mark.index
        end = root.end_mark.index
        root.flow_style = False
        rendered = yaml.serialize(root)
        text = text[:start] + rendered + text[end:]
    if not text:
        return block
    separator = "" if text.endswith("\n") else "\n"
    return text + separator + block


def _node_removal_span(
    text: str, node: Node, parent_flow_style: bool | None
) -> tuple[int, int]:
    if not parent_flow_style:
        start = text.rfind("\n", 0, node.start_mark.index) + 1
        end = node.end_mark.index
        if end < len(text) and text[end] == "\n":
            end += 1
        return start, end

    start = node.start_mark.index
    end = node.end_mark.index
    after = end
    while after < len(text) and text[after].isspace():
        after += 1
    if after < len(text) and text[after] == ",":
        end = after + 1
        while end < len(text) and text[end].isspace():
            end += 1
        return start, end
    before = start - 1
    while before >= 0 and text[before].isspace():
        before -= 1
    if before >= 0 and text[before] == ",":
        start = before
    return start, end


def _row_has_id(node: Node, row_id: str) -> bool:
    if not isinstance(node, MappingNode):
        return False
    value = _mapping_value(node, "id")
    return isinstance(value, ScalarNode) and value.value == row_id


def _remove_unmanaged_id_rows(text: str, root: Node | None) -> str:
    if not isinstance(root, SequenceNode):
        return text
    spans: list[tuple[int, int]] = []
    for item in root.value:
        if not isinstance(item, MappingNode):
            continue
        if _row_has_id(item, _DSH_ROW_ID):
            spans.append(_node_removal_span(text, item, root.flow_style))
            continue
        inserted = _mapping_value(item, "insert")
        if not isinstance(inserted, SequenceNode):
            continue
        matching = [
            child for child in inserted.value if _row_has_id(child, _DSH_ROW_ID)
        ]
        if matching and len(matching) == len(inserted.value):
            spans.append(_node_removal_span(text, item, root.flow_style))
        else:
            spans.extend(
                _node_removal_span(text, child, inserted.flow_style)
                for child in matching
            )
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    return text


def _prepare_unmanaged_text(
    path: Path, text: str, use_json: bool, force: bool
) -> tuple[str, Node | None]:
    root = _parse_yaml(path, text, use_json)
    if _DSH_ROW_ID not in _row_ids(root):
        return text, root
    if not force:
        emit_error(
            "DSH_CONFIG_CONFLICT",
            f"{path}: unmanaged row id '{_DSH_ROW_ID}' already exists",
            use_json,
        )
    cleaned = _remove_unmanaged_id_rows(text, root)
    return cleaned, _parse_yaml(path, cleaned, use_json)


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


def _validate_plugin_conflict(path: Path, use_json: bool, force: bool = False) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if force:
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
    """Warn a user install when a workspace native row may also run."""
    if scope != Scope.USER:
        return
    other = Path.cwd().resolve() / ".dsh" / "cordis.yml"
    if other.exists() and _DSH_MARKER_RE.search(_read_text(other)):
        click.echo(
            f"warning: {other} also contains the j-cli DSH row; both scopes may run duplicate hooks",
            err=True,
        )


def _configured_capabilities(
    config_path: Path, config_text: str, use_json: bool
) -> frozenset[Component]:
    matches = _marked_matches(config_text)
    if not matches:
        return frozenset()
    try:
        loaded = yaml.safe_load(matches[0].group(0))
        row = loaded[0]
        if "insert" in row:
            row = row["insert"][0]
    except (yaml.YAMLError, KeyError, IndexError, TypeError) as exc:
        emit_error(
            "DSH_CONFIG_INVALID",
            f"{config_path}: invalid managed capability row: {exc}",
            use_json,
        )
    config = row.get("config")
    if not isinstance(config, dict) or not ({"hooks", "tools"} & set(config)):
        return frozenset({Component.HOOK, Component.TOOL})
    if set(config) & {"hooks", "tools"} != {"hooks", "tools"} or any(
        not isinstance(config[key], bool) for key in ("hooks", "tools")
    ):
        emit_error(
            "DSH_CONFIG_INVALID",
            f"{config_path}: managed config hooks and tools must both be booleans",
            use_json,
        )
    return frozenset(
        component
        for component in (Component.HOOK, Component.TOOL)
        if config[f"{component.value}s"]
    )


def _install_or_remove(
    scope: Scope,
    config_path: Path,
    plugin_path: Path,
    remove: bool,
    ctx: CliContext,
    components: frozenset[Component],
    force: bool = False,
) -> dict[str, Any]:
    config_text = _read_text(config_path)
    before_capabilities = _configured_capabilities(
        config_path, config_text, ctx.use_json
    )
    if force and path_exists(plugin_path):
        current_plugin = _read_text(plugin_path)
        if not _has_plugin_header(current_plugin):
            before_capabilities = frozenset()
    chosen = components & {Component.HOOK, Component.TOOL}
    after_capabilities = (
        before_capabilities - chosen if remove else before_capabilities | chosen
    )
    removed_capabilities = len(before_capabilities - after_capabilities)
    delete_plugin = not after_capabilities
    legacy_path = _legacy_hooks_path(scope)
    legacy_before = load_dsh_legacy_settings(legacy_path, ctx.use_json)
    legacy_after, removed_legacy, has_user_content = clean_dsh_legacy_settings(
        legacy_before
    )

    if delete_plugin:
        _validate_plugin_remove(plugin_path, ctx.use_json)
        config_after, config_changed = _remove_config_block(
            config_path, config_text, ctx.use_json
        )
        plugin_changed = plugin_path.exists()
        legacy_changed = _legacy_changed(legacy_path, legacy_before, legacy_after)

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
        _warn_legacy_user_content(legacy_path, has_user_content)
        removed = (
            (1 if config_changed else 0) + (1 if plugin_changed else 0) + removed_legacy
        )
        return {"changed": bool(removed), "removed": removed, "capabilities": []}

    # Preflight every input and generated output before touching the filesystem.
    source = _versioned_plugin_source(
        _plugin_resource_source(ctx.use_json), ctx.use_json
    )
    _validate_plugin_conflict(plugin_path, ctx.use_json, force)
    existing_root = _parse_yaml(config_path, config_text, ctx.use_json)
    unmanaged_text = _remove_marked_blocks(config_text)
    cleaned_text, cleaned_root = _prepare_unmanaged_text(
        config_path, unmanaged_text, ctx.use_json, force
    )
    if cleaned_text != unmanaged_text:
        config_text = cleaned_text
        existing_root = cleaned_root
    config_after = _replace_or_append_block(
        config_text,
        _managed_block(scope, plugin_path, after_capabilities),
        existing_root,
    )
    _parse_yaml(config_path, config_after, ctx.use_json)
    plugin_changed = (
        not plugin_path.exists()
        or _read_text(plugin_path) != source
        or (force and plugin_path.is_symlink())
    )
    config_changed = config_after != config_text
    legacy_changed = _legacy_changed(legacy_path, legacy_before, legacy_after)

    # Install the adapter before declaring it in Cordis. This ensures a row that
    # DSH can observe always points at a readable native module.
    if plugin_changed:
        _atomic_replace(plugin_path, source)
    if config_changed:
        _write_text(config_path, config_after)
    if legacy_changed:
        if legacy_after:
            _write_text(legacy_path, _json_text(legacy_after))
        elif legacy_path.exists():
            legacy_path.unlink()

    _warn_legacy_user_content(legacy_path, has_user_content)
    changed = plugin_changed or config_changed or legacy_changed
    _warn_duplicate_scope(scope)
    return {
        "changed": changed,
        "removed": removed_legacy + removed_capabilities,
        "capabilities": sorted(component.value for component in after_capabilities),
    }


def _skill_target(scope: Scope, override: Path | None) -> Path:
    if scope == Scope.USER:
        agents_home = Path(os.environ.get("DSH_AGENTS_HOME") or Path.home() / ".agents")
        root = agents_home.expanduser().resolve() / "skills"
    else:
        root = Path.cwd().resolve() / ".agents" / "skills"
    return resolve_skill_target(root, override)


def _update_ignores(
    scope: Scope,
    components: frozenset[Component],
    remove: bool,
    config_path: Path,
    plugin_path: Path,
    skill_target: Path,
) -> bool:
    if scope == Scope.USER:
        return False
    enabled = scope == Scope.LOCAL and not remove
    changed = False
    if components & {Component.HOOK, Component.TOOL}:
        changed = update_managed_gitignore(
            config_path.parent,
            {
                Component.HOOK: ["/cordis.yml", "/plugins/jcli.ts"],
                Component.TOOL: ["/cordis.yml", "/plugins/jcli.ts"],
            },
            components,
            enabled,
        )
    changed = update_skill_ignore(skill_target, scope, remove, components) or changed
    return changed
