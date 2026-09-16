"""Tests for read-only saved notebook output access."""

import json
from pathlib import Path
from unittest.mock import patch

import nbformat
import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.outputs import OutputProtocolError
from jupyter_jcli.outputs.notebook import (
    list_notebook_outputs,
    read_notebook_output,
    resolve_notebook_cell,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "outputs" / "mixed_outputs.json"


def _outputs():
    return [nbformat.from_dict(item) for item in json.loads(_FIXTURE.read_text())]


def _write_notebook(path, cells):
    notebook = nbformat.v4.new_notebook(cells=cells)
    nbformat.write(notebook, path)
    return path


def _code_cell(source, *, cell_id=None, outputs=None, execution_count=None):
    kwargs = {"id": cell_id} if cell_id is not None else {}
    cell = nbformat.v4.new_code_cell(source, **kwargs)
    cell.outputs = outputs or []
    cell.execution_count = execution_count
    return cell


def test_direct_notebook_directory_keeps_empty_outputs_distinct_from_missing_cell(
    tmp_path,
):
    notebook_path = _write_notebook(tmp_path / "empty.ipynb", [_code_cell("value = 1")])

    result = list_notebook_outputs(notebook_path, 0)

    assert result["outputs"] == []
    assert result["source"]["mapping"] == "direct"
    with pytest.raises(OutputProtocolError) as missing:
        list_notebook_outputs(notebook_path, 1)
    assert missing.value.code == "CELL_NOT_FOUND"


def test_python_cell_maps_by_unique_stable_id_across_reorder(tmp_path):
    py_path = tmp_path / "report.py"
    py_path.write_text(
        '# %% id="first"\nfirst = 1\n\n# %% id="second"\nsecond = 2\n',
        encoding="utf-8",
    )
    notebook_path = _write_notebook(
        tmp_path / "report.ipynb",
        [
            _code_cell(
                "second = 20",
                cell_id="second",
                outputs=[nbformat.v4.new_output("stream", name="stdout", text="two")],
            ),
            _code_cell(
                "first = 10",
                cell_id="first",
                outputs=[nbformat.v4.new_output("stream", name="stdout", text="one")],
            ),
        ],
    )

    result = read_notebook_output(py_path, 0, 0, limit=None)

    assert result["stream"]["data"] == "one"
    assert result["source"]["path"] == str(notebook_path.resolve())
    assert result["source"]["cell_index"] == 1
    assert result["source"]["requested_cell_index"] == 0
    assert result["source"]["mapping"] == "id"
    assert result["source"]["source_start_line"] == 2


def test_python_cell_maps_by_unique_content_after_insertion(tmp_path):
    py_path = tmp_path / "report.py"
    py_path.write_text("# %%\nx = 1\n\n# %%\ny = 2\n", encoding="utf-8")
    _write_notebook(
        tmp_path / "report.ipynb",
        [
            _code_cell("inserted = True"),
            _code_cell("x = 1"),
            _code_cell(
                "y = 2",
                outputs=[
                    nbformat.v4.new_output("stream", name="stdout", text="mapped")
                ],
            ),
        ],
    )

    resolved = resolve_notebook_cell(py_path, 1)

    assert resolved.notebook_cell_index == 2
    assert resolved.mapping == "content"


@pytest.mark.parametrize(
    ("py_source", "notebook_sources"),
    [
        ("# %%\nx = 2\n", ["x = 1"]),
        ("# %%\nx = 1\n\n# %%\nx = 1\n", ["x = 1", "x = 1"]),
    ],
)
def test_python_mapping_rejects_position_guessing_and_duplicate_content(
    tmp_path, py_source, notebook_sources
):
    py_path = tmp_path / "report.py"
    py_path.write_text(py_source, encoding="utf-8")
    _write_notebook(
        tmp_path / "report.ipynb", [_code_cell(source) for source in notebook_sources]
    )

    with pytest.raises(OutputProtocolError) as unreliable:
        resolve_notebook_cell(py_path, 0)

    assert unreliable.value.code == "CELL_MAPPING_UNRELIABLE"


def test_python_mapping_rejects_conflicting_ids_even_when_source_matches(tmp_path):
    py_path = tmp_path / "report.py"
    py_path.write_text('# %% id="python"\nx = 1\n', encoding="utf-8")
    _write_notebook(
        tmp_path / "report.ipynb", [_code_cell("x = 1", cell_id="notebook")]
    )

    with pytest.raises(OutputProtocolError) as unreliable:
        resolve_notebook_cell(py_path, 0)

    assert unreliable.value.code == "CELL_MAPPING_UNRELIABLE"
    assert "conflicting cell IDs" in unreliable.value.message


def test_read_is_side_effect_free_and_does_not_consult_pair_baseline(tmp_path):
    py_path = tmp_path / "report.py"
    py_path.write_text("# %%\nx = 1\n", encoding="utf-8")
    notebook_path = _write_notebook(
        tmp_path / "report.ipynb",
        [
            _code_cell(
                "x = 1",
                outputs=[nbformat.v4.new_output("stream", name="stdout", text="saved")],
            )
        ],
    )
    before_py = py_path.read_bytes()
    before_notebook = notebook_path.read_bytes()

    with patch(
        "jupyter_jcli.pair_baseline.read_baseline",
        side_effect=AssertionError("reader must not consult the pair baseline"),
    ):
        result = read_notebook_output(py_path, 0, 0)

    assert result["stream"]["data"] == "saved"
    assert py_path.read_bytes() == before_py
    assert notebook_path.read_bytes() == before_notebook
    assert not (tmp_path / ".j-cli").exists()


def test_notebook_outputs_cli_lists_real_indices_and_mime_types(tmp_path):
    notebook_path = _write_notebook(
        tmp_path / "mixed.ipynb",
        [_code_cell("display()", outputs=_outputs(), execution_count=9)],
    )

    result = CliRunner().invoke(
        main,
        ["--json", "notebook", "outputs", str(notebook_path), "--cell", "0"],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert [item["output_index"] for item in data["outputs"]] == [0, 1, 2, 3]
    assert "image/png" in data["outputs"][1]["available_mime_types"]
    assert data["source"]["execution_count"] == 9


def test_notebook_output_cli_returns_exact_html_and_structured_json(tmp_path):
    notebook_path = _write_notebook(
        tmp_path / "mixed.ipynb", [_code_cell("display()", outputs=_outputs())]
    )
    runner = CliRunner()

    html_result = runner.invoke(
        main,
        [
            "--json",
            "notebook",
            "output",
            str(notebook_path),
            "--cell",
            "0",
            "--output",
            "1",
            "--mime",
            "text/html",
        ],
    )
    json_result = runner.invoke(
        main,
        [
            "--json",
            "notebook",
            "output",
            str(notebook_path),
            "--cell",
            "0",
            "--output",
            "2",
            "--mime",
            "application/json",
        ],
    )

    assert html_result.exit_code == 0
    assert json.loads(html_result.output)["selected"]["data"] == (
        "<strong>raw html</strong>"
    )
    assert json_result.exit_code == 0
    assert json.loads(json_result.output)["selected"]["data"] == {"answer": 42}


def test_notebook_output_cli_honors_repeated_supported_mime_and_compact_json(
    tmp_path,
):
    notebook_path = _write_notebook(
        tmp_path / "mixed.ipynb", [_code_cell("display()", outputs=_outputs())]
    )
    base_args = [
        "--json",
        "notebook",
        "output",
        str(notebook_path),
        "--cell",
        "0",
        "--output",
        "1",
        "--supported-mime",
        "text/plain",
        "--supported-mime",
        "text/html",
    ]
    runner = CliRunner()

    automatic = runner.invoke(main, base_args)
    excluded_explicit = runner.invoke(main, [*base_args, "--mime", "image/png"])

    assert automatic.exit_code == 0
    data = json.loads(automatic.output)
    assert data["selected"]["mime_type"] == "text/html"
    assert automatic.output == (
        json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    assert excluded_explicit.exit_code == 1
    assert json.loads(excluded_explicit.output)["code"] == "MIME_NOT_SUPPORTED"


def test_notebook_output_cli_converts_library_errors_to_structured_errors(tmp_path):
    notebook_path = _write_notebook(tmp_path / "one.ipynb", [_code_cell("x = 1")])

    result = CliRunner().invoke(
        main,
        [
            "--json",
            "notebook",
            "output",
            str(notebook_path),
            "--cell",
            "0",
            "--output",
            "0",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "status": "error",
        "code": "OUTPUT_NOT_FOUND",
        "message": "Output index out of range: 0",
    }


def test_python_reader_reports_missing_pair(tmp_path):
    py_path = tmp_path / "standalone.py"
    py_path.write_text("# %%\nx = 1\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["--json", "notebook", "outputs", str(py_path), "--cell", "0"],
    )

    assert result.exit_code == 1
    assert json.loads(result.output)["code"] == "NOTEBOOK_PAIR_NOT_FOUND"
