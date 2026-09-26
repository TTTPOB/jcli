"""Tests for shared enums in jupyter_jcli._enums."""

from __future__ import annotations

import dataclasses
import json

import pytest

from jupyter_jcli._enums import (
    CellType,
    DriftStatus,
    MergeMode,
    OutputPolicy,
    OutputType,
    ResponseStatus,
)
from jupyter_jcli.diff import (
    BaselineAvailable,
    BaselineMissing,
    Conflict,
    DriftOnly,
    InSync,
    Merged,
)
from jupyter_jcli.pair_state import PairState

# ---------------------------------------------------------------------------
# DriftStatus
# ---------------------------------------------------------------------------


class TestDriftStatus:
    def test_concrete_results_expose_fixed_read_only_status(self):
        results = [
            (InSync(BaselineAvailable()), DriftStatus.IN_SYNC),
            (Merged(PairState([], {}), False, False), DriftStatus.MERGED),
            (Conflict([], ""), DriftStatus.CONFLICT),
            (DriftOnly(""), DriftStatus.DRIFT_ONLY),
        ]
        for result, expected in results:
            assert result.status is expected
            with pytest.raises((AttributeError, TypeError)):
                result.status = DriftStatus.IN_SYNC

    def test_concrete_result_fields_are_disjoint(self):
        assert [field.name for field in dataclasses.fields(BaselineAvailable)] == []
        assert [field.name for field in dataclasses.fields(BaselineMissing)] == [
            "seed_text"
        ]
        assert [field.name for field in dataclasses.fields(InSync)] == ["baseline"]
        assert [field.name for field in dataclasses.fields(Merged)] == [
            "target_state",
            "py_needs_update",
            "ipynb_needs_update",
            "merge_mode",
        ]
        assert [field.name for field in dataclasses.fields(Conflict)] == [
            "conflict_indices",
            "diff_text",
            "metadata_conflicts",
        ]
        assert [field.name for field in dataclasses.fields(DriftOnly)] == ["diff_text"]

    def test_in_sync_requires_explicit_baseline(self):
        with pytest.raises(TypeError):
            InSync()

        available = InSync(BaselineAvailable())
        missing = InSync(BaselineMissing(seed_text=""))
        assert isinstance(available.baseline, BaselineAvailable)
        assert isinstance(missing.baseline, BaselineMissing)
        assert missing.baseline.seed_text == ""

    def test_concrete_results_reject_status_constructor_argument(self):
        with pytest.raises(TypeError, match="status"):
            InSync(baseline=BaselineAvailable(), status=DriftStatus.IN_SYNC)


# ---------------------------------------------------------------------------
# MergeMode
# ---------------------------------------------------------------------------


class TestMergeMode:
    def test_merged_defaults_to_three_way(self):
        r = Merged(PairState([], {}), False, False)
        assert r.merge_mode is MergeMode.THREE_WAY

    def test_merged_coerces_merge_mode(self):
        r = Merged(PairState([], {}), False, False, merge_mode="three_way")
        assert r.merge_mode is MergeMode.THREE_WAY

    def test_merged_rejects_invalid_merge_mode(self):
        with pytest.raises(ValueError):
            Merged(PairState([], {}), False, False, merge_mode="bogus")


# These values cross notebook, CLI, and JSON protocol boundaries.
@pytest.mark.parametrize(
    ("member", "value"),
    [
        (DriftStatus.IN_SYNC, "in_sync"),
        (DriftStatus.MERGED, "merged"),
        (DriftStatus.CONFLICT, "conflict"),
        (DriftStatus.DRIFT_ONLY, "drift_only"),
        (MergeMode.THREE_WAY, "three_way"),
        (CellType.CODE, "code"),
        (CellType.MARKDOWN, "markdown"),
        (CellType.RAW, "raw"),
        (OutputType.STREAM, "stream"),
        (OutputType.EXECUTE_RESULT, "execute_result"),
        (OutputType.DISPLAY_DATA, "display_data"),
        (OutputType.ERROR, "error"),
        (OutputPolicy.PRESERVE, "preserve"),
        (OutputPolicy.CLEAR_EDITED, "clear-edited"),
        (OutputPolicy.CLEAR_ALL, "clear-all"),
        (ResponseStatus.OK, "ok"),
        (ResponseStatus.NOOP, "noop"),
        (ResponseStatus.ERROR, "error"),
    ],
)
def test_external_enum_values(member, value):
    assert member.value == value


def test_enum_values_serialize_in_json_protocols():
    assert json.dumps({"status": ResponseStatus.OK, "cell_type": CellType.CODE}) == (
        '{"status": "ok", "cell_type": "code"}'
    )
