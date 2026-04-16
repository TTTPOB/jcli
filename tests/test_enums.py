"""Tests for shared enums in jupyter_jcli._enums."""

from __future__ import annotations

import json

import pytest

from jupyter_jcli._enums import CellType, DriftStatus, MergeMode, OutputType, ResponseStatus
from jupyter_jcli.drift import DriftResult


# ---------------------------------------------------------------------------
# DriftStatus
# ---------------------------------------------------------------------------

class TestDriftStatus:
    def test_members_exist(self):
        assert DriftStatus.IN_SYNC
        assert DriftStatus.MERGED
        assert DriftStatus.CONFLICT
        assert DriftStatus.DRIFT_ONLY

    def test_str_inheritance(self):
        assert DriftStatus.IN_SYNC == "in_sync"
        assert DriftStatus.MERGED == "merged"
        assert DriftStatus.CONFLICT == "conflict"
        assert DriftStatus.DRIFT_ONLY == "drift_only"
        assert isinstance(DriftStatus.IN_SYNC, str)

    def test_json_serializable(self):
        assert json.dumps(DriftStatus.IN_SYNC) == '"in_sync"'

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            DriftStatus("typo")

    def test_coerce_from_string(self):
        assert DriftStatus("in_sync") is DriftStatus.IN_SYNC

    def test_drift_result_coerces_status(self):
        r = DriftResult(status="in_sync")
        assert r.status is DriftStatus.IN_SYNC
        assert isinstance(r.status, DriftStatus)

    def test_drift_result_accepts_enum(self):
        r = DriftResult(status=DriftStatus.MERGED)
        assert r.status is DriftStatus.MERGED

    def test_drift_result_invalid_status_raises(self):
        with pytest.raises(ValueError):
            DriftResult(status="typo")


# ---------------------------------------------------------------------------
# CellType
# ---------------------------------------------------------------------------

class TestMergeMode:
    def test_members_exist(self):
        assert MergeMode.THREE_WAY
        assert MergeMode.PY_WINS_NO_BASE

    def test_str_inheritance(self):
        assert MergeMode.THREE_WAY == "three_way"
        assert MergeMode.PY_WINS_NO_BASE == "py_wins_no_base"
        assert isinstance(MergeMode.THREE_WAY, str)

    def test_json_serializable(self):
        assert json.dumps(MergeMode.THREE_WAY) == '"three_way"'

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            MergeMode("bogus")

    def test_coerce_from_string(self):
        assert MergeMode("three_way") is MergeMode.THREE_WAY
        assert MergeMode("py_wins_no_base") is MergeMode.PY_WINS_NO_BASE

    def test_drift_result_coerces_merge_mode(self):
        from jupyter_jcli.drift import DriftResult
        r = DriftResult(status="merged", merge_mode="py_wins_no_base")
        assert r.merge_mode is MergeMode.PY_WINS_NO_BASE

    def test_drift_result_defaults_to_three_way(self):
        from jupyter_jcli.drift import DriftResult
        r = DriftResult(status="in_sync")
        assert r.merge_mode is MergeMode.THREE_WAY


class TestCellType:
    def test_members_exist(self):
        assert CellType.CODE
        assert CellType.MARKDOWN
        assert CellType.RAW

    def test_str_inheritance(self):
        assert CellType.CODE == "code"
        assert CellType.MARKDOWN == "markdown"
        assert CellType.RAW == "raw"
        assert isinstance(CellType.CODE, str)

    def test_json_serializable(self):
        assert json.dumps(CellType.CODE) == '"code"'

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            CellType("bogus")

    def test_coerce_from_string(self):
        assert CellType("code") is CellType.CODE


# ---------------------------------------------------------------------------
# OutputType
# ---------------------------------------------------------------------------

class TestOutputType:
    def test_members_exist(self):
        assert OutputType.STREAM
        assert OutputType.EXECUTE_RESULT
        assert OutputType.DISPLAY_DATA
        assert OutputType.ERROR
        assert OutputType.IMAGE
        assert OutputType.HTML

    def test_str_inheritance(self):
        assert OutputType.STREAM == "stream"
        assert OutputType.EXECUTE_RESULT == "execute_result"
        assert OutputType.DISPLAY_DATA == "display_data"
        assert OutputType.ERROR == "error"
        assert isinstance(OutputType.STREAM, str)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            OutputType("bogus")


# ---------------------------------------------------------------------------
# ResponseStatus
# ---------------------------------------------------------------------------

class TestResponseStatus:
    def test_members_exist(self):
        assert ResponseStatus.OK
        assert ResponseStatus.NOOP
        assert ResponseStatus.ERROR

    def test_str_inheritance(self):
        assert ResponseStatus.OK == "ok"
        assert ResponseStatus.NOOP == "noop"
        assert ResponseStatus.ERROR == "error"
        assert isinstance(ResponseStatus.OK, str)

    def test_json_serializable(self):
        assert json.dumps({"status": ResponseStatus.OK}) == '{"status": "ok"}'

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            ResponseStatus("bogus")
