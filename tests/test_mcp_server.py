"""Tests for the read-only saved-output MCP server."""

from __future__ import annotations

import asyncio
import base64
import builtins
import json
import os
import sys
import threading
from pathlib import Path

import nbformat
import pytest
from click.testing import CliRunner
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from jupyter_jcli import mcp_server
from jupyter_jcli.commands.mcp import serve
from jupyter_jcli.mcp_server import (
    _authorize_pair,
    _client_roots,
    _mcp_result,
    _path_from_root_uri,
    _read_output,
    _resolve_allowed_path,
)
from jupyter_jcli.outputs import MAX_TRANSPORT_BYTES, OutputProtocolError

_FIXTURE = Path(__file__).parent / "fixtures" / "outputs" / "mixed_outputs.json"


def _write_notebook(path: Path) -> Path:
    outputs = [nbformat.from_dict(item) for item in json.loads(_FIXTURE.read_text())]
    cell = nbformat.v4.new_code_cell("display()", outputs=outputs)
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), path)
    return path


@pytest.mark.asyncio
async def test_stdio_handshake_and_saved_output_content_types(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    notebook = _write_notebook(root / "mixed.ipynb")
    outside = _write_notebook(tmp_path / "outside.ipynb")
    stderr_path = tmp_path / "server.stderr"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "jupyter_jcli",
            "mcp",
            "serve",
            "--root",
            str(root),
        ],
        env=dict(os.environ),
    )

    stderr = stderr_path.open("w", encoding="utf-8")
    async with stdio_client(parameters, errlog=stderr) as streams:  # noqa: SIM117
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert [tool.name for tool in tools.tools] == ["read_notebook_output"]
            assert tools.tools[0].annotations is not None
            assert tools.tools[0].annotations.readOnlyHint is True

            directory = await session.call_tool(
                "read_notebook_output",
                {"file_path": "mixed.ipynb", "cell_index": 0},
            )
            assert directory.isError is False
            assert directory.content == []
            assert directory.structuredContent is not None
            assert [
                item["output_index"] for item in directory.structuredContent["outputs"]
            ] == [0, 1, 2, 3]

            html = await session.call_tool(
                "read_notebook_output",
                {
                    "file_path": str(notebook),
                    "cell_index": 0,
                    "output_index": 1,
                    "mime_type": "text/html",
                },
            )
            assert html.isError is False
            assert html.structuredContent is not None
            assert "data" not in html.structuredContent["selected"]
            assert html.content == [
                types.TextContent(type="text", text="<strong>raw html</strong>")
            ]

            image = await session.call_tool(
                "read_notebook_output",
                {
                    "file_path": str(notebook),
                    "cell_index": 0,
                    "output_index": 1,
                },
            )
            assert image.isError is False
            assert image.structuredContent is None
            assert len(image.content) == 1
            assert isinstance(image.content[0], types.ImageContent)
            assert image.content[0].mimeType == "image/png"
            assert image.content[0].data == "iVBORw0KGgo="

            structured_json = await session.call_tool(
                "read_notebook_output",
                {
                    "file_path": str(notebook),
                    "cell_index": 0,
                    "output_index": 2,
                    "mime_type": "application/json",
                },
            )
            assert structured_json.isError is False
            assert structured_json.structuredContent is not None
            assert structured_json.content == []
            assert structured_json.structuredContent["selected"]["data"] == {
                "answer": 42
            }

            saved_error = await session.call_tool(
                "read_notebook_output",
                {
                    "file_path": str(notebook),
                    "cell_index": 0,
                    "output_index": 3,
                },
            )
            assert saved_error.isError is False
            assert saved_error.structuredContent["output_type"] == "error"
            assert saved_error.structuredContent["error"]["ename"] == "ValueError"

            denied = await session.call_tool(
                "read_notebook_output",
                {"file_path": str(outside), "cell_index": 0},
            )
            assert denied.isError is True
            assert json.loads(denied.content[0].text)["code"] == "PATH_NOT_ALLOWED"

    stderr.close()
    assert stderr_path.read_text(encoding="utf-8") == ""


def test_path_policy_rejects_ambiguous_relative_and_escaped_pair(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    outside = tmp_path / "outside"
    for directory in (first, second, outside):
        directory.mkdir()
    (first / "same.ipynb").touch()
    (second / "same.ipynb").touch()

    with pytest.raises(OutputProtocolError) as ambiguous:
        _resolve_allowed_path("same.ipynb", (first, second))
    assert ambiguous.value.code == "PATH_AMBIGUOUS"

    py_path = first / "report.py"
    py_path.write_text("# %%\n1\n", encoding="utf-8")
    outside_notebook = outside / "report.ipynb"
    outside_notebook.touch()
    (first / "report.ipynb").symlink_to(outside_notebook)
    with pytest.raises(OutputProtocolError) as denied:
        _authorize_pair(py_path, (first,))
    assert denied.value.code == "PATH_NOT_ALLOWED"


def test_client_root_uri_decoding_and_missing_roots_guidance(tmp_path: Path) -> None:
    root = tmp_path / "space root"
    root.mkdir()
    assert _path_from_root_uri(root.as_uri()) == root.resolve()

    class Session:
        async def list_roots(self):
            raise RuntimeError("unsupported")

    class Context:
        session = Session()

    class FakeServer:
        request_context = Context()

    with pytest.raises(OutputProtocolError) as missing:
        asyncio.run(_client_roots(FakeServer()))
    assert missing.value.code == "ROOTS_REQUIRED"
    assert "--root" in missing.value.message


def test_mcp_result_never_repeats_image_base64() -> None:
    result = {
        "selected": {
            "mime_type": "image/png",
            "encoding": "base64",
            "data": "iVBORw0KGgo=",
        }
    }
    content = _mcp_result(result)
    assert isinstance(content, list)
    assert len(content) == 1
    assert isinstance(content[0], types.ImageContent)
    assert base64.b64decode(content[0].data).startswith(b"\x89PNG")


def test_mcp_result_enforces_final_transport_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "MAX_TRANSPORT_BYTES", 64)
    with pytest.raises(OutputProtocolError) as too_large:
        _mcp_result({"status": "ok", "outputs": [{"value": "x" * 64}]})
    assert too_large.value.code == "OUTPUT_TOO_LARGE"


@pytest.mark.asyncio
async def test_read_is_cancellable_and_keeps_transport_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    captured: dict[str, object] = {}

    def slow_reader(*args, **kwargs):
        captured.update(kwargs)
        started.set()
        release.wait(timeout=2)
        return {"status": "ok", "outputs": []}

    monkeypatch.setattr("jupyter_jcli.mcp_server.list_notebook_outputs", slow_reader)
    task = asyncio.create_task(
        _read_output(tmp_path / "notebook.ipynb", {"cell_index": 0})
    )
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert captured["max_transport_bytes"] == MAX_TRANSPORT_BYTES


def test_cli_rejects_relative_root_and_guides_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative = CliRunner().invoke(serve, ["--root", "relative"])
    assert relative.exit_code == 2
    assert "must be an absolute path" in relative.output

    real_import = builtins.__import__

    def import_without_mcp_server(name, *args, **kwargs):
        if name == "jupyter_jcli.mcp_server":
            raise ModuleNotFoundError("No module named 'mcp'", name="mcp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_mcp_server)
    missing = CliRunner().invoke(serve, ["--root", str(tmp_path)])
    assert missing.exit_code == 1
    assert "reinstall jupyter-jcli" in missing.output
