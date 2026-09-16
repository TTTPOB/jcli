"""Tests for workspace-scoped inline output persistence."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.outputs.store import OutputStoreError, persist_inline_outputs

_FIXTURE = Path(__file__).parent / "fixtures" / "outputs" / "mixed_outputs.json"


def test_short_text_does_not_create_output_cache(tmp_path):
    stored = persist_inline_outputs(
        [{"output_type": "stream", "name": "stdout", "text": "short"}],
        cwd=tmp_path,
    )

    assert stored is None
    assert not (tmp_path / ".j-cli").exists()


def test_rich_outputs_preserve_order_metadata_and_real_image_paths(tmp_path):
    raw_outputs = json.loads(_FIXTURE.read_text(encoding="utf-8"))

    stored = persist_inline_outputs(raw_outputs, cwd=tmp_path)

    assert stored is not None
    assert stored.manifest_path.parent.parent.parent == tmp_path / ".j-cli"
    manifest = json.loads(stored.manifest_path.read_text(encoding="utf-8"))
    assert [output["output_type"] for output in manifest["outputs"]] == [
        "stream",
        "display_data",
        "execute_result",
        "error",
    ]
    bundle = manifest["outputs"][1]
    assert bundle["metadata"] == {"display": {"isolated": True}}
    assert list(bundle["data"]) == list(raw_outputs[1]["data"])
    for mime in ("image/png", "image/jpeg"):
        reference = bundle["data"][mime]
        image_path = Path(reference["path"])
        assert reference["type"] == "file"
        assert reference["mime"] == mime
        assert image_path.is_absolute()
        assert image_path.is_file()
    manifest_text = stored.manifest_path.read_text(encoding="utf-8")
    assert raw_outputs[1]["data"]["image/png"] not in manifest_text
    assert raw_outputs[1]["data"]["image/jpeg"] not in manifest_text
    assert Path(stored.outputs[1]["path"]).is_file()


def test_output_show_lists_and_reads_all_mime_data(tmp_path):
    raw_outputs = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    stored = persist_inline_outputs(raw_outputs, cwd=tmp_path)
    assert stored is not None
    runner = CliRunner()

    listed = runner.invoke(
        main, ["--json", "output", "show", str(stored.manifest_path)]
    )
    read_jpeg = runner.invoke(
        main,
        [
            "--json",
            "output",
            "show",
            str(stored.manifest_path),
            "--output",
            "1",
            "--mime",
            "image/jpeg",
        ],
    )
    read_json = runner.invoke(
        main,
        [
            "--json",
            "output",
            "show",
            str(stored.manifest_path),
            "--output",
            "2",
            "--mime",
            "application/json",
        ],
    )

    assert listed.exit_code == 0
    assert [item["output_index"] for item in json.loads(listed.output)["outputs"]] == [
        0,
        1,
        2,
        3,
    ]
    jpeg = json.loads(read_jpeg.output)
    assert jpeg["selected"]["data"] == raw_outputs[1]["data"]["image/jpeg"]
    assert json.loads(read_json.output)["selected"]["data"] == {"answer": 42}


def test_inline_save_failure_reports_executed_without_retry(live_session, monkeypatch):
    runner = CliRunner()
    execute_result = {
        "status": "ok",
        "outputs": [
            {
                "output_type": "display_data",
                "data": {"text/html": "<b>done</b>"},
                "metadata": {},
            }
        ],
    }
    with (
        patch(
            "jupyter_jcli.kernel.execute_code", return_value=execute_result
        ) as execute,
        patch(
            "jupyter_jcli.outputs.store.Path.mkdir",
            side_effect=OSError("read only"),
        ),
    ):
        result = runner.invoke(
            main,
            [
                "-s",
                live_session["url"],
                "-t",
                live_session["token"],
                "--json",
                "exec",
                live_session["session_id"],
                "--code",
                "display('done')",
            ],
        )

    assert result.exit_code == 1
    error = json.loads(result.output)
    assert error["code"] == "OUTPUT_SAVE_FAILED"
    assert "Execution completed" in error["message"]
    assert ".j-cli/outputs" not in error["message"]
    assert error["outputs"][0]["type"] == "html"
    execute.assert_called_once()


def test_many_short_outputs_are_persisted_and_share_one_summary_budget(tmp_path):
    raw_outputs = [
        {"output_type": "stream", "name": "stdout", "text": str(index)}
        for index in range(30)
    ]

    stored = persist_inline_outputs(raw_outputs, cwd=tmp_path)

    assert stored is not None
    assert len(stored.outputs) <= 21
    notice = stored.outputs[-1]
    assert notice["type"] == "summary_notice"
    assert notice["truncated"] is True
    assert notice["omitted_items"] == 10
    assert notice["complete_outputs"] == str(stored.manifest_path)
    manifest = json.loads(stored.manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["outputs"]) == 30


def test_text_budget_is_shared_across_outputs(tmp_path):
    raw_outputs = [
        {"output_type": "stream", "name": "stdout", "text": "a" * 3_000},
        {"output_type": "stream", "name": "stdout", "text": "b" * 3_000},
    ]

    stored = persist_inline_outputs(raw_outputs, cwd=tmp_path)

    assert stored is not None
    assert len(stored.outputs[0]["text"]) == 3_000
    assert stored.outputs[1]["text"].startswith("b" * 900)
    assert stored.outputs[1]["text"].endswith("...[truncated]")
    assert stored.outputs[-1]["type"] == "summary_notice"
    assert stored.outputs[-1]["complete_outputs"] == str(stored.manifest_path)


def test_storage_rejects_symlinked_workspace_metadata(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".j-cli").symlink_to(outside, target_is_directory=True)
    rich = [
        {
            "output_type": "display_data",
            "data": {"text/html": "<b>saved</b>"},
            "metadata": {},
        }
    ]

    with pytest.raises(OutputStoreError) as failure:
        persist_inline_outputs(rich, cwd=tmp_path)

    assert ".j-cli is not a real directory" in str(failure.value)
    assert not (outside / "outputs").exists()
