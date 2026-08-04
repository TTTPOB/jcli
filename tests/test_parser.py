"""Tests for shared parser helpers."""

import pytest

from jupyter_jcli.formats.percent import loads as parse_py_percent_text
from jupyter_jcli.parser import find_pair, find_paired_ipynb, parse_cell_spec


@pytest.mark.parametrize(
    ("spec", "num_cells", "expected"),
    [
        ("3", 10, [3]),
        ("3:7", 10, [3, 4, 5, 6]),
        ("3:", 5, [3, 4]),
        (":3", 5, [0, 1, 2]),
        (":", 3, [0, 1, 2]),
        ("2:99", 4, [2, 3]),
        ("5:", 3, []),
    ],
)
def test_parse_cell_spec_accepts_forward_half_open_ranges(spec, num_cells, expected):
    assert parse_cell_spec(spec, num_cells) == expected


@pytest.mark.parametrize(
    "spec",
    ["-1", "-1:2", "1:-2", "3:2", "1:2:3", "::", "1::"],
)
def test_parse_cell_spec_rejects_invalid_boundaries_and_syntax(spec):
    with pytest.raises(ValueError):
        parse_cell_spec(spec, 10)


def test_parse_py_percent_tracks_source_line_ranges():
    parsed = parse_py_percent_text(
        "# ---\n# jupyter:\n# ---\n\n# %%\n\nvalue = 1\nvalue\n\n"
        "# %% [markdown]\n# Title\n# body\n"
    )

    assert [
        (cell.source_start_line, cell.source_end_line) for cell in parsed.cells
    ] == [(7, 8), (11, 12)]


def test_plain_python_has_no_source_line_range():
    cell = parse_py_percent_text("value = 1\n").cells[0]

    assert cell.source_start_line is None
    assert cell.source_end_line is None


def test_pair_discovery_ignores_non_python_file(tmp_path):
    markdown = tmp_path / "notebook.md"
    notebook = tmp_path / "notebook.ipynb"
    markdown.write_text("# Notes\n", encoding="utf-8")
    notebook.write_text("{}", encoding="utf-8")

    assert find_paired_ipynb(markdown) is None
    assert find_pair(markdown) is None
