"""Shared prefilter behavior and the standard-library-only native entry point."""

import errno
import io
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from jupyter_jcli.pair_guard import needs_pair_guard

FIXTURE = Path(__file__).parent / "fixtures" / "pair_guard.json"
ROOT = Path(__file__).parents[1]


def test_shared_python_fixtures(tmp_path):
    (tmp_path / "nested").mkdir()
    for file in ["pair.ipynb", "nested/pair.ipynb", "blocked"]:
        (tmp_path / file).touch()
    (tmp_path / "loop.ipynb").symlink_to("loop.ipynb")
    cases = json.loads(FIXTURE.read_text().replace("$ROOT", str(tmp_path)))
    for case in cases:
        for phase in ["pre", "post"]:
            assert needs_pair_guard(case["payload"], phase) is case[phase], case["name"]


def test_pre_post_do_not_cache(tmp_path):
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "new.py"},
        "cwd": str(tmp_path),
    }
    assert not needs_pair_guard(payload, "pre")
    (tmp_path / "new.ipynb").touch()
    assert needs_pair_guard(payload, "post")
    (tmp_path / "new.ipynb").unlink()
    assert not needs_pair_guard(payload, "pre")


@pytest.mark.parametrize("code", [errno.EACCES, errno.EIO, errno.ELOOP])
def test_stat_errors_delegate(monkeypatch, code):
    def fail(_path):
        raise OSError(code, "stat failed")

    monkeypatch.setattr(Path, "stat", fail)
    assert needs_pair_guard(
        {"tool_name": "Edit", "tool_input": {"file_path": "x.py"}}, "pre"
    )


def test_shared_javascript_fixtures():
    if not shutil.which("node"):
        pytest.skip("Node.js is required")
    result = subprocess.run(
        ["node", "--test", "tests/js/pair_guard.test.mjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("platform", ["claude", "codex"])
def test_entry_skip_imports_only_stdlib(tmp_path, platform):
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "bare.py"},
        "cwd": str(tmp_path),
    }
    if platform == "codex":
        payload = {
            "tool_name": "apply_patch",
            "tool_input": {"command": ["apply_patch", "*** Add File: bare.py"]},
            "cwd": str(tmp_path),
        }
    code = """
import sys
from jupyter_jcli.hook_entry import main
main()
assert 'click' not in sys.modules
assert 'jupyter_jcli.cli' not in sys.modules
assert 'jupyter_jcli.config' not in sys.modules
assert 'jupyter_jcli.commands.hooks' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code, "pair-drift-guard-pre", "--platform", platform],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "raw",
    ["{bad json", "{}", '{"tool_name":"Edit","tool_input":{"file_path":"book.ipynb"}}'],
)
def test_entry_delegates_in_process_and_replays_exact_stdin(monkeypatch, raw):
    from jupyter_jcli.hook_entry import main

    def old_cli(**kwargs):
        assert sys.stdin.read() == raw
        assert kwargs["args"] == [
            "_hooks",
            "pair-drift-guard-pre",
            "--platform",
            "claude",
        ]
        print("original protocol")
        raise SystemExit(2)

    monkeypatch.setitem(
        sys.modules, "jupyter_jcli.cli", types.SimpleNamespace(main=old_cli)
    )
    monkeypatch.setattr(
        sys, "argv", ["j-cli-hook", "pair-drift-guard-pre", "--platform", "claude"]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **kw: pytest.fail("must not spawn")
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "raw",
    ["{bad json", "{}", '{"tool_name":"Edit","tool_input":{"file_path":"book.ipynb"}}'],
)
def test_entry_preserves_real_guard_protocol(raw):
    entry = subprocess.run(
        [sys.executable, "-m", "jupyter_jcli.hook_entry", "pair-drift-guard-pre"],
        input=raw,
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    original = subprocess.run(
        [sys.executable, "-m", "jupyter_jcli", "_hooks", "pair-drift-guard-pre"],
        input=raw,
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert (entry.returncode, entry.stdout, entry.stderr) == (
        original.returncode,
        original.stdout,
        original.stderr,
    )


def test_old_extractors_share_session_and_tool_cwd(tmp_path):
    from jupyter_jcli.commands.hooks.payload import (
        _extract_dsh_file_path,
        _extract_file_path_claude,
        _extract_file_paths_codex,
    )

    payload = {
        "tool_name": "Edit",
        "cwd": str(tmp_path),
        "tool_input": {"file_path": "pair.py", "workdir": "nested"},
    }
    expected = str(tmp_path / "nested" / "pair.py")
    assert _extract_file_path_claude(payload) == expected
    assert _extract_dsh_file_path(payload) == expected
    payload["tool_name"] = "apply_patch"
    payload["tool_input"]["command"] = [
        "apply_patch",
        "*** Update File: pair.py\n*** Move to: target.py",
    ]
    assert _extract_file_paths_codex(payload) == [
        expected,
        str(tmp_path / "nested" / "target.py"),
    ]


@pytest.mark.parametrize(
    "options", [["--platform", "unknown"], ["--help"], ["--debug"], ["--unknown"]]
)
def test_entry_does_not_hide_cli_options(tmp_path, options):
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "bare.py"},
        "cwd": str(tmp_path),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "jupyter_jcli.hook_entry",
            "pair-drift-guard-pre",
            *options,
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    if options == ["--help"]:
        assert result.returncode == 0
        assert "--platform" in result.stdout
    elif options == ["--debug"]:
        assert result.returncode == 0
        assert result.stderr == ""
    else:
        assert result.returncode == 2
        assert "Error:" in result.stderr


def test_resolution_errors_delegate(monkeypatch):
    import jupyter_jcli.pair_guard as guard

    def fail(*_args):
        raise OSError(errno.ENOENT, "cwd disappeared")

    monkeypatch.setattr(guard.os, "getcwd", fail)
    assert guard.needs_pair_guard(
        {"tool_name": "Edit", "tool_input": {"file_path": "bare.py"}}, "pre"
    )


def test_dsh_bash_cwd_and_shell_guards(tmp_path):
    from click.testing import CliRunner

    from jupyter_jcli.cli import main
    from jupyter_jcli.commands.hooks.payload import _extract_dsh_bash_command_and_cwd

    payload = {
        "tool_name": "bash",
        "cwd": str(tmp_path),
        "tool_input": {
            "command": "jupyter nbconvert --execute book.ipynb",
            "workdir": "nested",
        },
    }
    assert _extract_dsh_bash_command_and_cwd(payload) == (
        payload["tool_input"]["command"],
        str(tmp_path / "nested"),
    )
    result = CliRunner().invoke(
        main,
        ["_hooks", "notebook-exec-guard", "--platform", "dsh"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 2
    assert "deny" in result.output
