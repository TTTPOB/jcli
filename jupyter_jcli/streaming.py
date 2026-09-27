"""Small synchronous helpers for live output and notebook checkpoints."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Mapping
from typing import Any

import click

from jupyter_jcli.executor import format_outputs_human, strip_ansi, summarize_outputs

HUMAN_STREAM_FLUSH_INTERVAL = 0.2
HUMAN_STREAM_BUFFER_BYTES = 4 * 1024
HUMAN_OUTPUT_CHAR_LIMIT = 4_000
HUMAN_OUTPUT_ITEM_LIMIT = 20
NOTEBOOK_CHECKPOINT_INTERVAL = 10.0
NOTEBOOK_CHECKPOINT_SIZE_BYTES = 1024 * 1024
NOTEBOOK_CHECKPOINT_MIN_INTERVAL = 1.0

_OUTPUT_MESSAGE_TYPES = {
    "stream",
    "execute_result",
    "display_data",
    "update_display_data",
    "error",
    "clear_output",
}


def estimate_message_bytes(value: Any) -> int:
    """Estimate bytes in one message without serializing output history."""
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, Mapping):
        return sum(
            estimate_message_bytes(key) + estimate_message_bytes(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return sum(estimate_message_bytes(item) for item in value)
    return len(str(value).encode("utf-8"))


class NotebookCheckpoint:
    """Throttle notebook writeback for one executing cell."""

    def __init__(
        self,
        writeback: Callable[[str, list[dict]], str | None],
        ipynb_path: str,
        cell_index: int,
        expected_cell_id: str | None,
        expected_source: str,
        *,
        had_saved_outputs: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.writeback = writeback
        self.ipynb_path = ipynb_path
        self.cell_index = cell_index
        self.expected_cell_id = expected_cell_id
        self.expected_source = expected_source
        self.had_saved_outputs = had_saved_outputs
        self.clock = clock
        self.last_saved_at = clock()
        self.dirty = False
        self.bytes_since_save = 0

    def observe(
        self,
        message: dict | None,
        outputs: list[dict],
        changed_indices: set[int],
        execution_count: int | None,
    ) -> None:
        """Checkpoint changed output state on time or size thresholds."""
        if message is not None:
            msg_type = message.get("header", {}).get("msg_type")
            if msg_type in _OUTPUT_MESSAGE_TYPES:
                changed = bool(changed_indices)
                if msg_type == "clear_output" and not message.get("content", {}).get(
                    "wait", False
                ):
                    changed = changed or self.had_saved_outputs
                if changed:
                    self.dirty = True
                    self.bytes_since_save += estimate_message_bytes(message)

        if not self.dirty:
            return

        now = self.clock()
        elapsed = now - self.last_saved_at
        time_due = elapsed >= NOTEBOOK_CHECKPOINT_INTERVAL
        size_due = (
            self.bytes_since_save >= NOTEBOOK_CHECKPOINT_SIZE_BYTES
            and elapsed >= NOTEBOOK_CHECKPOINT_MIN_INTERVAL
        )
        if not (time_due or size_due):
            return

        updated = self.writeback(
            self.ipynb_path,
            [
                {
                    "cell_index": self.cell_index,
                    "expected_cell_id": self.expected_cell_id,
                    "expected_source": self.expected_source,
                    "raw_outputs": outputs,
                    "execution_count": execution_count,
                }
            ],
        )
        if updated is None:
            raise RuntimeError(f"Notebook writeback failed: {self.ipynb_path}")

        self.last_saved_at = self.clock()
        self.dirty = False
        self.bytes_since_save = 0
        self.had_saved_outputs = bool(outputs)


class HumanOutputStreamer:
    """Render live IOPub output with bounded buffering and final deduplication."""

    def __init__(
        self,
        *,
        write_text: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        char_limit: int = HUMAN_OUTPUT_CHAR_LIMIT,
        item_limit: int = HUMAN_OUTPUT_ITEM_LIMIT,
        flush_interval: float = HUMAN_STREAM_FLUSH_INTERVAL,
        buffer_bytes: int = HUMAN_STREAM_BUFFER_BYTES,
    ) -> None:
        self._sink = write_text or self._write_stdout
        self._at_line_start = True
        self.clock = clock
        self.char_limit = char_limit
        self.item_limit = item_limit
        self.flush_interval = flush_interval
        self.buffer_bytes = buffer_bytes
        self.characters_written = 0
        self.items_seen = 0
        self.truncated = False
        self._notice_shown = False
        self._pending = ""
        self._pending_name: str | None = None
        self._pending_since: float | None = None
        self._cell_index: int | None = None
        self._covered: set[int] = set()
        self._counted: set[int] = set()

    def write_text(self, text: str) -> None:
        self._sink(text)
        if text:
            self._at_line_start = text.endswith(("\n", "\r"))

    def _start_line(self) -> None:
        if not self._at_line_start:
            self.write_text("\n")

    @staticmethod
    def _write_stdout(text: str) -> None:
        click.echo(text, nl=False)
        sys.stdout.flush()

    def start_cell(self, cell_index: int) -> None:
        """Start a human-visible cell section before its first output."""
        self._finish_pending(terminate=True)
        self._cell_index = cell_index
        self._covered.clear()
        self._counted.clear()
        self.characters_written = 0
        self.items_seen = 0
        self.truncated = False
        self._notice_shown = False
        self.write_text(f"--- cell {cell_index} ---\n")

    def observe(
        self,
        cell_index: int | None,
        message: dict | None,
        outputs: list[dict],
        changed_indices: set[int],
        _execution_count: int | None,
    ) -> None:
        """Consume one IOPub message or an empty poll tick."""
        if cell_index != self._cell_index:
            self._finish_pending(terminate=True)
            self._cell_index = cell_index
            self._covered.clear()
            self._counted.clear()

        if message is None:
            self._flush_if_due()
            return

        msg_type = message.get("header", {}).get("msg_type")
        if msg_type == "clear_output":
            if not message.get("content", {}).get("wait", False):
                self._finish_pending(terminate=True)
                self._covered.clear()
                self._counted.clear()
                self._start_line()
                self._write_counted("[output cleared]\n")
            self._flush_if_due()
            return

        if msg_type == "stream":
            name = str(message.get("content", {}).get("name", "stdout"))
            if self._pending_name is not None and name != self._pending_name:
                self._finish_pending(terminate=False)
            self._pending_name = name
            for index in changed_indices:
                if not 0 <= index < len(outputs):
                    continue
                self._covered.add(index)
                if not self._count_item(index):
                    continue
                raw_text = message.get("content", {}).get("text", "")
                if isinstance(raw_text, list):
                    raw_text = "".join(str(part) for part in raw_text)
                self._append_stream(strip_ansi(str(raw_text)))
            self._flush_if_due()
            return

        self._finish_pending(terminate=True)
        for index in changed_indices:
            if not 0 <= index < len(outputs):
                continue
            self._covered.add(index)
            if not self._count_item(index):
                continue
            raw_output = outputs[index]
            prefix = (
                "[display output updated]\n"
                if msg_type == "update_display_data"
                else ""
            )
            self._render_raw_output(raw_output, prefix=prefix)
        self._flush_if_due()

    def finish_cell(self, event, *, location: str | None = None) -> None:
        """Flush final fragments and render outputs not already shown live."""
        self._finish_pending(terminate=True)
        unseen = [
            output
            for index, output in enumerate(event.raw_outputs)
            if index not in self._covered
        ]
        if unseen:
            self._render_unseen(unseen, location=location)

        notices = [
            output for output in event.outputs if output.get("type") == "summary_notice"
        ]
        for notice in notices:
            self._render_notice(notice)

        parts = []
        if event.notebook_created:
            parts.append(f"Notebook created: {event.notebook_created}")
        if event.notebook_updated:
            parts.append("Notebook updated")
        if (
            event.notebook_cell_index is not None
            and event.notebook_cell_index != event.cell_index
        ):
            parts.append(f"Notebook cell: {event.notebook_cell_index}")
        if event.output_manifest:
            parts.append(f"Outputs saved: {event.output_manifest}")
        if self.truncated and location:
            parts.append(f"Full outputs: {location}")
        for part in parts:
            self._start_line()
            self.write_text(f"{part}\n")

        self._cell_index = None
        self._covered.clear()
        self._counted.clear()

    def finish_inline(
        self,
        raw_outputs: list[dict],
        summary_outputs: list[dict],
        *,
        output_manifest: str | None = None,
    ) -> None:
        """Flush final inline fragments and print persistence/truncation notices."""
        self._finish_pending(terminate=True)
        unseen = [
            output
            for index, output in enumerate(raw_outputs)
            if index not in self._covered
        ]
        if unseen:
            self._render_unseen(unseen, location=output_manifest)
        for notice in summary_outputs:
            if notice.get("type") == "summary_notice":
                self._render_notice(notice)
        if output_manifest:
            self._start_line()
            self.write_text(f"Outputs saved: {output_manifest}\n")

    def report_outputs_saved(self, manifest_path: str) -> None:
        """Report storage created after the live display budget was reached."""
        self._start_line()
        self.write_text(f"Outputs saved: {manifest_path}\n")

    def _count_item(self, index: int) -> bool:
        if index in self._counted:
            return True
        self._counted.add(index)
        if self.items_seen >= self.item_limit:
            self._mark_truncated()
            return False
        self.items_seen += 1
        return True

    def _append_stream(self, text: str) -> None:
        if not text:
            return
        available = max(
            0, self.char_limit - self.characters_written - len(self._pending)
        )
        accepted = text[:available]
        if accepted:
            if not self._pending:
                self._pending_since = self.clock()
            self._pending += accepted
        if len(accepted) != len(text):
            self._mark_truncated()

        while self._pending:
            newline_positions = [
                position
                for delimiter in ("\n", "\r")
                if (position := self._pending.find(delimiter)) >= 0
            ]
            if not newline_positions:
                break
            end = min(newline_positions) + 1
            self._flush_pending_text(end)

        if len(self._pending.encode("utf-8")) >= self.buffer_bytes:
            self._finish_pending(terminate=False)

    def _flush_if_due(self) -> None:
        if (
            self._pending
            and self._pending_since is not None
            and self.clock() - self._pending_since >= self.flush_interval
        ):
            self._finish_pending(terminate=False)

    def _flush_pending_text(self, end: int) -> None:
        text = self._pending[:end]
        self._pending = self._pending[end:]
        self.characters_written += len(text)
        self.write_text(text)
        self._pending_since = self.clock() if self._pending else None
        if not self._pending:
            self._pending_name = None

    def _finish_pending(self, *, terminate: bool) -> None:
        if not self._pending:
            self._pending_name = None
            self._pending_since = None
            return
        text = self._pending
        self._pending = ""
        self._pending_name = None
        self._pending_since = None
        self.characters_written += len(text)
        self.write_text(text)
        if terminate:
            self._start_line()

    def _render_raw_output(self, raw_output: dict, *, prefix: str = "") -> None:
        remaining = max(
            0, self.char_limit - self.characters_written - len(self._pending)
        )
        items_remaining = max(1, self.item_limit - self.items_seen + 1)
        summary = summarize_outputs(
            [raw_output], text_limit=remaining, item_limit=items_remaining
        )
        notices = [item for item in summary if item.get("type") == "summary_notice"]
        visible = [item for item in summary if item.get("type") != "summary_notice"]
        if notices:
            self._mark_truncated()
        text = prefix + format_outputs_human(visible)
        if text:
            self._start_line()
            self._write_counted(text + "\n")
        if raw_output.get("output_type") == "error":
            traceback = raw_output.get("traceback", [])
            if isinstance(traceback, list):
                self.items_seen += len(traceback)

    def _render_unseen(self, raw_outputs: list[dict], *, location: str | None) -> None:
        remaining = max(
            0, self.char_limit - self.characters_written - len(self._pending)
        )
        items_remaining = max(0, self.item_limit - self.items_seen)
        summary = summarize_outputs(
            raw_outputs,
            text_limit=remaining,
            item_limit=items_remaining,
            full_output_location=location,
        )
        notices = [item for item in summary if item.get("type") == "summary_notice"]
        visible = [item for item in summary if item.get("type") != "summary_notice"]
        for output in raw_outputs:
            if output.get("output_type") == "error" and isinstance(
                output.get("traceback", []), list
            ):
                self.items_seen += len(output.get("traceback", []))
        if visible:
            text = format_outputs_human(visible)
            if text:
                self._start_line()
                self._write_counted(text + "\n")
        if notices:
            self._mark_truncated()
            for notice in notices:
                self._render_notice(notice)

    def _render_notice(self, notice: dict) -> None:
        self.truncated = True
        location = notice.get("complete_outputs")
        if not self._notice_shown:
            text = format_outputs_human([notice])
            if text:
                self._start_line()
                self.write_text(f"{text}\n")
            self._notice_shown = True
        elif location:
            self._start_line()
            self.write_text(f"Full outputs: {location}\n")

    def _write_counted(self, text: str) -> None:
        available = max(0, self.char_limit - self.characters_written)
        visible = text[:available]
        if visible:
            self.write_text(visible)
            self.characters_written += len(visible)
        if len(visible) != len(text):
            self._mark_truncated()

    def _mark_truncated(self) -> None:
        self.truncated = True
        if self._notice_shown:
            return
        self._finish_pending(terminate=False)
        self._start_line()
        self.write_text(
            f"[Live output limited to {self.char_limit} characters or "
            f"{self.item_limit} outputs; final result location follows if saved.]\n"
        )
        self._notice_shown = True
