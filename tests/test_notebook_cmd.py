"""Test notebook inspection commands."""

import json
from unittest.mock import patch

import nbformat
import pytest
from click.testing import CliRunner

from jupyter_jcli._enums import CellChangeKind, CellType
from jupyter_jcli.cli import main
from jupyter_jcli.diff import CellChange, diff_cells
from jupyter_jcli.formats.model import Cell, ParsedFile
from jupyter_jcli.summ import build_summary_data, format_summary_human


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
        cells=[
            Cell(index=index, cell_type=CellType.CODE, source=source)
            for index, source in enumerate(sources)
        ],
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
    assert code["source_start_line"] == 8
    assert code["source_end_line"] == 17
    assert code["preview"].startswith("import os\nfrom pkg")
    assert code["preview_truncated"] is True
    assert "full_text" not in code
    assert code["imports"] == ["os", "pkg.item as alias"]
    assert code["defines"] == ["Model", "build"]
    assert code["writes"] == ["data", "row", "total"]
    assert code["calls"] == ["load", "service.fetch", "helper"]
    assert code["ast_parsed"] is True
    assert all(
        not code[f"{field}_truncated"]
        for field in ("imports", "defines", "writes", "calls")
    )

    markdown = data["cells"][1]
    assert markdown["type"] == "markdown"
    assert markdown["full_text"] == "\n# Report title\nMore text"
    assert "preview" not in markdown
    assert "first_nonempty_line" not in markdown

    raw = data["cells"][2]
    assert raw["type"] == "raw"
    assert raw["full_text"] == "raw payload"
    assert "preview" not in raw


@pytest.mark.parametrize(
    ("cell_type", "source", "source_field"),
    [
        pytest.param(CellType.MARKDOWN, "m" * 120, "full_text", id="markdown-120"),
        pytest.param(CellType.MARKDOWN, "m" * 121, "preview", id="markdown-121"),
        pytest.param(
            CellType.MARKDOWN,
            "heading\n" + "long paragraph " * 10,
            "preview",
            id="markdown-long-multiline",
        ),
        pytest.param(CellType.RAW, "raw payload", "full_text", id="raw-short"),
    ],
)
def test_summary_source_fields_are_exclusive_across_non_code_cells(
    cell_type, source, source_field
):
    parsed = ParsedFile(
        kernel_name="python3",
        cells=[Cell(index=0, cell_type=cell_type, source=source)],
    )

    data = build_summary_data(parsed)
    cell = data["cells"][0]
    normal = format_summary_human(data)
    bounded = format_summary_human(data, max_cells=1, max_chars=8_000)

    assert normal == bounded
    assert source_field in cell
    assert {"full_text", "preview"}.intersection(cell) == {source_field}
    assert all(
        field not in cell
        for field in ("source", "source_preview", "first_line", "first_nonempty_line")
    )
    if source_field == "full_text":
        assert cell["full_text"] == source
        assert "preview_truncated" not in cell
        assert f"full_text={source!r}" in normal
    else:
        assert cell["preview"] == source[:120]
        assert cell["preview_truncated"] is True
        assert f"preview={source[:120]!r} [truncated]" in normal


def test_summary_falls_back_to_preview_when_python_ast_cannot_parse(tmp_path):
    path = tmp_path / "magic.py"
    source = "%matplotlib inline\nplot(values)\n" + "# padding\n" * 20
    path.write_text(f"# %%\n{source}", encoding="utf-8")

    result = CliRunner().invoke(main, ["--json", "notebook", "summary", str(path)])

    assert result.exit_code == 0
    cell = json.loads(result.output)["cells"][0]
    assert cell["ast_parsed"] is False
    assert cell["imports"] == []
    assert cell["preview"] == source[:120]
    assert cell["preview_truncated"] is True


def test_summary_marks_truncated_ast_fields(tmp_path):
    path = tmp_path / "many_imports.py"
    path.write_text(
        "\n".join(f"import package_{index}" for index in range(9)), encoding="utf-8"
    )

    result = CliRunner().invoke(main, ["--json", "notebook", "summary", str(path)])

    assert result.exit_code == 0
    cell = json.loads(result.output)["cells"][0]
    assert cell["imports"] == [f"package_{index}" for index in range(8)]
    assert cell["imports_truncated"] is True


def test_summary_bounds_many_unique_writes_and_calls(tmp_path):
    path = tmp_path / "many_names.py"
    path.write_text(
        "\n".join(
            [f"value_{index} = {index}" for index in range(100)]
            + [f"function_{index}()" for index in range(100)]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["--json", "notebook", "summary", str(path)])

    assert result.exit_code == 0
    cell = json.loads(result.output)["cells"][0]
    assert cell["writes"] == [f"value_{index}" for index in range(8)]
    assert cell["calls"] == [f"function_{index}" for index in range(8)]
    assert cell["writes_truncated"] is True
    assert cell["calls_truncated"] is True


def test_summary_formats_relative_imports_without_an_extra_dot(tmp_path):
    path = tmp_path / "relative.py"
    path.write_text(
        "from . import sibling\nfrom .. import parent\nfrom .package import child\n"
        + "# padding\n" * 10,
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["--json", "notebook", "summary", str(path)])

    assert result.exit_code == 0
    assert json.loads(result.output)["cells"][0]["imports"] == [
        ".sibling",
        "..parent",
        ".package.child",
    ]


def test_summary_human_includes_notebook_metadata_and_cells(tmp_path):
    path = tmp_path / "summary.py"
    _write_percent_notebook(path)

    result = CliRunner().invoke(main, ["notebook", "summary", str(path)])

    assert result.exit_code == 0
    assert f"path={path} cells=4 kernel=python3" in result.output
    assert "0 [code] [10 lines] [L8-17]" in result.output
    assert (
        "1 [markdown] [3 lines] [L20-22] "
        "full_text='\\n# Report title\\nMore text'" in result.output
    )
    assert "2 [raw] [1 line]" in result.output


def test_summary_human_omits_empty_code_categories(tmp_path):
    path = tmp_path / "summary.py"
    path.write_text(
        "# %%\nresult = calculate()\n" + "# padding\n" * 20,
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["notebook", "summary", str(path)])

    assert result.exit_code == 0
    assert "writes=result" in result.output
    assert "calls=calculate" in result.output
    assert "imports=" not in result.output
    assert "defines=" not in result.output


def test_summary_shows_full_source_for_short_cell(tmp_path):
    path = tmp_path / "summary.py"
    source = "value = load()\nvalue"
    path.write_text(f"# %%\n{source}\n", encoding="utf-8")

    json_result = CliRunner().invoke(main, ["--json", "notebook", "summary", str(path)])
    human_result = CliRunner().invoke(main, ["notebook", "summary", str(path)])

    assert json_result.exit_code == 0
    cell = json.loads(json_result.output)["cells"][0]
    assert cell["full_text"] == source
    assert "preview" not in cell
    assert "source" not in cell
    assert "ast_parsed" not in cell
    assert human_result.exit_code == 0
    assert f"full_text={source!r}" in human_result.output
    assert "writes=" not in human_result.output
    assert "calls=" not in human_result.output


def test_summary_preserves_empty_trailing_newline_and_long_single_line_text():
    sources = ["", "first\nsecond\n", "x" * 121]
    data = build_summary_data(_parsed(*sources))
    human = format_summary_human(data)

    assert data["cells"][0]["line_count"] == 0
    assert data["cells"][0]["full_text"] == ""
    assert data["cells"][1]["line_count"] == 2
    assert data["cells"][1]["full_text"] == "first\nsecond\n"
    assert data["cells"][2]["line_count"] == 1
    assert data["cells"][2]["preview"] == "x" * 120
    assert data["cells"][2]["preview_truncated"] is True
    assert "full_text" not in data["cells"][2]
    assert "0 [code] [0 lines] full_text=''" in human
    assert "1 [code] [2 lines] full_text='first\\nsecond\\n'" in human
    assert "2 [code] [1 line]" in human
    assert "[truncated]" in human


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

    result = CliRunner().invoke(
        main, ["--json", "notebook", "show", str(path), "--cell", "1"]
    )

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

    result = CliRunner().invoke(
        main, ["--json", "notebook", "show", str(path), "--cell", "3"]
    )

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "status": "error",
        "code": "CELL_NOT_FOUND",
        "message": "No cells matched: 3",
    }


def test_show_invalid_cell_spec_uses_json_parse_error(tmp_path):
    path = tmp_path / "one.py"
    path.write_text("value = 1\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["--json", "notebook", "show", str(path), "--cell", "1:0"],
    )

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "status": "error",
        "code": "PARSE_ERROR",
        "message": "Invalid cell spec: 1:0",
    }


def test_cli_registers_notebook_group():
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "notebook" in result.output
    subcommand_help = CliRunner().invoke(main, ["notebook", "--help"])
    assert subcommand_help.exit_code == 0
    assert "summary" in subcommand_help.output
    assert "show" in subcommand_help.output
    assert "map" in subcommand_help.output


def test_map_returns_pair_alignment_lines_ids_and_baseline_changes(tmp_path):
    py_path = tmp_path / "analysis.py"
    ipynb_path = tmp_path / "analysis.ipynb"
    py_path.write_text(
        '# %% id="first"\nx = 2\n\n# %% id="second"\ny = 2\n',
        encoding="utf-8",
    )
    notebook = nbformat.v4.new_notebook(
        cells=[
            nbformat.v4.new_code_cell("x = 2", id="first"),
            nbformat.v4.new_code_cell("y = 3", id="second"),
        ]
    )
    nbformat.write(notebook, ipynb_path)
    baseline = '# %% id="first"\nx = 1\n\n# %% id="second"\ny = 2\n'

    with patch("jupyter_jcli.commands.notebook.pair_baseline.read_baseline") as read:
        read.return_value = baseline
        result = CliRunner().invoke(main, ["--json", "notebook", "map", str(py_path)])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "ok"
    assert data["python_path"] == str(py_path.resolve())
    assert data["notebook_path"] == str(ipynb_path.resolve())
    assert data["baseline_available"] is True
    assert data["cells"] == [
        {
            "python_index": 0,
            "notebook_index": 0,
            "cell_id": "first",
            "notebook_cell_id": "first",
            "type": "code",
            "source_start_line": 2,
            "source_end_line": 2,
            "alignment": "id",
            "change": "equal",
            "python_baseline_index": 0,
            "notebook_baseline_index": 0,
            "python_change": "edited",
            "notebook_change": "edited",
        },
        {
            "python_index": 1,
            "notebook_index": 1,
            "cell_id": "second",
            "notebook_cell_id": "second",
            "type": "code",
            "source_start_line": 5,
            "source_end_line": 5,
            "alignment": "id",
            "change": "edited",
            "python_baseline_index": 1,
            "notebook_baseline_index": 1,
            "python_change": "equal",
            "notebook_change": "edited",
        },
    ]


def test_map_accepts_notebook_path_and_nulls_baseline_fields(tmp_path):
    py_path = tmp_path / "paired.py"
    ipynb_path = tmp_path / "paired.ipynb"
    py_path.write_text("# %%\nvalue = 1\n", encoding="utf-8")
    nbformat.write(
        nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("value = 1")]),
        ipynb_path,
    )

    result = CliRunner().invoke(main, ["--json", "notebook", "map", str(ipynb_path)])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["python_path"] == str(py_path.resolve())
    assert data["notebook_path"] == str(ipynb_path.resolve())
    assert data["baseline_available"] is False
    assert data["cells"][0]["alignment"] == "content"
    assert data["cells"][0]["python_change"] is None
    assert data["cells"][0]["notebook_change"] is None


def test_map_missing_pair_uses_structured_error(tmp_path):
    path = tmp_path / "unpaired.py"
    path.write_text("value = 1\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["--json", "notebook", "map", str(path)])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "status": "error",
        "code": "PAIR_NOT_FOUND",
        "message": f"No paired file found for: {path}",
    }


def test_summary_marks_only_the_inserted_cell_after_leading_insert():
    old = _parsed("first", "second")
    current = _parsed("new first", "first", "second")

    changes = diff_cells(old, current)
    data = build_summary_data(current, changes)

    assert [(change.kind, change.new_index) for change in changes] == [("inserted", 0)]
    assert data["cells"][0]["change"] == "inserted"
    assert "change" not in data["cells"][1]
    assert "change" not in data["cells"][2]


def test_summary_preserves_markers_after_nearby_insert_and_delete():
    old = _parsed(*(f"value_{index}" for index in range(200)))
    current_sources = [cell.source for cell in old.cells]
    current_sources.insert(50, "inserted")
    del current_sources[56]
    current = _parsed(*current_sources)

    changes = diff_cells(old, current)

    assert [
        (change.kind, change.old_index, change.new_index) for change in changes
    ] == [
        ("inserted", None, 50),
        ("deleted", 55, None),
    ]
    data = build_summary_data(current, changes)
    assert data["cells"][50]["change"] == "inserted"
    assert all("change" not in data["cells"][index] for index in range(51, 56))


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
        CellChange(CellChangeKind.EDITED, 1, 0, old.cells[1], current.cells[0], 0),
        CellChange(CellChangeKind.INSERTED, None, 1, None, current.cells[1], 1),
        CellChange(CellChangeKind.DELETED, 0, None, old.cells[0], None, 0),
    ]

    data = build_summary_data(current, changes)
    human = format_summary_human(data)

    assert data["changes"][2]["old_index"] == 0
    assert data["changes"][2]["current_insertion_index"] == 0
    assert data["changes"][2]["old_cell"]["full_text"] == "gone = 1"
    assert (
        "changes: edited current[0]; inserted current[1]; deleted [old:0 at current:0]"
        in human
    )
    assert "legend: ~ edited | + inserted | - deleted" in human
    assert "~ 0 [code]" in human
    assert "+ 1 [code]" in human
    assert "- old:0 at current:0 [code]" in human
    assert "full_text='gone = 1'" in human


def test_bounded_summary_keeps_changed_cell_at_end_and_reports_omissions():
    old = _parsed(*(f"value_{index} = {index}" for index in range(50)))
    current_sources = [cell.source for cell in old.cells]
    current_sources[-1] = "value_49 = 999"
    current = _parsed(*current_sources)

    human = format_summary_human(
        build_summary_data(current, diff_cells(old, current)),
        max_cells=4,
        max_chars=2000,
    )

    assert "~ 49 [code]" in human
    assert "value_49 = 999" in human
    assert "omitted:" in human
    assert "j-cli notebook summary" in human
    assert len(human) <= 2000


def test_bounded_summary_preserves_change_marker_with_long_kernel():
    old = _parsed("value = 1")
    current = ParsedFile(
        kernel_name="kernel" * 2_000,
        cells=[Cell(index=0, cell_type=CellType.CODE, source="value = 2")],
        source_path="notebook.py",
    )

    human = format_summary_human(
        build_summary_data(current, diff_cells(old, current)),
        max_cells=16,
        max_chars=8_000,
    )

    assert "path=notebook.py cells=1 kernel=" in human
    assert "changes: edited current[0]" in human
    assert "legend: ~ edited" in human
    assert "~ 0 [code]" in human
    assert human.endswith("j-cli notebook summary notebook.py")
    assert len(human) <= 8_000


def test_bounded_summary_counts_only_rendered_long_changed_cells():
    old = _parsed(*(f"value_{index} = 0" for index in range(30)))
    long_identifier = "identifier" * 400
    current = _parsed(
        *(f"{long_identifier}_{index} = 1" for index in range(16)),
        *(cell.source for cell in old.cells[16:]),
    )

    human = format_summary_human(
        build_summary_data(current, diff_cells(old, current)),
        max_cells=16,
        max_chars=8_000,
    )
    rendered_markers = sum(f"~ {index} [code]" in human for index in range(16))

    assert rendered_markers >= 1
    assert (
        f"omitted: {30 - rendered_markers} current cells, 0 deleted tombstones" in human
    )
    assert human.endswith("j-cli notebook summary ")
    assert len(human) <= 8_000
