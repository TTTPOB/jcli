"""Tests for shared py/notebook pair state."""

import pytest

from jupyter_jcli.formats.model import Cell, ParsedFile
from jupyter_jcli.pair_state import (
    PairState,
    merge_metadata,
    metadata_with_local_fields,
)


def _state(metadata, source="x = 1"):
    parsed = ParsedFile(cells=[Cell(0, "code", source)])
    parsed.notebook.metadata = metadata
    return PairState.from_parsed(parsed, include_cell_ids=False)


def test_pair_state_shares_file_metadata_but_not_cell_metadata():
    left = _state({"kernelspec": {"name": "env", "display_name": "Left"}})
    right = _state({"kernelspec": {"name": "env", "display_name": "Right"}})
    left.cells[0].node.metadata["tags"] = ["left"]

    assert left != right
    right.metadata["kernelspec"]["display_name"] = "Left"
    assert left == right


def test_projection_excludes_only_declared_local_paths():
    state = _state(
        {
            "language_info": {"name": "python", "version": "3.12"},
            "widgets": {"state": "local"},
            "jupytext": {"formats": "ipynb,py:percent"},
            "vscode": {"interpreter": "local"},
            "colab": {"gpu": True},
            "custom": {"value": None},
        }
    )

    assert state.metadata == {
        "language_info": {"name": "python"},
        "custom": {"value": None},
    }


def test_canonical_text_is_deterministic_and_includes_shared_metadata():
    left = _state({"z": 1, "kernelspec": {"name": "env", "language": "julia"}})
    right = _state({"kernelspec": {"language": "julia", "name": "env"}, "z": 1})

    assert left.canonical_text() == right.canonical_text()
    assert "language: julia" in left.canonical_text()
    assert "kernelspec" not in left.cell_text()


def test_metadata_merge_combines_disjoint_mapping_edits_and_deletion():
    merged, conflicts = merge_metadata(
        {"custom": {"keep": 1, "delete": 2}},
        {"custom": {"keep": 10}},
        {"custom": {"keep": 1, "delete": 2, "added": None}},
    )

    assert conflicts == []
    assert merged == {"custom": {"keep": 10, "added": None}}


def test_metadata_merge_distinguishes_missing_from_null_conflict():
    merged, conflicts = merge_metadata(
        {"custom": {"value": 1}},
        {"custom": {}},
        {"custom": {"value": None}},
    )

    assert merged is None
    assert conflicts[0].display_path == "metadata.custom.value"
    assert conflicts[0].py == "<missing>"
    assert conflicts[0].notebook is None


def test_kernel_and_language_info_merge_as_atomic_unit():
    merged, conflicts = merge_metadata(
        {
            "kernelspec": {"name": "base", "display_name": "Base"},
            "language_info": {"name": "python"},
        },
        {
            "kernelspec": {"name": "py", "display_name": "Py"},
            "language_info": {"name": "python"},
        },
        {
            "kernelspec": {"name": "nb", "display_name": "Notebook"},
            "language_info": {"name": "julia"},
        },
    )

    assert merged is None
    assert [conflict.path for conflict in conflicts] == [("kernel",)]


def test_local_metadata_is_retained_but_runtime_state_invalidates_on_kernel_change():
    current = {
        "kernelspec": {"name": "old"},
        "language_info": {"name": "python", "version": "3.12"},
        "widgets": {"state": "old"},
        "vscode": {"keep": True},
    }
    updated = metadata_with_local_fields(
        current,
        {
            "kernelspec": {"name": "new"},
            "language_info": {"name": "julia"},
        },
    )

    assert updated == {
        "kernelspec": {"name": "new"},
        "language_info": {"name": "julia"},
        "vscode": {"keep": True},
    }


def test_pair_state_rejects_non_string_metadata_keys():
    with pytest.raises(TypeError, match="keys must be strings"):
        _state({1: "invalid"})


def test_pair_state_distinguishes_booleans_from_numbers():
    assert _state({"custom": True}) != _state({"custom": 1})
    assert _state({"custom": False}) != _state({"custom": 0})


def test_metadata_merge_does_not_treat_boolean_as_unchanged_number():
    merged, conflicts = merge_metadata(
        {"custom": {"value": False}},
        {"custom": {"value": 0}},
        {"custom": {"value": False}},
    )

    assert conflicts == []
    assert merged == {"custom": {"value": 0}}

    merged, conflicts = merge_metadata(
        {"custom": {"value": None}},
        {"custom": {"value": True}},
        {"custom": {"value": 1}},
    )
    assert merged is None
    assert conflicts[0].display_path == "metadata.custom.value"
