"""Tests for shared parser helpers."""

import pytest

from jupyter_jcli.parser import parse_cell_spec


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
