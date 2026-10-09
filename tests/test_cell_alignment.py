"""Cell alignment behavior and bounded-work regressions."""

import subprocess
import sys
from difflib import SequenceMatcher as RealSequenceMatcher
from unittest.mock import patch

from jupyter_jcli._enums import AlignmentMethod, CellChangeKind, CellType
from jupyter_jcli.diff import align_cells
from jupyter_jcli.formats.model import Cell, ParsedFile
from jupyter_jcli.formats.percent import loads as parse_py_percent_text


def _parsed(*sources: str) -> ParsedFile:
    return ParsedFile(
        kernel_name="python3",
        cells=[
            Cell(index=index, cell_type=CellType.CODE, source=source)
            for index, source in enumerate(sources)
        ],
    )


def test_cell_alignment_reports_id_content_and_position_methods():
    by_id_old = parse_py_percent_text('# %% id="same"\nold\n')
    by_id_current = parse_py_percent_text('# %% id="same"\nnew\n')
    by_id = align_cells(by_id_old, by_id_current)
    by_content = align_cells(_parsed("same"), _parsed("same"))
    by_position = align_cells(_parsed("old"), _parsed("new"))

    assert by_id[0].kind is CellChangeKind.EDITED
    assert by_content[0].kind is CellChangeKind.EQUAL
    assert by_id[0].alignment is AlignmentMethod.ID
    assert by_content[0].alignment is AlignmentMethod.CONTENT
    assert by_position[0].alignment is AlignmentMethod.POSITION


def test_notebook_helpers_import_without_cli_cycle(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from jupyter_jcli.diff import align_cells; print(align_cells.__name__)",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "align_cells"


def _changed_alignments(old: ParsedFile, current: ParsedFile):
    return [
        change
        for change in align_cells(old, current)
        if change.kind is not CellChangeKind.EQUAL
    ]


def test_cell_alignment_classifies_edited_inserted_deleted_and_unequal_replace():
    edited = _changed_alignments(_parsed("old"), _parsed("new"))
    inserted = _changed_alignments(_parsed("keep"), _parsed("new", "keep"))
    deleted = _changed_alignments(_parsed("keep", "gone"), _parsed("keep"))
    unequal_replace = _changed_alignments(
        _parsed("old one", "old two"), _parsed("new one")
    )

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


def test_cell_alignment_pairs_an_edited_cell_with_the_most_similar_insertion_neighbor():
    changes = _changed_alignments(
        _parsed("x = 1", "y = 2"), _parsed("new = 0", "x = 10", "y = 2")
    )

    assert [
        (change.kind, change.old_index, change.new_index) for change in changes
    ] == [
        ("inserted", None, 0),
        ("edited", 0, 1),
    ]


def test_cell_alignment_uses_stable_ids_before_source_similarity():
    old = parse_py_percent_text(
        '# %% id="first"\nshared old\n\n# %% id="second"\nshared old\n'
    )
    current = parse_py_percent_text(
        '# %% id="first"\nfirst rewritten\n\n'
        '# %% id="inserted"\nshared old\n\n'
        '# %% id="second"\nsecond rewritten\n'
    )

    changes = _changed_alignments(old, current)

    assert [
        (change.kind, change.old_index, change.new_index) for change in changes
    ] == [
        ("edited", 0, 0),
        ("inserted", None, 1),
        ("edited", 1, 2),
    ]


def test_cell_alignment_preserves_positions_after_nearby_insert_and_delete():
    old = _parsed(*(f"value_{index}" for index in range(200)))
    current_sources = [cell.source for cell in old.cells]
    current_sources.insert(50, "inserted")
    del current_sources[56]

    alignments = align_cells(old, _parsed(*current_sources))

    expected = [
        *(("equal", index, index) for index in range(50)),
        ("inserted", None, 50),
        *(("equal", index, index + 1) for index in range(50, 55)),
        ("deleted", 55, None),
        *(("equal", index, index) for index in range(56, 200)),
    ]
    assert [
        (change.kind, change.old_index, change.new_index) for change in alignments
    ] == expected


def test_large_replace_block_uses_linear_positional_fallback():
    old = _parsed(*(f"old value {index}" for index in range(100)))
    current = _parsed(*(f"new value {index}" for index in range(100)))

    with patch(
        "jupyter_jcli.diff.alignment._cell_edit_cost",
        side_effect=AssertionError("large replace block allocated similarity DP"),
    ):
        changes = _changed_alignments(old, current)

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
        "jupyter_jcli.diff.alignment.SequenceMatcher", side_effect=recording_matcher
    ):
        changes = _changed_alignments(
            _parsed("a" * 10_000 + "x"), _parsed("a" * 10_000 + "y")
        )

    assert [
        (change.kind, change.old_index, change.new_index) for change in changes
    ] == [("edited", 0, 0)]
    assert compared_lengths
    assert max(compared_lengths) <= 512


def test_large_equal_repeated_sequence_skips_sequence_matcher():
    old = _parsed(*("same" for _ in range(4_000)))
    current = _parsed(*("same" for _ in range(4_000)))

    with patch(
        "jupyter_jcli.diff.alignment.SequenceMatcher",
        side_effect=AssertionError("equal sequence used SequenceMatcher"),
    ):
        alignments = align_cells(old, current)

    assert len(alignments) == 4_000
    assert all(change.kind is CellChangeKind.EQUAL for change in alignments)


def test_large_repeated_sequence_with_sparse_edits_uses_linear_path():
    old = _parsed(*("same" for _ in range(4_000)))
    current_sources = [cell.source for cell in old.cells]
    current_sources[100] = "changed first"
    current_sources[3_900] = "changed last"
    current = _parsed(*current_sources)

    with patch(
        "jupyter_jcli.diff.alignment.SequenceMatcher",
        side_effect=AssertionError("sparse sequence used SequenceMatcher"),
    ):
        alignments = align_cells(old, current)

    assert [
        (change.kind, change.old_index, change.new_index) for change in alignments
    ] == [
        ("edited" if index in {100, 3_900} else "equal", index, index)
        for index in range(4_000)
    ]


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
        "jupyter_jcli.diff.alignment.SequenceMatcher", side_effect=recording_matcher
    ):
        changes = _changed_alignments(old, current)

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

    changes = _changed_alignments(old, current)

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

    inserted = _changed_alignments(old, _parsed(*edited, "trailing_insert = True"))
    deleted = _changed_alignments(
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
