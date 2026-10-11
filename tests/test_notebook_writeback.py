"""Test notebook output writeback — the core differentiating feature."""

import json
import textwrap
from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import patch

import nbformat
import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.notebook_writer import write_outputs_to_notebook
from jupyter_jcli.outputs.notebook import read_notebook_output


def _jsonl_events(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


class TestPyPercentWriteback:
    """Test that exec --file on .py writes outputs to paired .ipynb."""

    def test_writeback_creates_output_in_ipynb(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        py_file = tmp_path / "analysis.py"
        py_file.write_text(
            textwrap.dedent("""\
            # ---
            # jupyter:
            #   kernelspec:
            #     name: python3
            # ---

            # %%
            print("hello writeback")

            # %%
            40 + 2
        """)
        )

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [
            nbformat.v4.new_code_cell('print("hello writeback")'),
            nbformat.v4.new_code_cell("40 + 2"),
        ]
        nb_path = tmp_path / "analysis.ipynb"
        nbformat.write(nb, nb_path)

        result = runner.invoke(
            main,
            [
                "-s",
                live_session["url"],
                "-t",
                live_session["token"],
                "exec",
                live_session["session_id"],
                "--file",
                str(py_file),
                "--cell",
                "0",
            ],
        )
        assert result.exit_code == 0
        assert "hello writeback" in result.output
        assert "Notebook updated" in result.output
        assert str(nb_path) not in result.output
        assert "Notebook cell:" not in result.output

        updated_nb = nbformat.read(nb_path, as_version=4)
        cell0 = updated_nb.cells[0]
        assert len(cell0.outputs) > 0
        assert any("hello writeback" in str(o) for o in cell0.outputs)

    def test_writeback_multiple_cells(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        py_file = tmp_path / "multi.py"
        py_file.write_text(
            textwrap.dedent("""\
            # ---
            # jupyter:
            #   kernelspec:
            #     name: python3
            # ---

            # %%
            _wb_x = 10

            # %%
            print(_wb_x * 2)

            # %%
            print(_wb_x * 3)
        """)
        )

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [
            nbformat.v4.new_code_cell("_wb_x = 10"),
            nbformat.v4.new_code_cell("print(_wb_x * 2)"),
            nbformat.v4.new_code_cell("print(_wb_x * 3)"),
        ]
        nb_path = tmp_path / "multi.ipynb"
        nbformat.write(nb, nb_path)

        result = runner.invoke(
            main,
            [
                "-s",
                live_session["url"],
                "-t",
                live_session["token"],
                "--json",
                "exec",
                live_session["session_id"],
                "--file",
                str(py_file),
                "--cell",
                "0:3",
            ],
        )
        assert result.exit_code == 0
        events = _jsonl_events(result.output)
        cell_events = [event for event in events if "cell" in event]
        summary = next(event["summary"] for event in events if "summary" in event)
        assert len(cell_events) == 3
        assert all(event["notebook_updated"] is True for event in cell_events)
        assert summary["notebook_updated"] is True
        assert all("notebook_cell_index" not in event["cell"] for event in cell_events)

        updated_nb = nbformat.read(nb_path, as_version=4)
        assert any("20" in str(o) for o in updated_nb.cells[1].outputs)
        assert any("30" in str(o) for o in updated_nb.cells[2].outputs)

    def test_writeback_happens_after_each_completed_cell(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        py_file = tmp_path / "streaming.py"
        py_file.write_text(
            textwrap.dedent("""\
            # %%
            print("first")

            # %%
            print("second")
        """)
        )

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [
            nbformat.v4.new_code_cell('print("first")'),
            nbformat.v4.new_code_cell('print("second")'),
        ]
        nb_path = tmp_path / "streaming.ipynb"
        nbformat.write(nb, nb_path)

        writeback_sizes = []

        from jupyter_jcli.notebook_writer import (
            write_outputs_to_notebook as real_writeback,
        )

        def _recording_writeback(path, cell_results):
            writeback_sizes.append(len(cell_results))
            return real_writeback(path, cell_results)

        with patch(
            "jupyter_jcli.commands.exec_cmd.write_outputs_to_notebook",
            side_effect=_recording_writeback,
        ):
            result = runner.invoke(
                main,
                [
                    "-s",
                    live_session["url"],
                    "-t",
                    live_session["token"],
                    "exec",
                    live_session["session_id"],
                    "--file",
                    str(py_file),
                    "--cell",
                    "0:2",
                ],
            )

        assert result.exit_code == 0
        assert writeback_sizes == [1, 1]

        updated_nb = nbformat.read(nb_path, as_version=4)
        assert any("first" in str(o) for o in updated_nb.cells[0].outputs)
        assert any("second" in str(o) for o in updated_nb.cells[1].outputs)

    def test_no_stream_disables_periodic_notebook_writeback(
        self, live_session, mock_kernel_connection, tmp_path, monkeypatch
    ):
        py_file = tmp_path / "no_stream.py"
        py_file.write_text('# %%\nprint("completed-only")\n')
        nb_path = tmp_path / "no_stream.ipynb"
        nbformat.write(
            nbformat.v4.new_notebook(
                cells=[nbformat.v4.new_code_cell('print("completed-only")')]
            ),
            nb_path,
        )
        monkeypatch.setattr("jupyter_jcli.streaming.NOTEBOOK_CHECKPOINT_INTERVAL", 0)
        calls = []
        from jupyter_jcli.notebook_writer import (
            write_outputs_to_notebook as real_writeback,
        )

        def recording_writeback(path, cell_results):
            calls.append(cell_results[0])
            return real_writeback(path, cell_results)

        with patch(
            "jupyter_jcli.commands.exec_cmd.write_outputs_to_notebook",
            side_effect=recording_writeback,
        ):
            result = CliRunner().invoke(
                main,
                [
                    "-s",
                    live_session["url"],
                    "-t",
                    live_session["token"],
                    "exec",
                    live_session["session_id"],
                    "--file",
                    str(py_file),
                    "--no-stream",
                ],
            )

        assert result.exit_code == 0
        assert result.output.count("completed-only") == 1
        assert len(calls) == 1

    def test_streaming_saves_notebook_during_a_running_cell(
        self, live_session, mock_kernel_connection, tmp_path, monkeypatch
    ):
        py_file = tmp_path / "periodic.py"
        py_file.write_text(
            '# %%\nprint("checkpointed", flush=True)\n'
            "import time; time.sleep(0.25); print('finished', flush=True)\n"
        )
        nb_path = tmp_path / "periodic.ipynb"
        nb = nbformat.v4.new_notebook(
            cells=[
                nbformat.v4.new_code_cell(
                    'print("checkpointed", flush=True)\n'
                    "import time; time.sleep(0.25); print('finished', flush=True)"
                )
            ]
        )
        nbformat.write(nb, nb_path)

        monkeypatch.setattr("jupyter_jcli.streaming.NOTEBOOK_CHECKPOINT_INTERVAL", 0.05)
        writes = []
        from jupyter_jcli.notebook_writer import (
            write_outputs_to_notebook as real_writeback,
        )

        def recording_writeback(path, cell_results):
            writes.append(deepcopy(cell_results[0]["raw_outputs"]))
            return real_writeback(path, cell_results)

        with patch(
            "jupyter_jcli.commands.exec_cmd.write_outputs_to_notebook",
            side_effect=recording_writeback,
        ):
            result = CliRunner().invoke(
                main,
                [
                    "-s",
                    live_session["url"],
                    "-t",
                    live_session["token"],
                    "exec",
                    live_session["session_id"],
                    "--file",
                    str(py_file),
                ],
            )

        assert result.exit_code == 0
        assert len(writes) >= 2
        assert any("checkpointed" in str(output) for output in writes[0])
        assert not any("finished" in str(output) for output in writes[0])
        saved = nbformat.read(nb_path, as_version=4)
        assert any("finished" in str(output) for output in saved.cells[0].outputs)

    def test_completed_cells_are_written_before_later_kernel_exception(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        py_file = tmp_path / "partial.py"
        py_file.write_text(
            textwrap.dedent("""\
            # %%
            print("before failure")

            # %%
            raise RuntimeError("client failure")
        """)
        )

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [
            nbformat.v4.new_code_cell('print("before failure")'),
            nbformat.v4.new_code_cell('raise RuntimeError("client failure")'),
        ]
        nb_path = tmp_path / "partial.ipynb"
        nbformat.write(nb, nb_path)

        call_count = 0
        client = mock_kernel_connection._manager.client
        original_execute = client.execute

        def _execute_then_raise(source, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise RuntimeError("simulated kernel transport failure")
            return original_execute(source, **kwargs)

        with patch.object(client, "execute", side_effect=_execute_then_raise):
            result = runner.invoke(
                main,
                [
                    "-s",
                    live_session["url"],
                    "-t",
                    live_session["token"],
                    "exec",
                    live_session["session_id"],
                    "--file",
                    str(py_file),
                    "--cell",
                    "0:2",
                ],
            )

        assert result.exit_code == 1
        assert "simulated kernel transport failure" in result.output

        updated_nb = nbformat.read(nb_path, as_version=4)
        assert any("before failure" in str(o) for o in updated_nb.cells[0].outputs)
        assert updated_nb.cells[1].outputs == []

    def test_missing_notebook_during_writeback_is_error(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        py_file = tmp_path / "missing_target.py"
        py_file.write_text(
            textwrap.dedent("""\
            # %%
            print("cannot persist")
        """)
        )

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [nbformat.v4.new_code_cell('print("cannot persist")')]
        nb_path = tmp_path / "missing_target.ipynb"
        nbformat.write(nb, nb_path)

        def _delete_then_write(path, cell_results):
            nb_path.unlink()
            from jupyter_jcli.notebook_writer import (
                write_outputs_to_notebook as real_writeback,
            )

            return real_writeback(path, cell_results)

        with patch(
            "jupyter_jcli.commands.exec_cmd.write_outputs_to_notebook",
            side_effect=_delete_then_write,
        ):
            result = runner.invoke(
                main,
                [
                    "-s",
                    live_session["url"],
                    "-t",
                    live_session["token"],
                    "exec",
                    live_session["session_id"],
                    "--file",
                    str(py_file),
                    "--cell",
                    "0",
                ],
            )

        assert result.exit_code == 1
        assert "Notebook writeback failed" in result.output

    def test_output_summarizing_failure_happens_after_writeback(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        py_file = tmp_path / "invalid_output.py"
        py_file.write_text('# %%\nprint("invalid output")\n')

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3"}
        nb.cells = [nbformat.v4.new_code_cell('print("invalid output")')]
        nb_path = tmp_path / "invalid_output.ipynb"
        nbformat.write(nb, nb_path)

        with patch(
            "jupyter_jcli.file_execution.summarize_outputs",
            side_effect=ValueError("invalid output"),
        ):
            result = runner.invoke(
                main,
                [
                    "-s",
                    live_session["url"],
                    "-t",
                    live_session["token"],
                    "--json",
                    "exec",
                    live_session["session_id"],
                    "--file",
                    str(py_file),
                ],
            )

        assert result.exit_code == 1
        error = json.loads(result.output)
        assert error["code"] == "OUTPUT_SUMMARY_FAILED"
        assert "execution completed and raw outputs were saved" in error["message"]
        assert f"{nb_path} cell 0" in error["message"]
        assert "invalid output" in error["message"]

        # The real writeback must have persisted the kernel output before the
        # summarizing failure, so the notebook is not left empty.
        updated_nb = nbformat.read(nb_path, as_version=4)
        cell0 = updated_nb.cells[0]
        assert cell0.execution_count is not None
        assert any(
            output.get("output_type") == "stream"
            and "invalid output" in output.get("text", "")
            for output in cell0.outputs
        )

    def test_py_cell_maps_to_reordered_notebook_before_execution(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        from jupyter_jcli.parser import parse_file

        runner = CliRunner()
        py_file = tmp_path / "mapped.py"
        py_file.write_text('# %%\nprint("mapped")\n')
        cell_id = parse_file(str(py_file)).cells[0].node.id

        nb = nbformat.v4.new_notebook()
        nb.cells = [
            nbformat.v4.new_markdown_cell("intro"),
            nbformat.v4.new_code_cell('print("mapped")', id=cell_id),
        ]
        nb_path = tmp_path / "mapped.ipynb"
        nbformat.write(nb, nb_path)

        result = runner.invoke(
            main,
            [
                "-s",
                live_session["url"],
                "-t",
                live_session["token"],
                "--json",
                "exec",
                live_session["session_id"],
                "--file",
                str(py_file),
                "--cell",
                "0",
            ],
        )

        assert result.exit_code == 0
        events = _jsonl_events(result.output)
        cell = next(event["cell"] for event in events if "cell" in event)
        assert cell["cell_index"] == 0
        assert cell["notebook_cell_index"] == 1
        updated_nb = nbformat.read(nb_path, as_version=4)
        assert updated_nb.cells[0].cell_type == "markdown"
        assert any("mapped" in str(output) for output in updated_nb.cells[1].outputs)

    def test_stale_id_source_blocks_execution_but_keeps_historical_outputs(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        py_file = tmp_path / "stale.py"
        py_file.write_text(
            '# %% id="first"\nprint("first")\n\n# %% id="stable"\nprint("new")\n'
        )
        old_output = nbformat.v4.new_output("stream", name="stdout", text="old output")
        nb = nbformat.v4.new_notebook(
            cells=[
                nbformat.v4.new_code_cell('print("first")', id="first"),
                nbformat.v4.new_code_cell('print("old")', id="stable"),
            ]
        )
        nb.cells[1].outputs = [old_output]
        nb_path = tmp_path / "stale.ipynb"
        nbformat.write(nb, nb_path)

        assert read_notebook_output(py_file, 1, 0)["stream"]["data"] == "old output"
        client = mock_kernel_connection._manager.client
        with patch.object(client, "execute", wraps=client.execute) as execute:
            result = CliRunner().invoke(
                main,
                [
                    "-s",
                    live_session["url"],
                    "-t",
                    live_session["token"],
                    "--json",
                    "exec",
                    live_session["session_id"],
                    "--file",
                    str(py_file),
                    "--cell",
                    "0:2",
                ],
            )

        assert result.exit_code == 1
        assert "synchronize the notebook before execution" in result.output
        execute.assert_not_called()
        saved = nbformat.read(nb_path, as_version=4)
        assert saved.cells[0].outputs == []
        assert saved.cells[1].source == 'print("old")'
        assert saved.cells[1].outputs[0].text == "old output"

    @pytest.mark.parametrize("changed_field", ["id", "source"])
    def test_writeback_rejects_changed_target(self, tmp_path, changed_field):
        nb_path = tmp_path / "changed.ipynb"
        cell = nbformat.v4.new_code_cell('print("original")', id="stable")
        cell.outputs = [
            nbformat.v4.new_output("stream", name="stdout", text="saved output")
        ]
        if changed_field == "id":
            cell.id = "replacement"
        else:
            cell.source = 'print("replacement")'
        nbformat.write(nbformat.v4.new_notebook(cells=[cell]), nb_path)

        with pytest.raises(ValueError, match="synchronize before execution"):
            write_outputs_to_notebook(
                str(nb_path),
                [
                    {
                        "cell_index": 0,
                        "expected_cell_id": "stable",
                        "expected_source": 'print("original")',
                        "raw_outputs": [
                            {
                                "output_type": "stream",
                                "name": "stdout",
                                "text": "new output",
                            }
                        ],
                        "execution_count": 1,
                    }
                ],
            )

        saved = nbformat.read(nb_path, as_version=4)
        assert saved.cells[0].outputs[0].text == "saved output"

    def test_no_writeback_for_plain_script(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        """Plain Python scripts (no # %% markers, no front matter) never create a notebook."""
        runner = CliRunner()
        py_file = tmp_path / "standalone.py"
        py_file.write_text('print("no paired notebook")\n')

        result = runner.invoke(
            main,
            [
                "-s",
                live_session["url"],
                "-t",
                live_session["token"],
                "exec",
                live_session["session_id"],
                "--file",
                str(py_file),
            ],
        )
        assert result.exit_code == 0
        assert "no paired notebook" in result.output
        assert "Notebook updated" not in result.output
        assert "Notebook created" not in result.output
        assert not (tmp_path / "standalone.ipynb").exists()


class TestIpynbWriteback:
    """Test that exec --file on .ipynb writes outputs back to itself."""

    def test_ipynb_writeback(self, live_session, mock_kernel_connection, tmp_path):
        runner = CliRunner()
        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [
            nbformat.v4.new_code_cell("print('ipynb writeback')"),
        ]
        nb_path = tmp_path / "direct.ipynb"
        nbformat.write(nb, nb_path)

        result = runner.invoke(
            main,
            [
                "-s",
                live_session["url"],
                "-t",
                live_session["token"],
                "exec",
                live_session["session_id"],
                "--file",
                str(nb_path),
                "--cell",
                "0",
            ],
        )
        assert result.exit_code == 0
        assert "Notebook updated" in result.output

        updated_nb = nbformat.read(nb_path, as_version=4)
        assert len(updated_nb.cells[0].outputs) > 0
        assert any("ipynb writeback" in str(o) for o in updated_nb.cells[0].outputs)

    def test_ipynb_image_writeback(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [
            nbformat.v4.new_code_cell(
                textwrap.dedent("""\
                %matplotlib inline
                import matplotlib.pyplot as plt
                plt.plot([1,2,3])
                plt.show()
            """)
            ),
        ]
        nb_path = tmp_path / "plot.ipynb"
        nbformat.write(nb, nb_path)

        result = runner.invoke(
            main,
            [
                "-s",
                live_session["url"],
                "-t",
                live_session["token"],
                "--json",
                "exec",
                live_session["session_id"],
                "--file",
                str(nb_path),
                "--cell",
                "0",
            ],
        )
        assert result.exit_code == 0
        events = _jsonl_events(result.output)
        cell_event = next(event for event in events if "cell" in event)
        assert cell_event["notebook_updated"] is True

        updated_nb = nbformat.read(nb_path, as_version=4)
        outputs = updated_nb.cells[0].outputs
        has_image = any(
            "image/png" in o.get("data", {})
            for o in outputs
            if o.get("output_type") in ("display_data", "execute_result")
        )
        assert has_image


@pytest.fixture
def synced_newline_pair(tmp_path):
    from jupyter_jcli.diff import InSync, check_drift
    from jupyter_jcli.pairing import synchronize_pair

    py_path = tmp_path / "newline.py"
    nb_path = tmp_path / "newline.ipynb"
    target = nbformat.v4.new_code_cell("x = 1\n", id="target")
    target.outputs = [
        nbformat.v4.new_output("stream", name="stdout", text="old output")
    ]
    nbformat.write(
        nbformat.v4.new_notebook(
            cells=[nbformat.v4.new_markdown_cell("untouched", id="intro"), target]
        ),
        nb_path,
    )
    synchronize_pair(py_path, nb_path, authoritative="ipynb")
    assert isinstance(check_drift(py_path, nb_path), InSync)
    return py_path, nb_path


def _mock_file_kernel(monkeypatch, execute):
    monkeypatch.setattr(
        "jupyter_jcli.kernel.kernel_connection", lambda *a, **kw: nullcontext(object())
    )
    monkeypatch.setattr(
        "jupyter_jcli.kernel.expression_display_mode", lambda *a, **kw: nullcontext()
    )
    monkeypatch.setattr("jupyter_jcli.kernel.execute_with_timeout", execute)
    monkeypatch.setattr("jupyter_jcli.streaming.NOTEBOOK_CHECKPOINT_INTERVAL", 0)


@pytest.mark.parametrize("extra_lf_side", ["notebook", "python"])
def test_synced_newlines_resolve_checkpoint_and_final_writeback(
    synced_newline_pair, monkeypatch, extra_lf_side
):
    from jupyter_jcli.diff import InSync, check_drift
    from jupyter_jcli.file_execution import execute_file
    from jupyter_jcli.formats import percent
    from jupyter_jcli.outputs.notebook import resolve_execution_notebook_cell
    from jupyter_jcli.pairing import synchronize_pair

    py_path, nb_path = synced_newline_pair
    if extra_lf_side == "python":
        notebook = nbformat.read(nb_path, as_version=4)
        notebook.cells[1].source = "x = 1"
        nbformat.write(notebook, nb_path)
        py_path.write_text(py_path.read_text() + "\n\n")
    synchronize_pair(py_path, nb_path)
    assert isinstance(check_drift(py_path, nb_path), InSync)
    resolved = resolve_execution_notebook_cell(py_path, 1)
    code = percent.load(py_path).cells[1].source
    original = nbformat.read(nb_path, as_version=4)
    expected_sources = {
        "notebook": ("x = 1", "x = 1\n"),
        "python": ("x = 1\n", "x = 1"),
    }
    assert (code, original.cells[1].source) == expected_sources[extra_lf_side]
    assert resolved.notebook_cell.source == original.cells[1].source
    writes = []

    def writeback(path, results):
        writes.append(deepcopy(results[0]))
        updated = write_outputs_to_notebook(path, results)
        saved = nbformat.read(path, as_version=4)
        assert saved.cells[0] == original.cells[0]
        assert saved.cells[1].source == original.cells[1].source
        assert saved.cells[1].id == original.cells[1].id
        assert saved.cells[1].outputs[0].text == results[0]["raw_outputs"][0]["text"]
        return updated

    def execute(kernel, source, *, observer, **kwargs):
        assert source == code
        partial = [{"output_type": "stream", "name": "stdout", "text": "partial"}]
        observer({"header": {"msg_type": "stream"}}, partial, {0}, 7)
        assert len(writes) == 1
        return {
            "status": "ok",
            "outputs": [{**partial[0], "text": "finished"}],
            "execution_count": 7,
        }

    _mock_file_kernel(monkeypatch, execute)
    summary = execute_file(
        "unused", None, "mock", str(py_path), "1", "last", None, writeback=writeback
    )
    assert summary.cells_executed == 1
    assert [result["raw_outputs"][0]["text"] for result in writes] == [
        "partial",
        "finished",
    ]
    assert all(
        result["expected_source"] == original.cells[1].source for result in writes
    )
    assert nbformat.read(nb_path, as_version=4).cells[1].execution_count == 7


@pytest.mark.parametrize(
    ("changed_field", "periodic"), [("source", True), ("id", False)]
)
def test_resolved_target_change_rejects_writeback(
    synced_newline_pair, monkeypatch, changed_field, periodic
):
    from jupyter_jcli.file_execution import execute_file

    py_path, nb_path = synced_newline_pair
    mutated_snapshot = None

    def execute(kernel, source, *, observer, **kwargs):
        nonlocal mutated_snapshot
        notebook = nbformat.read(nb_path, as_version=4)
        if changed_field == "source":
            notebook.cells[1].source += "\n"
        else:
            notebook.cells[1].id = "replacement"
        nbformat.write(notebook, nb_path)
        mutated_snapshot = nbformat.read(nb_path, as_version=4)
        outputs = [{"output_type": "stream", "name": "stdout", "text": "new output"}]
        if periodic:
            observer({"header": {"msg_type": "stream"}}, outputs, {0}, 7)
        return {"status": "ok", "outputs": outputs, "execution_count": 7}

    _mock_file_kernel(monkeypatch, execute)
    with pytest.raises(ValueError, match="synchronize before execution"):
        execute_file(
            "unused",
            None,
            "mock",
            str(py_path),
            "1",
            "last",
            None,
            periodic_writeback=periodic,
        )
    saved = nbformat.read(nb_path, as_version=4)
    assert mutated_snapshot is not None
    assert saved == mutated_snapshot


def test_source_difference_rejected_before_kernel(synced_newline_pair, monkeypatch):
    from jupyter_jcli.file_execution import execute_file
    from jupyter_jcli.outputs import OutputProtocolError

    py_path, nb_path = synced_newline_pair
    py_path.write_text(py_path.read_text().replace("x = 1", "x = 2"))

    def unexpected_kernel(*args, **kwargs):
        pytest.fail("Source mismatch must be rejected before opening the kernel")

    monkeypatch.setattr("jupyter_jcli.kernel.kernel_connection", unexpected_kernel)
    with pytest.raises(OutputProtocolError) as caught:
        execute_file("unused", None, "mock", str(py_path), "1", "last", None)
    assert caught.value.code == "NOTEBOOK_OUT_OF_SYNC"
    assert nbformat.read(nb_path, as_version=4).cells[1].outputs[0].text == "old output"


def test_writeback_does_not_mutate_live_display_id(tmp_path):
    nb_path = tmp_path / "live_display.ipynb"
    cell = nbformat.v4.new_code_cell("display(value)", id="stable")
    nbformat.write(nbformat.v4.new_notebook(cells=[cell]), nb_path)
    output = {
        "output_type": "display_data",
        "data": {"text/plain": "before update"},
        "metadata": {},
        "transient": {"display_id": "display-1"},
    }

    write_outputs_to_notebook(
        str(nb_path),
        [
            {
                "cell_index": 0,
                "expected_cell_id": "stable",
                "expected_source": "display(value)",
                "raw_outputs": [output],
                "execution_count": 3,
            }
        ],
    )

    saved = nbformat.read(nb_path, as_version=4)
    assert output["transient"] == {"display_id": "display-1"}
    assert "transient" not in saved.cells[0].outputs[0]
