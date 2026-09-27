"""Fresh-process checks for lightweight ordinary hook paths."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("hook", "platform", "payload"),
    [
        ("pair-drift-guard-pre", "dsh", {"tool_input": {"file_path": "notes.txt"}}),
        ("pair-drift-guard-post", "dsh", {"tool_input": {"file_path": "orphan.py"}}),
        ("pair-drift-guard-pre", "claude", {"tool_input": {"file_path": "orphan.py"}}),
        ("pair-drift-guard-post", "claude", {"tool_input": {"file_path": "notes.txt"}}),
        (
            "pair-drift-guard-pre",
            "codex",
            {
                "tool_name": "apply_patch",
                "tool_input": {
                    "command": [
                        "apply_patch",
                        "*** Begin Patch\n*** Update File: notes.txt\n@@ -1 +1 @@\n- old\n+ new\n*** Update File: orphan.py\n@@ -1 +1 @@\n- old\n+ new\n*** End Patch",
                    ]
                },
            },
        ),
        (
            "pair-drift-guard-post",
            "codex",
            {
                "tool_name": "apply_patch",
                "tool_input": {
                    "command": [
                        "apply_patch",
                        "*** Begin Patch\n*** Update File: orphan.py\n@@ -1 +1 @@\n- old\n+ new\n*** End Patch",
                    ]
                },
            },
        ),
        ("notebook-exec-guard", "claude", {"tool_input": {"command": "echo ok"}}),
        (
            "python-run-guard",
            "codex",
            {"tool_input": {"command": ["bash", "-c", "python orphan.py"]}},
        ),
    ],
)
def test_noop_hooks_do_not_import_heavy_dependencies(
    tmp_path: Path, hook, platform, payload
):
    (tmp_path / "notes.txt").write_text("notes", encoding="utf-8")
    (tmp_path / "orphan.py").write_text("print('ok')", encoding="utf-8")
    code = """
import json
import sys
from click.testing import CliRunner
from jupyter_jcli.cli import main

result = CliRunner().invoke(
    main, ['_hooks', HOOK, '--platform', PLATFORM], input=json.dumps(PAYLOAD), catch_exceptions=False
)
assert result.exit_code == 0, (result.exit_code, result.output)
blocked = ('jupyter_server_client', 'nbformat', 'jupyter_jcli.diff')
loaded = sorted(name for name in sys.modules if any(
    name == prefix or name.startswith(prefix + '.') for prefix in blocked
))
assert not loaded, loaded
"""
    script = f"HOOK = {hook!r}\nPLATFORM = {platform!r}\nPAYLOAD = {payload!r}\n" + code
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cli_context_server_is_lazy_cached_and_injectable(monkeypatch):
    from jupyter_jcli import server as server_module
    from jupyter_jcli.cli import CliContext
    from jupyter_jcli.config import AppConfig

    config = AppConfig("http://localhost:8888", "token", Path("/tmp"))
    created = []

    def fake_client(url, token):
        client = object()
        created.append((url, token, client))
        return client

    monkeypatch.setattr(server_module, "ServerClient", fake_client)
    ctx = CliContext(config=config, use_json=False)
    assert created == []
    assert ctx.server is ctx.server
    assert created == [(config.server_url, config.token, ctx.server)]

    injected = object()
    other = CliContext(config=config, use_json=True, server=injected)
    assert other.server is injected
    assert len(created) == 1
