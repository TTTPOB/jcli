"""Read-only MCP server for saved notebook outputs."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from copy import deepcopy
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import anyio
import mcp.server.stdio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from jupyter_jcli.outputs import (
    DEFAULT_TEXT_LIMIT,
    MAX_TRANSPORT_BYTES,
    RASTER_MIME_TYPES,
    OutputProtocolError,
)
from jupyter_jcli.outputs.notebook import (
    list_notebook_outputs,
)
from jupyter_jcli.outputs.notebook import (
    read_notebook_output as read_saved_output,
)
from jupyter_jcli.parser import find_pair

_TOOL_NAME = "read_notebook_output"
_ROOTS_REQUIRED = (
    "No trusted filesystem roots are available. Configure this MCP server with "
    "one or more absolute --root paths, or use a client that provides MCP roots."
)


def create_server(explicit_roots: Sequence[Path] = ()) -> Server:
    """Create a server constrained to explicit roots or trusted client roots."""
    roots = tuple(root.resolve() for root in explicit_roots)
    server = Server("jupyter-jcli")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=_TOOL_NAME,
                description=(
                    "Read a saved output from a .ipynb file or paired py:percent "
                    "file without executing a kernel or modifying either file. Omit "
                    "output_index to list the cell's saved outputs."
                ),
                inputSchema=_input_schema(),
                annotations=types.ToolAnnotations(
                    title="Read saved notebook output",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            )
        ]

    @server.call_tool(validate_input=True)
    async def call_tool(
        name: str, arguments: dict[str, Any]
    ) -> Sequence[types.ContentBlock] | tuple[list[types.ContentBlock], dict[str, Any]]:
        if name != _TOOL_NAME:
            raise ValueError(f"Unknown tool: {name}")
        try:
            allowed_roots = roots or await _client_roots(server)
            file_path = _resolve_allowed_path(arguments["file_path"], allowed_roots)
            _authorize_pair(file_path, allowed_roots)
            result = await _read_output(file_path, arguments)
            return _mcp_result(result)
        except OutputProtocolError as error:
            raise ValueError(_error_text(error.code, error.message)) from error
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as error:
            raise ValueError(
                _error_text("NOTEBOOK_OUTPUT_READ_FAILED", str(error))
            ) from error

    return server


async def serve_stdio(explicit_roots: Sequence[Path] = ()) -> None:
    """Run the MCP server on stdio without writing protocol data elsewhere."""
    server = create_server(explicit_roots)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="jupyter-jcli",
                server_version=version("jupyter-jcli"),
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


async def _client_roots(server: Server) -> tuple[Path, ...]:
    try:
        result = await server.request_context.session.list_roots()
    except Exception as error:
        raise OutputProtocolError("ROOTS_REQUIRED", _ROOTS_REQUIRED) from error
    roots = tuple(_path_from_root_uri(str(root.uri)) for root in result.roots)
    if not roots:
        raise OutputProtocolError("ROOTS_REQUIRED", _ROOTS_REQUIRED)
    return roots


def _path_from_root_uri(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.query or parsed.fragment:
        raise OutputProtocolError(
            "ROOT_INVALID", f"Client root must be a local file URI: {uri}"
        )
    if parsed.netloc not in ("", "localhost"):
        raise OutputProtocolError(
            "ROOT_INVALID", f"Client root must not use a remote host: {uri}"
        )
    raw_path = unquote(parsed.path)
    if os.name == "nt" and len(raw_path) >= 3 and raw_path[0] == "/":
        raw_path = raw_path[1:]
    path = Path(raw_path).resolve()
    if not path.exists():
        raise OutputProtocolError("ROOT_INVALID", f"Client root does not exist: {path}")
    return path


def _resolve_allowed_path(file_path: str, roots: Sequence[Path]) -> Path:
    requested = Path(file_path).expanduser()
    if requested.is_absolute():
        resolved = requested.resolve()
        _require_allowed(resolved, roots)
        return resolved

    directory_roots = tuple(root for root in roots if root.is_dir())
    if len(directory_roots) == 1:
        resolved = (directory_roots[0] / requested).resolve()
        _require_allowed(resolved, roots)
        return resolved

    candidates = [
        candidate
        for root in directory_roots
        if (candidate := (root / requested).resolve()).is_file()
        and _is_within(candidate, root)
    ]
    unique = tuple(dict.fromkeys(candidates))
    if len(unique) != 1:
        raise OutputProtocolError(
            "PATH_AMBIGUOUS",
            "Relative file_path must resolve to exactly one trusted root; use an "
            "absolute path or configure a single --root",
        )
    return unique[0]


def _require_allowed(path: Path, roots: Sequence[Path]) -> None:
    if not any(_is_within(path, root) for root in roots):
        raise OutputProtocolError(
            "PATH_NOT_ALLOWED", f"Path is outside the trusted roots: {path}"
        )


def _is_within(path: Path, root: Path) -> bool:
    return path == root or (root.is_dir() and path.is_relative_to(root))


def _authorize_pair(file_path: Path, roots: Sequence[Path]) -> None:
    if file_path.suffix != ".py":
        return
    pair = find_pair(file_path)
    if pair is not None:
        _require_allowed(pair.resolve(), roots)


async def _read_output(file_path: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    output_index = arguments.get("output_index")
    if output_index is None:
        function = partial(
            list_notebook_outputs,
            file_path,
            arguments["cell_index"],
            max_transport_bytes=MAX_TRANSPORT_BYTES,
        )
    else:
        function = partial(
            read_saved_output,
            file_path,
            arguments["cell_index"],
            output_index,
            mime_type=arguments.get("mime_type"),
            offset=arguments.get("offset", 0),
            limit=arguments.get("limit", DEFAULT_TEXT_LIMIT),
            max_transport_bytes=MAX_TRANSPORT_BYTES,
        )
    return await anyio.to_thread.run_sync(function, abandon_on_cancel=True)


def _mcp_result(
    result: dict[str, Any],
) -> Sequence[types.ContentBlock] | tuple[list[types.ContentBlock], dict[str, Any]]:
    selected = result.get("selected")
    if isinstance(selected, dict):
        mime_type = selected.get("mime_type")
        if mime_type in RASTER_MIME_TYPES:
            content = [
                types.ImageContent(
                    type="image",
                    data=selected["data"],
                    mimeType=mime_type,
                )
            ]
            return _enforce_mcp_budget(content)
        if selected.get("encoding") == "utf-8":
            metadata = deepcopy(result)
            del metadata["selected"]["data"]
            content = [types.TextContent(type="text", text=selected["data"])]
            return _enforce_mcp_budget(content, metadata)

    stream = result.get("stream")
    if isinstance(stream, dict) and isinstance(stream.get("data"), str):
        metadata = deepcopy(result)
        del metadata["stream"]["data"]
        content = [types.TextContent(type="text", text=stream["data"])]
        return _enforce_mcp_budget(content, metadata)

    return _enforce_mcp_budget([], result)


def _enforce_mcp_budget(
    content: list[types.ContentBlock], structured: dict[str, Any] | None = None
) -> Sequence[types.ContentBlock] | tuple[list[types.ContentBlock], dict[str, Any]]:
    response = types.CallToolResult(content=content, structuredContent=structured)
    size = len(response.model_dump_json(exclude_none=True).encode("utf-8"))
    if size > MAX_TRANSPORT_BYTES:
        raise OutputProtocolError(
            "OUTPUT_TOO_LARGE",
            f"Serialized MCP result is {size} bytes; limit is "
            f"{MAX_TRANSPORT_BYTES} bytes",
        )
    if structured is None:
        return content
    return content, structured


def _error_text(code: str, message: str) -> str:
    return json.dumps(
        {"status": "error", "code": code, "message": message},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": (
                    "Absolute path, or a path relative to one unambiguous trusted root."
                ),
            },
            "cell_index": {"type": "integer", "minimum": 0},
            "output_index": {"type": "integer", "minimum": 0},
            "mime_type": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {
                "type": ["integer", "null"],
                "minimum": 1,
                "default": DEFAULT_TEXT_LIMIT,
            },
        },
        "required": ["file_path", "cell_index"],
        "additionalProperties": False,
    }
