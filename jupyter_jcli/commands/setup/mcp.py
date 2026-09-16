"""Managed notebook-output MCP configuration for setup commands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from jupyter_jcli.output import emit_error

from .common import Scope

_MCP_NAME = "jcli-notebook-output"
_MCP_COMMAND = "j-cli"
_MCP_BASE_ARGS = ["mcp", "serve"]


def manage_claude_mcp(
    scope: str, project_root: Path, remove: bool, use_json: bool
) -> str:
    """Install or remove the managed Claude MCP entry via Claude's CLI."""
    resolved_scope = Scope(scope)
    root = project_root.resolve()
    expected_args = _expected_args(resolved_scope, root)
    entry = _read_claude_entry(resolved_scope, root, use_json)

    if entry is not None and not _is_managed_entry(entry, expected_args):
        emit_error(
            "MCP_NAME_CONFLICT",
            f"Claude MCP server {_MCP_NAME!r} already exists in {resolved_scope.value} "
            "scope with a different command; remove or rename it before retrying.",
            use_json,
        )

    if remove:
        if entry is None:
            return "noop"
        _run_claude(
            ["claude", "mcp", "remove", "--scope", resolved_scope.value, _MCP_NAME],
            root,
            use_json,
        )
        return "removed"

    if entry is not None:
        return "unchanged"

    command = [
        "claude",
        "mcp",
        "add",
        "--scope",
        resolved_scope.value,
        _MCP_NAME,
        "--",
        _MCP_COMMAND,
        *expected_args,
    ]
    _run_claude(command, root, use_json)
    return "installed"


def _expected_args(scope: Scope, root: Path) -> list[str]:
    args = list(_MCP_BASE_ARGS)
    if scope != Scope.USER:
        args.extend(["--root", str(root)])
    return args


def _is_managed_entry(entry: object, expected_args: list[str]) -> bool:
    if not isinstance(entry, dict):
        return False
    entry_type = entry.get("type", "stdio")
    env = entry.get("env", {})
    return (
        entry_type == "stdio"
        and entry.get("command") == _MCP_COMMAND
        and entry.get("args") == expected_args
        and (env is None or env == {})
    )


def _read_claude_entry(scope: Scope, root: Path, use_json: bool) -> object | None:
    if scope == Scope.PROJECT:
        config = _load_json(root / ".mcp.json", use_json)
        return _named_entry(config)

    config = _load_json(Path.home() / ".claude.json", use_json)
    if scope == Scope.USER:
        return _named_entry(config)

    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        return None
    for key in _claude_project_keys(root):
        project = projects.get(key)
        if isinstance(project, dict):
            entry = _named_entry(project)
            if entry is not None:
                return entry
    return None


def _claude_project_keys(root: Path) -> list[str]:
    """Return project keys Claude may use for a regular or linked worktree."""
    keys = [str(root)]
    git_file = root / ".git"
    if not git_file.is_file():
        return keys
    try:
        marker = git_file.read_text(encoding="utf-8").strip()
    except OSError:
        return keys
    if not marker.startswith("gitdir:"):
        return keys
    git_dir = Path(marker.removeprefix("gitdir:").strip())
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    parts = git_dir.parts
    try:
        index = parts.index(".git")
    except ValueError:
        return keys
    common_root = Path(*parts[:index])
    common_key = str(common_root)
    if common_key not in keys:
        keys.append(common_key)
    return keys


def _named_entry(config: dict[str, Any]) -> object | None:
    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict):
        return None
    return servers.get(_MCP_NAME)


def _load_json(path: Path, use_json: bool) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        emit_error("MCP_CONFIG_INVALID", f"{path}: {exc}", use_json)
    if not isinstance(value, dict):
        emit_error("MCP_CONFIG_INVALID", f"{path}: expected a JSON object", use_json)
    return value


def _run_claude(command: list[str], cwd: Path, use_json: bool) -> None:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        emit_error(
            "CLAUDE_CLI_NOT_FOUND",
            "Claude Code CLI was not found; install it before running setup claude.",
            use_json,
        )
    except OSError as exc:
        emit_error(
            "MCP_SETUP_FAILED", f"Could not run Claude Code CLI: {exc}", use_json
        )

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "unknown Claude CLI error"
        if "already exists" in detail:
            emit_error(
                "MCP_NAME_CONFLICT",
                f"Claude MCP server {_MCP_NAME!r} already exists but its managed "
                f"configuration could not be verified: {detail}",
                use_json,
            )
        emit_error("MCP_SETUP_FAILED", detail, use_json)
