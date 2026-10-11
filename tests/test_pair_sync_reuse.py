"""Behavioral boundaries for operation-local pair synchronization reuse."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import nbformat
import pytest

from jupyter_jcli.diff import Conflict, InSync, Merged, check_drift
from jupyter_jcli.formats import ipynb, percent
from jupyter_jcli.pairing import synchronize_pair


def _code(source, cell_id, count):
    cell = nbformat.v4.new_code_cell(source, id=cell_id)
    cell.execution_count = count
    cell.metadata = {"tags": [cell_id], "custom": {"keep": True}}
    cell.outputs = [
        nbformat.v4.new_output("stream", name="stdout", text=f"{cell_id}\nµ中文\n"),
        nbformat.v4.new_output(
            "display_data",
            data={"application/json": {"values": [1, None]}, "text/plain": "saved\n"},
            metadata={"custom": {"keep": cell_id}},
        ),
    ]
    return cell


def _make_pair(tmp_path, monkeypatch, *, extra_cells=(), py_text=None):
    notebook = nbformat.v4.new_notebook(
        cells=[_code("x = 1", "first", 11), _code("y = 2", "second", 12), *extra_cells],
        metadata={
            "kernelspec": {
                "name": "python3",
                "display_name": "Python 3",
                "language": "python",
            },
            "language_info": {"name": "python", "version": "3.10"},
            "custom": {"left": 1, "right": 1},
            "vscode": {"side": "notebook"},
            "widgets": {"custom": "saved widget state"},
        },
    )
    template = deepcopy(notebook)
    template.metadata["vscode"] = {"side": "python"}
    if py_text is None:
        py_text = percent.dumps(ipynb.from_node(template))
    py_path = tmp_path / "report.py"
    nb_path = tmp_path / "report.ipynb"
    py_path.write_text(py_text, encoding="utf-8")
    nb_path.write_text(nbformat.writes(notebook), encoding="utf-8")
    for name in ("_get_git_base_text", "_get_git_base_text_strict"):
        monkeypatch.setattr(f"jupyter_jcli.diff.drift.{name}", lambda path: py_text)
    return SimpleNamespace(py=py_path, nb=nb_path, notebook=notebook, baseline=py_text)


def _change_python(pair, *, source=None, metadata=None):
    parsed = percent.load(pair.py)
    if source is not None:
        parsed.cells[0].node.source = source
    if metadata is not None:
        parsed.notebook.metadata.update(metadata)
    pair.py.write_text(percent.dumps(parsed), encoding="utf-8")


def test_plain_cell_sync_preserves_complete_saved_notebook_data(tmp_path, monkeypatch):
    pair = _make_pair(tmp_path, monkeypatch)
    _change_python(pair, source="x = 10")
    edited_py = pair.py.read_bytes()

    result = synchronize_pair(pair.py, pair.nb, persist_baseline=False)

    expected = deepcopy(pair.notebook)
    expected.cells[0].source = "x = 10"
    assert nbformat.read(pair.nb, as_version=4) == expected
    assert pair.py.read_bytes() == edited_py
    assert isinstance(result.drift, Merged)
    assert result.py_changed is False
    assert result.ipynb_changed is True
    assert result.state.cells[0].source == "x = 10"
    assert isinstance(check_drift(pair.py, pair.nb), InSync)


def test_new_merge_combines_cell_and_metadata_edits_from_both_sides(
    tmp_path, monkeypatch
):
    pair = _make_pair(tmp_path, monkeypatch)
    _change_python(pair, source="x = 10", metadata={"custom": {"left": 2, "right": 1}})
    notebook = nbformat.read(pair.nb, as_version=4)
    notebook.cells[1].source = "y = 20"
    notebook.metadata["custom"]["right"] = 3
    pair.nb.write_text(nbformat.writes(notebook), encoding="utf-8")

    result = synchronize_pair(pair.py, pair.nb, persist_baseline=False)

    expected = deepcopy(pair.notebook)
    expected.cells[0].source = "x = 10"
    expected.cells[1].source = "y = 20"
    expected.metadata["custom"] = {"left": 2, "right": 3}
    assert nbformat.read(pair.nb, as_version=4) == expected
    parsed = percent.load(pair.py)
    assert [cell.source for cell in parsed.cells] == ["x = 10", "y = 20"]
    assert parsed.notebook.metadata["custom"] == {"left": 2, "right": 3}
    assert parsed.notebook.metadata["vscode"] == {"side": "python"}
    assert result.py_changed is result.ipynb_changed is True
    assert isinstance(check_drift(pair.py, pair.nb), InSync)


@pytest.mark.parametrize("conflict", ["source", "metadata", "kernel"])
def test_conflict_does_not_write_pair_or_advance_baseline(
    tmp_path, monkeypatch, conflict
):
    pair = _make_pair(tmp_path, monkeypatch)
    notebook = nbformat.read(pair.nb, as_version=4)
    if conflict == "source":
        _change_python(pair, source="x = 10")
        notebook.cells[0].source = "x = 20"
    elif conflict == "metadata":
        _change_python(pair, metadata={"custom": {"left": 2, "right": 1}})
        notebook.metadata["custom"]["left"] = 3
    else:
        _change_python(
            pair,
            metadata={"kernelspec": {"name": "ours", "display_name": "Ours"}},
        )
        notebook.metadata["kernelspec"] = {"name": "theirs", "display_name": "Theirs"}
    pair.nb.write_text(nbformat.writes(notebook), encoding="utf-8")
    before = pair.py.read_bytes(), pair.nb.read_bytes()
    persist = Mock()
    monkeypatch.setattr("jupyter_jcli.pairing._persist_pair_baseline", persist)

    result = synchronize_pair(pair.py, pair.nb)

    assert isinstance(result.drift, Conflict)
    assert result.state is None
    assert result.py_changed is result.ipynb_changed is result.baseline_written is False
    assert (pair.py.read_bytes(), pair.nb.read_bytes()) == before
    if conflict == "kernel":
        assert result.drift.kernel_conflict is not None
    elif conflict == "metadata":
        assert result.drift.metadata_conflicts[0].display_path == "metadata.custom.left"
    else:
        assert result.drift.conflict_indices == [0]
    persist.assert_not_called()


@pytest.mark.parametrize(
    "special", ["magics", "trailing-blank", "empty", "markdown-raw"]
)
def test_normalization_fallback_syncs_without_losing_unrelated_cells(
    tmp_path, monkeypatch, special
):
    if special == "magics":
        extras = [nbformat.v4.new_code_cell("%time value", id="special")]
        expected_sources = ["%time value"]
    elif special == "trailing-blank":
        extras = [nbformat.v4.new_code_cell("value = 3\n\n", id="special")]
        expected_sources = ["value = 3"]
    elif special == "empty":
        extras = [nbformat.v4.new_code_cell("", id="special")]
        expected_sources = [""]
    else:
        markdown = nbformat.v4.new_markdown_cell(
            "![saved](attachment:plot.png)", id="md"
        )
        markdown.attachments = {"plot.png": {"image/png": "aGVsbG8="}}
        extras = [markdown, nbformat.v4.new_raw_cell("raw µ", id="raw")]
        expected_sources = [markdown.source, "raw µ"]
    pair = _make_pair(tmp_path, monkeypatch, extra_cells=extras)
    if special == "trailing-blank":
        raw = pair.py.read_text(encoding="utf-8") + "\n\n"
        pair.py.write_text(raw, encoding="utf-8")
    pair.py.write_text(
        pair.py.read_text(encoding="utf-8").replace("x = 1", "x = 10"),
        encoding="utf-8",
    )

    result = synchronize_pair(pair.py, pair.nb, persist_baseline=False)

    saved = nbformat.read(pair.nb, as_version=4)
    assert [cell.source for cell in saved.cells] == [
        "x = 10",
        "y = 2",
        *expected_sources,
    ]
    assert [cell.id for cell in saved.cells] == [
        cell.id for cell in pair.notebook.cells
    ]
    assert saved.cells[0].outputs == pair.notebook.cells[0].outputs
    assert saved.cells[1] == pair.notebook.cells[1]
    if special == "markdown-raw":
        assert saved.cells[2].attachments == extras[0].attachments
        assert [cell.cell_type for cell in saved.cells[2:]] == ["markdown", "raw"]
    assert result.ipynb_changed is True
    assert isinstance(check_drift(pair.py, pair.nb), InSync)


@pytest.mark.parametrize("new_marker", ["# %%", '# %% id="first"'])
def test_missing_or_duplicate_id_is_repaired_during_normal_sync(
    tmp_path, monkeypatch, new_marker
):
    pair = _make_pair(tmp_path, monkeypatch)
    pair.py.write_text(pair.baseline + f"\n{new_marker}\nz = 3\n", encoding="utf-8")

    result = synchronize_pair(pair.py, pair.nb, persist_baseline=False)

    py_cells = percent.load(pair.py).cells
    nb_cells = nbformat.read(pair.nb, as_version=4).cells
    ids = [cell.cell_id for cell in py_cells]
    assert ids[:2] == ["first", "second"]
    assert ids[2] is not None and len(set(ids)) == 3
    assert ids == [cell.id for cell in nb_cells]
    assert [cell.source for cell in py_cells] == ["x = 1", "y = 2", "z = 3"]
    assert nb_cells[0].outputs == pair.notebook.cells[0].outputs
    assert nb_cells[1].outputs == pair.notebook.cells[1].outputs
    assert result.py_changed is result.ipynb_changed is True
    assert isinstance(check_drift(pair.py, pair.nb), InSync)


def test_writeback_does_not_mutate_loaded_input_cell_nodes(tmp_path, monkeypatch):
    pair = _make_pair(tmp_path, monkeypatch)
    _change_python(pair, source="x = 10")
    original_load = ipynb.load
    captured = []

    def capture_load(path):
        parsed = original_load(path)
        nodes = [cell.node for cell in parsed.cells]
        captured.append((nodes, deepcopy(nodes)))
        return parsed

    monkeypatch.setattr(ipynb, "load", capture_load)

    result = synchronize_pair(pair.py, pair.nb, persist_baseline=False)

    assert captured
    for nodes, snapshot in captured:
        assert nodes == snapshot
    assert result.state.cells[0].source == "x = 10"
    assert nbformat.read(pair.nb, as_version=4).cells[0].source == "x = 10"


def test_successful_write_is_physically_reloaded_before_baseline(tmp_path, monkeypatch):
    pair = _make_pair(tmp_path, monkeypatch)
    _change_python(pair, source="x = 10")
    original_write = Path.write_bytes
    original_py_load, original_nb_load = percent.load, ipynb.load
    events = []

    def write_bytes(path, data):
        result = original_write(path, data)
        if path == pair.nb:
            events.append("write")
        return result

    def py_load(path):
        if "write" in events:
            events.append("reload-py")
        return original_py_load(path)

    def nb_load(path):
        if "write" in events:
            events.append("reload-nb")
        return original_nb_load(path)

    def persist(*args, **kwargs):
        assert "reload-py" in events and "reload-nb" in events
        events.append("baseline")
        return True

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    monkeypatch.setattr(percent, "load", py_load)
    monkeypatch.setattr(ipynb, "load", nb_load)
    monkeypatch.setattr("jupyter_jcli.pairing._persist_pair_baseline", persist)

    result = synchronize_pair(pair.py, pair.nb)

    assert result.baseline_written is True
    assert events[-1] == "baseline"


def test_nonconvergent_writeback_does_not_advance_baseline(tmp_path, monkeypatch):
    pair = _make_pair(tmp_path, monkeypatch)
    _change_python(pair, source="x = 10")
    original_write = Path.write_bytes
    original_nb = pair.nb.read_bytes()
    persist = Mock()

    def omit_notebook_write(path, data):
        if path == pair.nb:
            return len(data)
        return original_write(path, data)

    monkeypatch.setattr(Path, "write_bytes", omit_notebook_write)
    monkeypatch.setattr("jupyter_jcli.pairing._persist_pair_baseline", persist)

    with pytest.raises(RuntimeError, match="did not converge"):
        synchronize_pair(pair.py, pair.nb)

    assert pair.nb.read_bytes() == original_nb
    persist.assert_not_called()


def test_later_sync_reloads_files_and_ignores_mutated_previous_result(
    tmp_path, monkeypatch
):
    pair = _make_pair(tmp_path, monkeypatch)
    first = synchronize_pair(pair.py, pair.nb, persist_baseline=False)
    first.state.cells[0].node.source = "stale result"
    first.state.metadata["custom"]["left"] = 999
    _change_python(pair, source="x = 30")

    second = synchronize_pair(pair.py, pair.nb, persist_baseline=False)

    assert second.state.cells[0].source == "x = 30"
    assert second.state.metadata["custom"] == {"left": 1, "right": 1}
    assert nbformat.read(pair.nb, as_version=4).cells[0].source == "x = 30"
    assert isinstance(check_drift(pair.py, pair.nb), InSync)
