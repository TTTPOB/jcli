"""Tests for workspace output retention and cleanup."""

import json
from pathlib import Path

from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.outputs.cleanup import cleanup_outputs, retention_settings
from jupyter_jcli.outputs.store import persist_inline_outputs


def _complete_run(workspace: Path, run_id: str, created_at: float) -> Path:
    run_dir = workspace / ".j-cli" / "outputs" / run_id
    run_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "status": "complete",
        "run_id": run_id,
        "created_at": created_at,
        "cwd": str(workspace),
        "source": {"kind": "inline", "cwd": str(workspace), "run_id": run_id},
        "outputs": [{"output_type": "stream", "name": "stdout", "text": run_id}],
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_cleanup_applies_age_and_count_to_whole_runs(tmp_path):
    old = _complete_run(tmp_path, "old", 100.0)
    middle = _complete_run(tmp_path, "middle", 200.0)
    newest = _complete_run(tmp_path, "newest", 300.0)

    result = cleanup_outputs(cwd=tmp_path, days=1, max_runs=1, now=300.0)

    assert {Path(path).name for path in result.deleted_runs} == {"old", "middle"}
    assert not old.parent.exists()
    assert not middle.parent.exists()
    assert newest.parent.exists()


def test_cleanup_dry_run_is_read_only_and_cli_reports_candidates(tmp_path, monkeypatch):
    manifest = _complete_run(tmp_path, "expired", 0.0)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        main, ["--json", "output", "clean", "--dry-run", "--days", "1"]
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["deleted_runs"] == []
    assert data["would_delete_runs"] == [str(manifest.parent)]
    assert manifest.is_file()


def test_cleanup_preserves_active_unknown_and_linked_runs(tmp_path):
    root = tmp_path / ".j-cli" / "outputs"
    active = root / "active"
    active.mkdir(parents=True)
    (active / ".manifest.json.tmp").write_text("publishing", encoding="utf-8")
    unknown = _complete_run(tmp_path, "unknown", 0.0).parent
    (unknown / "foreign.txt").write_text("keep", encoding="utf-8")
    target = tmp_path / "outside"
    target.mkdir()
    linked = root / "linked"
    linked.symlink_to(target, target_is_directory=True)

    result = cleanup_outputs(cwd=tmp_path, days=1, max_runs=1, now=200_000.0)

    assert active.exists()
    assert unknown.exists()
    assert linked.is_symlink()
    reasons = " ".join(item["reason"] for item in result.skipped_runs)
    assert "state-unknown" in reasons
    assert "unknown run files" in reasons
    assert "linked" in reasons


def test_automatic_cleanup_protects_current_run_and_removes_old(tmp_path, monkeypatch):
    monkeypatch.setenv("JCLI_OUTPUT_MAX_RUNS", "1")
    monkeypatch.setenv("JCLI_OUTPUT_RETENTION_DAYS", "7")
    rich = [
        {
            "output_type": "display_data",
            "data": {"text/html": "<b>saved</b>"},
            "metadata": {},
        }
    ]
    old = persist_inline_outputs(
        rich, cwd=tmp_path, clock=lambda: 100.0, uuid_factory=lambda: "old"
    )
    current = persist_inline_outputs(
        rich, cwd=tmp_path, clock=lambda: 200.0, uuid_factory=lambda: "current"
    )

    assert old is not None and current is not None
    assert not old.manifest_path.exists()
    assert current.manifest_path.is_file()


def test_show_reports_output_not_found_after_cleanup(tmp_path):
    manifest = _complete_run(tmp_path, "expired", 0.0)
    cleanup_outputs(cwd=tmp_path, days=1, max_runs=50, now=200_000.0)

    result = CliRunner().invoke(main, ["--json", "output", "show", str(manifest)])

    assert result.exit_code == 1
    assert json.loads(result.output)["code"] == "OUTPUT_NOT_FOUND"


def test_positive_environment_overrides(monkeypatch):
    monkeypatch.setenv("JCLI_OUTPUT_RETENTION_DAYS", "3")
    monkeypatch.setenv("JCLI_OUTPUT_MAX_RUNS", "9")
    assert retention_settings() == (3, 9)
