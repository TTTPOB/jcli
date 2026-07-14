"""Test notebook output writeback — the core differentiating feature."""

import json
import textwrap
from unittest.mock import patch

import nbformat
from click.testing import CliRunner

from jupyter_jcli.cli import main


def _jsonl_events(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


class TestPyPercentWriteback:
    """Test that exec --file on .py writes outputs to paired .ipynb."""

    def test_writeback_creates_output_in_ipynb(self, live_session, mock_kernel_connection, tmp_path):
        runner = CliRunner()
        py_file = tmp_path / "analysis.py"
        py_file.write_text(textwrap.dedent("""\
            # ---
            # jupyter:
            #   kernelspec:
            #     name: python3
            # ---

            # %%
            print("hello writeback")

            # %%
            40 + 2
        """))

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [
            nbformat.v4.new_code_cell('print("hello writeback")'),
            nbformat.v4.new_code_cell("40 + 2"),
        ]
        nb_path = tmp_path / "analysis.ipynb"
        nbformat.write(nb, nb_path)

        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "exec", live_session["session_id"], "--file", str(py_file), "--cell", "0",
        ])
        assert result.exit_code == 0
        assert "hello writeback" in result.output
        assert "Notebook updated" in result.output

        updated_nb = nbformat.read(nb_path, as_version=4)
        cell0 = updated_nb.cells[0]
        assert len(cell0.outputs) > 0
        assert any("hello writeback" in str(o) for o in cell0.outputs)

    def test_writeback_multiple_cells(self, live_session, mock_kernel_connection, tmp_path):
        runner = CliRunner()
        py_file = tmp_path / "multi.py"
        py_file.write_text(textwrap.dedent("""\
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
        """))

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [
            nbformat.v4.new_code_cell("_wb_x = 10"),
            nbformat.v4.new_code_cell("print(_wb_x * 2)"),
            nbformat.v4.new_code_cell("print(_wb_x * 3)"),
        ]
        nb_path = tmp_path / "multi.ipynb"
        nbformat.write(nb, nb_path)

        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "--json", "exec", live_session["session_id"],
            "--file", str(py_file), "--cell", "0:3",
        ])
        assert result.exit_code == 0
        events = _jsonl_events(result.output)
        cell_events = [event for event in events if "cell" in event]
        summary = next(event["summary"] for event in events if "summary" in event)
        assert len(cell_events) == 3
        assert all(event["notebook_updated"] == str(nb_path) for event in cell_events)
        assert summary["notebook_updated"] == str(nb_path)

        updated_nb = nbformat.read(nb_path, as_version=4)
        assert any("20" in str(o) for o in updated_nb.cells[1].outputs)
        assert any("30" in str(o) for o in updated_nb.cells[2].outputs)

    def test_writeback_happens_after_each_completed_cell(self, live_session, mock_kernel_connection, tmp_path):
        runner = CliRunner()
        py_file = tmp_path / "streaming.py"
        py_file.write_text(textwrap.dedent("""\
            # %%
            print("first")

            # %%
            print("second")
        """))

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [
            nbformat.v4.new_code_cell('print("first")'),
            nbformat.v4.new_code_cell('print("second")'),
        ]
        nb_path = tmp_path / "streaming.ipynb"
        nbformat.write(nb, nb_path)

        writeback_sizes = []

        from jupyter_jcli.notebook_writer import write_outputs_to_notebook as real_writeback

        def _recording_writeback(path, cell_results):
            writeback_sizes.append(len(cell_results))
            return real_writeback(path, cell_results)

        with patch("jupyter_jcli.commands.exec_cmd.write_outputs_to_notebook", side_effect=_recording_writeback):
            result = runner.invoke(main, [
                "-s", live_session["url"], "-t", live_session["token"],
                "exec", live_session["session_id"], "--file", str(py_file), "--cell", "0:2",
            ])

        assert result.exit_code == 0
        assert writeback_sizes == [1, 1]

        updated_nb = nbformat.read(nb_path, as_version=4)
        assert any("first" in str(o) for o in updated_nb.cells[0].outputs)
        assert any("second" in str(o) for o in updated_nb.cells[1].outputs)

    def test_completed_cells_are_written_before_later_kernel_exception(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        py_file = tmp_path / "partial.py"
        py_file.write_text(textwrap.dedent("""\
            # %%
            print("before failure")

            # %%
            raise RuntimeError("client failure")
        """))

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [
            nbformat.v4.new_code_cell('print("before failure")'),
            nbformat.v4.new_code_cell('raise RuntimeError("client failure")'),
        ]
        nb_path = tmp_path / "partial.ipynb"
        nbformat.write(nb, nb_path)

        call_count = 0
        original_execute = mock_kernel_connection.execute

        def _execute_then_raise(source, timeout=10, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise RuntimeError("simulated kernel transport failure")
            return original_execute(source, timeout=timeout, **kwargs)

        with patch.object(mock_kernel_connection, "execute", side_effect=_execute_then_raise):
            result = runner.invoke(main, [
                "-s", live_session["url"], "-t", live_session["token"],
                "exec", live_session["session_id"], "--file", str(py_file), "--cell", "0:2",
            ])

        assert result.exit_code == 1
        assert "simulated kernel transport failure" in result.output

        updated_nb = nbformat.read(nb_path, as_version=4)
        assert any("before failure" in str(o) for o in updated_nb.cells[0].outputs)
        assert updated_nb.cells[1].outputs == []

    def test_missing_notebook_during_writeback_is_error(self, live_session, mock_kernel_connection, tmp_path):
        runner = CliRunner()
        py_file = tmp_path / "missing_target.py"
        py_file.write_text(textwrap.dedent("""\
            # %%
            print("cannot persist")
        """))

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [nbformat.v4.new_code_cell('print("cannot persist")')]
        nb_path = tmp_path / "missing_target.ipynb"
        nbformat.write(nb, nb_path)

        def _delete_then_write(path, cell_results):
            nb_path.unlink()
            from jupyter_jcli.notebook_writer import write_outputs_to_notebook as real_writeback

            return real_writeback(path, cell_results)

        with patch("jupyter_jcli.commands.exec_cmd.write_outputs_to_notebook", side_effect=_delete_then_write):
            result = runner.invoke(main, [
                "-s", live_session["url"], "-t", live_session["token"],
                "exec", live_session["session_id"], "--file", str(py_file), "--cell", "0",
            ])

        assert result.exit_code == 1
        assert "Notebook writeback failed" in result.output

    def test_no_writeback_for_plain_script(self, live_session, mock_kernel_connection, tmp_path):
        """Plain Python scripts (no # %% markers, no front matter) never create a notebook."""
        runner = CliRunner()
        py_file = tmp_path / "standalone.py"
        py_file.write_text('print("no paired notebook")\n')

        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "exec", live_session["session_id"], "--file", str(py_file),
        ])
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

        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "exec", live_session["session_id"], "--file", str(nb_path), "--cell", "0",
        ])
        assert result.exit_code == 0
        assert "Notebook updated" in result.output

        updated_nb = nbformat.read(nb_path, as_version=4)
        assert len(updated_nb.cells[0].outputs) > 0
        assert any("ipynb writeback" in str(o) for o in updated_nb.cells[0].outputs)

    def test_ipynb_image_writeback(self, live_session, mock_kernel_connection, tmp_path):
        runner = CliRunner()
        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [
            nbformat.v4.new_code_cell(textwrap.dedent("""\
                %matplotlib inline
                import matplotlib.pyplot as plt
                plt.plot([1,2,3])
                plt.show()
            """)),
        ]
        nb_path = tmp_path / "plot.ipynb"
        nbformat.write(nb, nb_path)

        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "--json", "exec", live_session["session_id"],
            "--file", str(nb_path), "--cell", "0",
        ])
        assert result.exit_code == 0
        events = _jsonl_events(result.output)
        cell_event = next(event for event in events if "cell" in event)
        assert cell_event["notebook_updated"] == str(nb_path)

        updated_nb = nbformat.read(nb_path, as_version=4)
        outputs = updated_nb.cells[0].outputs
        has_image = any(
            "image/png" in o.get("data", {})
            for o in outputs
            if o.get("output_type") in ("display_data", "execute_result")
        )
        assert has_image
