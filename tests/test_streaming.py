from jupyter_jcli.streaming import HumanOutputStreamer, NotebookCheckpoint


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _stream_message(text, *, name="stdout"):
    return {
        "header": {"msg_type": "stream"},
        "content": {"name": name, "text": text},
    }


def test_human_stream_flushes_at_newline_and_carriage_return():
    writes = []
    streamer = HumanOutputStreamer(write_text=writes.append)
    outputs = []

    for index, text in enumerate(("line\n", "progress\r")):
        outputs.append({"output_type": "stream", "name": "stdout", "text": text})
        streamer.observe(0, _stream_message(text), outputs, {index}, None)

    assert "line\n" in "".join(writes)
    assert "progress\r" in "".join(writes)


def test_human_stream_flushes_on_empty_tick_after_point_two_seconds():
    clock = _Clock()
    writes = []
    streamer = HumanOutputStreamer(write_text=writes.append, clock=clock)
    outputs = [{"output_type": "stream", "name": "stdout", "text": "pending"}]

    streamer.observe(0, _stream_message("pending"), outputs, {0}, None)
    assert "pending" not in "".join(writes)

    clock.advance(0.21)
    streamer.observe(0, None, outputs, set(), None)

    assert "pending" in "".join(writes)


def test_partial_tick_then_final_notices_start_new_line():
    clock = _Clock()
    writes = []
    streamer = HumanOutputStreamer(write_text=writes.append, clock=clock)
    output = {"output_type": "stream", "name": "stdout", "text": "pending"}
    streamer.observe(0, _stream_message("pending"), [output], {0}, None)
    clock.advance(0.21)
    streamer.observe(0, None, [output], set(), None)
    streamer.finish_inline([output], [], output_manifest="final.json")
    assert "pending\nOutputs saved: final.json" in "".join(writes)


def test_human_stream_flushes_four_kibibytes_of_utf8():
    writes = []
    streamer = HumanOutputStreamer(write_text=writes.append)
    text = "界" * 1_400
    outputs = [{"output_type": "stream", "name": "stdout", "text": text}]

    streamer.observe(0, _stream_message(text), outputs, {0}, None)

    assert streamer._pending == ""
    assert text in "".join(writes)


def test_human_stream_uses_one_budget_and_does_not_repeat_live_output():
    writes = []
    streamer = HumanOutputStreamer(write_text=writes.append, char_limit=6)
    outputs = []
    for index, text in enumerate(("abc", "defghi")):
        outputs.append({"output_type": "stream", "name": "stdout", "text": text})
        streamer.observe(0, _stream_message(text), outputs, {index}, None)

    streamer.finish_inline(outputs, [])
    rendered = "".join(writes)
    assert "abcdef" in rendered
    assert rendered.count("abc") == 1
    assert streamer.truncated


def test_human_stream_uses_message_delta_when_kernel_merges_streams():
    writes = []
    streamer = HumanOutputStreamer(write_text=writes.append)
    output = {"output_type": "stream", "name": "stdout", "text": "first\n"}
    streamer.observe(0, _stream_message("first\n"), [output], {0}, None)
    output["text"] += "second\n"
    streamer.observe(0, _stream_message("second\n"), [output], {0}, None)
    streamer.finish_inline([output], [])
    assert "".join(writes).count("first") == 1
    assert "".join(writes).count("second") == 1


def test_human_stream_budget_resets_for_next_cell():
    writes = []
    streamer = HumanOutputStreamer(write_text=writes.append, char_limit=3)
    for index in (0, 1):
        streamer.start_cell(index)
        output = {"output_type": "stream", "name": "stdout", "text": "abc"}
        streamer.observe(index, _stream_message("abc"), [output], {0}, None)
        streamer.finish_inline([output], [])
    assert "".join(writes).count("abc") == 2


def test_human_stream_flushes_final_fragment_without_repeating_it():
    writes = []
    streamer = HumanOutputStreamer(write_text=writes.append)
    outputs = [{"output_type": "stream", "name": "stdout", "text": "tail"}]

    streamer.observe(0, _stream_message("tail"), outputs, {0}, None)
    assert "tail" not in "".join(writes)

    streamer.finish_inline(outputs, [])

    assert "".join(writes).count("tail") == 1


def test_human_stream_formats_rich_output_once_without_materializing_images():
    writes = []
    streamer = HumanOutputStreamer(write_text=writes.append)
    raw_output = {
        "output_type": "display_data",
        "data": {"text/html": "<table></table>"},
        "metadata": {},
    }
    message = {
        "header": {"msg_type": "display_data"},
        "content": {"data": raw_output["data"], "metadata": {}},
    }

    streamer.observe(0, message, [raw_output], {0}, None)
    streamer.finish_inline([raw_output], [])

    assert "[HTML output]" in "".join(writes)
    assert "".join(writes).count("[HTML output]") == 1


def test_notebook_checkpoint_waits_for_dirty_time_and_empty_tick(monkeypatch):
    from jupyter_jcli import streaming

    monkeypatch.setattr(streaming, "NOTEBOOK_CHECKPOINT_INTERVAL", 10.0)
    clock = _Clock()
    writes = []
    checkpoint = NotebookCheckpoint(
        lambda path, results: writes.append((path, results)) or path,
        "test.ipynb",
        2,
        "cell-id",
        "print('saved')",
        clock=clock,
    )
    execute_input = {
        "header": {"msg_type": "execute_input"},
        "content": {"execution_count": 4, "code": "print('saved')"},
    }
    checkpoint.observe(execute_input, [], set(), 4)
    clock.advance(20)
    checkpoint.observe(None, [], set(), 4)
    assert writes == []

    message = _stream_message("saved\n")
    outputs = [{"output_type": "stream", "name": "stdout", "text": "saved\n"}]
    checkpoint.observe(message, outputs, {0}, 4)
    clock.advance(10.1)
    checkpoint.observe(None, outputs, set(), 4)

    assert len(writes) == 1
    assert writes[0][1][0]["execution_count"] == 4
    assert checkpoint.dirty is False
    assert checkpoint.bytes_since_save == 0


def test_notebook_checkpoint_size_threshold_waits_one_second(monkeypatch):
    from jupyter_jcli import streaming

    monkeypatch.setattr(streaming, "NOTEBOOK_CHECKPOINT_INTERVAL", 100.0)
    monkeypatch.setattr(streaming, "NOTEBOOK_CHECKPOINT_SIZE_BYTES", 1)
    monkeypatch.setattr(streaming, "NOTEBOOK_CHECKPOINT_MIN_INTERVAL", 1.0)
    clock = _Clock()
    writes = []
    checkpoint = NotebookCheckpoint(
        lambda path, results: writes.append(results) or path,
        "test.ipynb",
        0,
        "cell-id",
        "print('x')",
        clock=clock,
    )
    output = {"output_type": "stream", "name": "stdout", "text": "x"}

    checkpoint.observe(_stream_message("x"), [output], {0}, None)
    assert writes == []
    clock.advance(0.9)
    checkpoint.observe(None, [output], set(), None)
    assert writes == []
    clock.advance(0.11)
    checkpoint.observe(None, [output], set(), None)

    assert len(writes) == 1
