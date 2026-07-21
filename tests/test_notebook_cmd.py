"""Test notebook inspection commands."""

import json

import nbformat
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli._enums import CellType
from jupyter_jcli.commands.notebook import (
    CellChange,
    build_summary_data,
    diff_cells,
    format_summary_human,
)
from jupyter_jcli.parser import Cell, ParsedFile


def _write_percent_notebook(path):
    path.write_text(
        "# ---\n"
        "# jupyter:\n"
        "#   kernelspec:\n"
        "#     name: python3\n"
        "# ---\n"
        "\n"
        "# %%\n"
        "import os\n"
        "from pkg import item as alias\n"
        "data = load()\n"
        "data = service.fetch()\n"
        "class Model:\n"
        "    pass\n"
        "def build():\n"
        "    return helper()\n"
        "for row in rows:\n"
        "    total = row\n"
        "\n"
        "# %% [markdown]\n"
        "#\n"
        "# # Report title\n"
        "# More text\n"
        "\n"
        "# %% [raw]\n"
        "# raw payload\n"
        "\n"
        "# %%\n"
        "%matplotlib inline\n"
        "plot(values)\n",
        encoding="utf-8",
    )


def _parsed(*sources: str) -> ParsedFile:
    return ParsedFile(
        kernel_name="python3",
        cells=[Cell(index=index, cell_type=CellType.CODE, source=source) for index, source in enumerate(sources)],
    )


def test_summary_extracts_python_ast_fields_and_non_code_previews(tmp_path):
    path = tmp_path / "report.py"
    _write_percent_notebook(path)

    result = CliRunner().invoke(main, ["--json", "notebook", "summary", str(path)])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "ok"
    assert data["path"] == str(path)
    assert data["cell_count"] == 4
    assert data["kernel"] == "python3"
    assert "_human" not in data

    code = data["cells"][0]
    assert code["line_count"] == 10
    assert code["source_preview"] == "import os"
    assert code["imports"] == ["os", "pkg.item as alias"]
    assert code["defines"] == ["Model", "build"]
    assert code["writes"] == ["data", "row", "total"]
    assert code["calls"] == ["load", "service.fetch", "helper"]
    assert code["ast_parsed"] is True
    assert all(not code[f"{field}_truncated"] for field in ("imports", "defines", "writes", "calls"))

    markdown = data["cells"][1]
    assert markdown["type"] == "markdown"
    assert markdown["first_nonempty_line"] == "# Report title"
    assert markdown["source_preview"] == "# Report title"

    raw = data["cells"][2]
    assert raw["type"] == "raw"
    assert raw["source_preview"] == "raw payload"


def test_summary_falls_back_to_preview_when_python_ast_cannot_parse(tmp_path):
    path = tmp_path / "magic.py"
    path.write_text("# %%\n%matplotlib inline\nplot(values)\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["--json", "notebook", "summary", str(path)])

    assert result.exit_code == 0
    cell = json.loads(result.output)["cells"][0]
    assert cell["ast_parsed"] is False
    assert cell["imports"] == []
    assert cell["source_preview"] == "%matplotlib inline"


def test_summary_marks_truncated_ast_fields(tmp_path):
    path = tmp_path / "many_imports.py"
    path.write_text("\n".join(f"import package_{index}" for index in range(9)), encoding="utf-8")

    result = CliRunner().invoke(main, ["--json", "notebook", "summary", str(path)])

    assert result.exit_code == 0
    cell = json.loads(result.output)["cells"][0]
    assert cell["imports"] == [f"package_{index}" for index in range(8)]
    assert cell["imports_truncated"] is True


def test_summary_human_includes_notebook_metadata_and_cells(tmp_path):
    path = tmp_path / "summary.py"
    _write_percent_notebook(path)

    result = CliRunner().invoke(main, ["notebook", "summary", str(path)])

    assert result.exit_code == 0
    assert f"path={path} cells=4 kernel=python3" in result.output
    assert "0 [code] [10L]" in result.output
    assert "1 [markdown] [3L] first_line='# Report title'" in result.output
    assert "2 [raw] [1L]" in result.output


def test_show_returns_one_cell_and_full_source_json(tmp_path):
    path = tmp_path / "notebook.ipynb"
    notebook = nbformat.v4.new_notebook()
    notebook.metadata["kernelspec"] = {"name": "python3"}
    notebook.cells = [
        nbformat.v4.new_markdown_cell("# Title"),
        nbformat.v4.new_raw_cell("raw source"),
        nbformat.v4.new_code_cell("print('complete source')"),
    ]
    nbformat.write(notebook, path)

    result = CliRunner().invoke(main, ["--json", "notebook", "show", str(path), "--cell", "1"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "ok"
    assert data["cell_count"] == 3
    assert data["kernel"] == "python3"
    assert data["cells"] == [{"index": 1, "type": "raw", "source": "raw source"}]


def test_show_range_includes_all_cell_types_and_human_headers(tmp_path):
    path = tmp_path / "ranges.py"
    _write_percent_notebook(path)

    result = CliRunner().invoke(main, ["notebook", "show", str(path), "--cell", ":3"])

    assert result.exit_code == 0
    assert "--- cell 0 [code] ---" in result.output
    assert "--- cell 1 [markdown] ---" in result.output
    assert "--- cell 2 [raw] ---" in result.output
    assert "# Report title" in result.output
    assert "raw payload" in result.output


def test_show_no_matching_cell_uses_structured_error(tmp_path):
    path = tmp_path / "empty.py"
    path.write_text("print('one cell')\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["--json", "notebook", "show", str(path), "--cell", "3"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "status": "error",
        "code": "CELL_NOT_FOUND",
        "message": "No cells matched: 3",
    }


def test_cli_registers_notebook_group():
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "notebook" in result.output
    subcommand_help = CliRunner().invoke(main, ["notebook", "--help"])
    assert subcommand_help.exit_code == 0
    assert "summary" in subcommand_help.output
    assert "show" in subcommand_help.output


def test_cell_diff_classifies_edited_inserted_deleted_and_unequal_replace():
    edited = diff_cells(_parsed("old"), _parsed("new"))
    inserted = diff_cells(_parsed("keep"), _parsed("new", "keep"))
    deleted = diff_cells(_parsed("keep", "gone"), _parsed("keep"))
    unequal_replace = diff_cells(_parsed("old one", "old two"), _parsed("new one"))

    assert [(change.kind, change.old_index, change.new_index) for change in edited] == [("edited", 0, 0)]
    assert [(change.kind, change.old_index, change.new_index) for change in inserted] == [("inserted", None, 0)]
    assert [(change.kind, change.old_index, change.new_index) for change in deleted] == [("deleted", 1, None)]
    assert [(change.kind, change.old_index, change.new_index) for change in unequal_replace] == [
        ("edited", 0, 0),
        ("deleted", 1, None),
    ]


def test_cell_diff_keeps_unchanged_cells_aligned_after_leading_insert():
    old = _parsed("first", "second")
    current = _parsed("new first", "first", "second")

    changes = diff_cells(old, current)
    data = build_summary_data(current, changes)

    assert [(change.kind, change.new_index) for change in changes] == [("inserted", 0)]
    assert data["cells"][0]["change"] == "inserted"
    assert "change" not in data["cells"][1]
    assert "change" not in data["cells"][2]


def test_cell_diff_pairs_an_edited_cell_with_the_most_similar_insertion_neighbor():
    changes = diff_cells(_parsed("x = 1", "y = 2"), _parsed("new = 0", "x = 10", "y = 2"))

    assert [(change.kind, change.old_index, change.new_index) for change in changes] == [
        ("inserted", None, 0),
        ("edited", 0, 1),
    ]


def test_summary_data_has_no_changes_or_markers_without_diff():
    data = build_summary_data(_parsed("value = 1"))
    human = format_summary_human(data)

    assert data["changes"] == []
    assert "change" not in data["cells"][0]
    assert "legend:" not in human
    assert "~ 0" not in human


def test_summary_human_renders_dynamic_legend_and_deleted_tombstone():
    old = _parsed("gone = 1", "value = 1")
    current = _parsed("value = 2", "new = 3")
    changes = [
        CellChange("edited", 1, 0, old.cells[1], current.cells[0], 0),
        CellChange("inserted", None, 1, None, current.cells[1], 1),
        CellChange("deleted", 0, None, old.cells[0], None, 0),
    ]

    data = build_summary_data(current, changes)
    human = format_summary_human(data)

    assert data["changes"][2]["old_index"] == 0
    assert data["changes"][2]["current_insertion_index"] == 0
    assert data["changes"][2]["old_cell"]["writes"] == ["gone"]
    assert "changes: edited current[0]; inserted current[1]; deleted [old:0 at current:0]" in human
    assert "legend: ~ edited | + inserted | - deleted" in human
    assert "~ 0 [code]" in human
    assert "+ 1 [code]" in human
    assert "- old:0 at current:0 [code]" in human
    assert "writes=gone" in human
