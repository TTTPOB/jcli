"""Tests for the shared output extraction protocol."""

import json
from pathlib import Path

import pytest

from jupyter_jcli.outputs import (
    DEFAULT_TEXT_LIMIT,
    SCHEMA_VERSION,
    OutputProtocolError,
    list_outputs,
    read_output,
    select_mime_type,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "outputs" / "mixed_outputs.json"
_SOURCE = {"kind": "notebook", "path": "/work/report.ipynb", "cell_index": 4}


@pytest.fixture
def mixed_outputs():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_directory_preserves_physical_indices_and_all_mime_types(mixed_outputs):
    result = list_outputs(mixed_outputs, source=_SOURCE)

    assert result["schema_version"] == SCHEMA_VERSION
    assert result["source"] == _SOURCE
    assert [entry["output_index"] for entry in result["outputs"]] == [0, 1, 2, 3]
    assert result["outputs"][0] == {
        "output_index": 0,
        "output_type": "stream",
        "available_mime_types": [],
        "name": "stderr",
    }
    assert result["outputs"][1]["available_mime_types"] == list(
        mixed_outputs[1]["data"]
    )
    assert "application/octet-stream" in result["outputs"][1]["available_mime_types"]
    assert result["outputs"][2]["execution_count"] == 7
    assert result["outputs"][3]["ename"] == "ValueError"


def test_auto_selection_prefers_raster_and_honors_adapter_capabilities(mixed_outputs):
    default = read_output(mixed_outputs, 1, source=_SOURCE)
    text_adapter = read_output(
        mixed_outputs,
        1,
        source=_SOURCE,
        supported_mime_types={"text/html", "text/plain"},
    )

    assert default["selected"] == {
        "mime_type": "image/png",
        "encoding": "base64",
        "bytes": 8,
        "data": "iVBORw0KGgo=",
    }
    assert text_adapter["selected"]["mime_type"] == "text/html"
    assert text_adapter["selected"]["data"] == "<strong>raw html</strong>"


def test_explicit_mime_is_exact_and_preserves_json_structure(mixed_outputs):
    result = read_output(mixed_outputs, 1, source=_SOURCE, mime_type="application/json")

    assert result["selected"]["encoding"] == "json"
    assert result["selected"]["data"] == {"ok": True, "items": [1, 2]}
    assert result["selected"]["bytes"] == len(b'{"ok":true,"items":[1,2]}')
    with pytest.raises(OutputProtocolError, match="not found") as missing:
        read_output(mixed_outputs, 1, source=_SOURCE, mime_type="text/csv")
    assert missing.value.code == "MIME_NOT_FOUND"


def test_text_windows_are_character_based_and_report_truncation(mixed_outputs):
    result = read_output(
        mixed_outputs,
        0,
        source=_SOURCE,
        offset=6,
        limit=9,
    )

    assert result["stream"] == {
        "name": "stderr",
        "bytes": 9,
        "data": "line\nseco",
        "offset": 6,
        "returned_characters": 9,
        "total_characters": 23,
        "truncated": True,
        "next_offset": 15,
    }


def test_html_markdown_plain_and_svg_are_returned_verbatim(mixed_outputs):
    expected = {
        "text/html": "<strong>raw html</strong>",
        "text/markdown": "**raw markdown**",
        "text/plain": "plain text",
        "image/svg+xml": '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
    }

    for mime_type, value in expected.items():
        result = read_output(
            mixed_outputs, 1, source=_SOURCE, mime_type=mime_type, limit=None
        )
        assert result["selected"]["data"] == value
        assert result["selected"]["truncated"] is False


def test_stream_error_metadata_and_execution_count_keep_their_structure(mixed_outputs):
    stream = read_output(mixed_outputs, 0, source=_SOURCE, limit=None)
    execute_result = read_output(mixed_outputs, 2, source=_SOURCE)
    error = read_output(mixed_outputs, 3, source=_SOURCE)

    assert stream["stream"]["name"] == "stderr"
    assert stream["stream"]["data"] == "first line\nsecond line\n"
    assert execute_result["metadata"] == {"jcli": "fixture"}
    assert execute_result["execution_count"] == 7
    assert error["error"] == {
        "ename": "ValueError",
        "evalue": "bad value",
        "traceback": ["Traceback line 1", "ValueError: bad value"],
    }


def test_json_and_images_cannot_be_partially_returned(mixed_outputs):
    with pytest.raises(OutputProtocolError) as json_window:
        read_output(
            mixed_outputs,
            2,
            source=_SOURCE,
            mime_type="application/json",
            limit=1,
        )
    assert json_window.value.code == "WINDOW_NOT_SUPPORTED"

    with pytest.raises(OutputProtocolError) as image_window:
        read_output(mixed_outputs, 1, source=_SOURCE, limit=1)
    assert image_window.value.code == "WINDOW_NOT_SUPPORTED"


def test_unknown_mime_is_discoverable_but_not_silently_selected(mixed_outputs):
    assert (
        "application/octet-stream"
        in list_outputs(mixed_outputs, source=_SOURCE)["outputs"][1][
            "available_mime_types"
        ]
    )

    with pytest.raises(OutputProtocolError) as unsupported:
        read_output(
            mixed_outputs,
            1,
            source=_SOURCE,
            mime_type="application/octet-stream",
        )
    assert unsupported.value.code == "MIME_NOT_SUPPORTED"


def test_invalid_image_and_transport_overflow_are_structured_errors(mixed_outputs):
    mixed_outputs[1]["data"]["image/png"] = "bm90IGEgcG5n"
    with pytest.raises(OutputProtocolError) as invalid:
        read_output(mixed_outputs, 1, source=_SOURCE)
    assert invalid.value.code == "OUTPUT_DATA_INVALID"

    with pytest.raises(OutputProtocolError) as too_large:
        read_output(
            [{"output_type": "stream", "name": "stdout", "text": "abcdef"}],
            0,
            source=_SOURCE,
            limit=DEFAULT_TEXT_LIMIT,
            max_transport_bytes=64,
        )
    assert too_large.value.code == "OUTPUT_TOO_LARGE"


def test_selection_order_places_json_before_svg():
    assert select_mime_type(["image/svg+xml", "application/vnd.test+json"]) == (
        "application/vnd.test+json"
    )
