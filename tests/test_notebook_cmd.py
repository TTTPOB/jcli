"""Test notebook inspection commands."""

import json
import subprocess
import sys
from difflib import SequenceMatcher as RealSequenceMatcher
from unittest.mock import patch

import nbformat
from click.testing import CliRunner

from jupyter_jcli._enums import CellType
from jupyter_jcli.cli import main
from jupyter_jcli.commands.notebook import (
    CellChange,
    build_summary_data,
    diff_cells,
    format_summary_human,
)
from jupyter_jcli.formats.model import Cell, ParsedFile


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
    assert code["source_preview"] == "import os"
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
        "from . import sibling\nfrom .. import parent\nfrom .package import child\n",
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
    assert "0 [code] [10L] [L8-17]" in result.output
    assert (
        "1 [markdown] [3L] [L20-22] source='\\n# Report title\\nMore text'"
        in result.output
    )
    assert "2 [raw] [1L]" in result.output


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
    assert json.loads(json_result.output)["cells"][0]["source"] == source
    assert human_result.exit_code == 0
    assert f"source={source!r}" in human_result.output
    assert "writes=" not in human_result.output
    assert "calls=" not in human_result.output


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


def test_notebook_helpers_import_without_cli_cycle(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from jupyter_jcli.commands.notebook import diff_cells; print(diff_cells.__name__)",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "diff_cells"


def test_cell_diff_classifies_edited_inserted_deleted_and_unequal_replace():
    edited = diff_cells(_parsed("old"), _parsed("new"))
    inserted = diff_cells(_parsed("keep"), _parsed("new", "keep"))
    deleted = diff_cells(_parsed("keep", "gone"), _parsed("keep"))
    unequal_replace = diff_cells(_parsed("old one", "old two"), _parsed("new one"))

    assert [(change.kind, change.old_index, change.new_index) for change in edited] == [
        ("edited", 0, 0)
    ]
    assert [
        (change.kind, change.old_index, change.new_index) for change in inserted
    ] == [("inserted", None, 0)]
    assert [
        (change.kind, change.old_index, change.new_index) for change in deleted
    ] == [("deleted", 1, None)]
    assert [
        (change.kind, change.old_index, change.new_index) for change in unequal_replace
    ] == [
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
    changes = diff_cells(
        _parsed("x = 1", "y = 2"), _parsed("new = 0", "x = 10", "y = 2")
    )

    assert [
        (change.kind, change.old_index, change.new_index) for change in changes
    ] == [
        ("inserted", None, 0),
        ("edited", 0, 1),
    ]


def test_large_replace_block_uses_linear_positional_fallback():
    old = _parsed(*(f"old value {index}" for index in range(100)))
    current = _parsed(*(f"new value {index}" for index in range(100)))

    with patch(
        "jupyter_jcli.cell_alignment._cell_edit_cost",
        side_effect=AssertionError("large replace block allocated similarity DP"),
    ):
        changes = diff_cells(old, current)

    assert len(changes) == 100
    assert all(change.kind == "edited" for change in changes)
    assert [(change.old_index, change.new_index) for change in changes] == [
        (index, index) for index in range(100)
    ]


def test_long_cell_edit_uses_bounded_similarity_input():
    compared_lengths = []

    def recording_matcher(*args, **kwargs):
        if isinstance(args[1], str):
            compared_lengths.extend((len(args[1]), len(args[2])))
        return RealSequenceMatcher(*args, **kwargs)

    with patch(
        "jupyter_jcli.cell_alignment.SequenceMatcher", side_effect=recording_matcher
    ):
        changes = diff_cells(_parsed("a" * 10_000 + "x"), _parsed("a" * 10_000 + "y"))

    assert [
        (change.kind, change.old_index, change.new_index) for change in changes
    ] == [("edited", 0, 0)]
    assert compared_lengths
    assert max(compared_lengths) <= 512


def test_large_equal_repeated_sequence_skips_sequence_matcher():
    old = _parsed(*("same" for _ in range(4_000)))
    current = _parsed(*("same" for _ in range(4_000)))

    with patch(
        "jupyter_jcli.cell_alignment.SequenceMatcher",
        side_effect=AssertionError("equal sequence used SequenceMatcher"),
    ):
        assert diff_cells(old, current) == []


def test_large_repeated_sequence_with_sparse_edits_uses_linear_path():
    old = _parsed(*("same" for _ in range(4_000)))
    current_sources = [cell.source for cell in old.cells]
    current_sources[100] = "changed first"
    current_sources[3_900] = "changed last"
    current = _parsed(*current_sources)

    with patch(
        "jupyter_jcli.cell_alignment.SequenceMatcher",
        side_effect=AssertionError("sparse sequence used SequenceMatcher"),
    ):
        changes = diff_cells(old, current)

    assert [
        (change.kind, change.old_index, change.new_index) for change in changes
    ] == [
        ("edited", 100, 100),
        ("edited", 3_900, 3_900),
    ]


def test_large_unique_sequence_preserves_nearby_insert_and_delete_alignment():
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


def test_large_replace_fallback_detects_leading_insertion_before_edits():
    old = _parsed(*(f"value_{index} = 0" for index in range(101)))
    current = _parsed(
        "inserted = True",
        *(f"value_{index} = 1" for index in range(101)),
    )

    autojunk_values = []

    def recording_matcher(*args, **kwargs):
        autojunk_values.append(kwargs.get("autojunk"))
        return RealSequenceMatcher(*args, **kwargs)

    with patch(
        "jupyter_jcli.cell_alignment.SequenceMatcher", side_effect=recording_matcher
    ):
        changes = diff_cells(old, current)

    assert autojunk_values[0] is True
    assert (changes[0].kind, changes[0].old_index, changes[0].new_index) == (
        "inserted",
        None,
        0,
    )
    assert [
        (change.kind, change.old_index, change.new_index) for change in changes[1:]
    ] == [("edited", index, index + 1) for index in range(101)]


def test_large_replace_fallback_detects_leading_deletion_before_edits():
    old = _parsed(
        "removed = True",
        *(f"value_{index} = 0" for index in range(101)),
    )
    current = _parsed(*(f"value_{index} = 1" for index in range(101)))

    changes = diff_cells(old, current)

    assert (changes[0].kind, changes[0].old_index, changes[0].new_index) == (
        "deleted",
        0,
        None,
    )
    assert [
        (change.kind, change.old_index, change.new_index) for change in changes[1:]
    ] == [("edited", index + 1, index) for index in range(101)]


def test_large_replace_fallback_detects_trailing_insert_and_delete():
    old = _parsed(*(f"value_{index} = 0" for index in range(101)))
    edited = [f"value_{index} = 1" for index in range(101)]

    inserted = diff_cells(old, _parsed(*edited, "trailing_insert = True"))
    deleted = diff_cells(
        _parsed(*(cell.source for cell in old.cells), "trailing_delete = True"),
        _parsed(*edited),
    )

    assert (inserted[-1].kind, inserted[-1].old_index, inserted[-1].new_index) == (
        "inserted",
        None,
        101,
    )
    assert (deleted[-1].kind, deleted[-1].old_index, deleted[-1].new_index) == (
        "deleted",
        101,
        None,
    )


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
    assert (
        "changes: edited current[0]; inserted current[1]; deleted [old:0 at current:0]"
        in human
    )
    assert "legend: ~ edited | + inserted | - deleted" in human
    assert "~ 0 [code]" in human
    assert "+ 1 [code]" in human
    assert "- old:0 at current:0 [code]" in human
    assert "source='gone = 1'" in human


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
