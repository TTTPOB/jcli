"""Test exec command — inline code and file-based execution."""

import json
import os
import textwrap

from click.testing import CliRunner

from jupyter_jcli.cli import main


class TestExecCode:
    """Test inline --code execution.

    Uses mock_execute_code so the CLI path reuses the persistent WebSocket
    instead of opening a new connection for every test.
    """

    def test_print(self, live_session, mock_execute_code):
        runner = CliRunner()
        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "exec", live_session["session_id"], "--code", "print('hello jcli')",
        ])
        assert result.exit_code == 0
        assert "hello jcli" in result.output

    def test_expression(self, live_session, mock_execute_code):
        runner = CliRunner()
        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "exec", live_session["session_id"], "--code", "2 + 3",
        ])
        assert result.exit_code == 0
        assert "5" in result.output

    def test_error_output(self, live_session, mock_execute_code):
        runner = CliRunner()
        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "exec", live_session["session_id"], "--code", "1/0",
        ])
        assert result.exit_code == 0
        assert "ZeroDivisionError" in result.output

    def test_json_output(self, live_session, mock_execute_code):
        runner = CliRunner()
        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "--json", "exec", live_session["session_id"], "--code", "print('hi')",
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert any(o["type"] == "stream" and "hi" in o["text"] for o in data["outputs"])

    def test_image_output(self, live_session, mock_execute_code):
        runner = CliRunner()
        code = textwrap.dedent("""\
            %matplotlib inline
            import matplotlib.pyplot as plt
            plt.plot([1,2],[3,4])
            plt.show()
        """)
        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "--json", "exec", live_session["session_id"], "--code", code,
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        images = [o for o in data["outputs"] if o["type"] == "image"]
        assert len(images) >= 1
        assert os.path.isfile(images[0]["path"])
        assert images[0]["path"].endswith(".png")


class TestExecFile:
    """Test file-based --file execution.

    Uses mock_kernel_connection so the CLI path reuses the persistent WebSocket.
    """

    def test_py_percent_single_cell(self, live_session, mock_kernel_connection, tmp_path):
        runner = CliRunner()
        script = tmp_path / "test.py"
        script.write_text(textwrap.dedent("""\
            # ---
            # jupyter:
            #   kernelspec:
            #     name: python3
            # ---

            # %%
            print("cell zero")

            # %%
            x = 42
            x
        """))

        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "exec", live_session["session_id"], "--file", str(script), "--cell", "0",
        ])
        assert result.exit_code == 0
        assert "cell zero" in result.output

    def test_py_percent_cell_range(self, live_session, mock_kernel_connection, tmp_path):
        runner = CliRunner()
        script = tmp_path / "test_range.py"
        script.write_text(textwrap.dedent("""\
            # ---
            # jupyter:
            #   kernelspec:
            #     name: python3
            # ---

            # %%
            _range_a = 10

            # %%
            print(_range_a + 1)

            # %%
            print(_range_a + 2)
        """))

        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "--json", "exec", live_session["session_id"],
            "--file", str(script), "--cell", "0:3",
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["cells"]) == 3
        # cell 1 prints 11
        cell1_out = data["cells"][1]["outputs"]
        assert any("11" in o.get("text", "") for o in cell1_out)

    def test_ipynb_execution(self, live_session, mock_kernel_connection, tmp_path):
        runner = CliRunner()
        import nbformat
        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [
            nbformat.v4.new_code_cell("print('from ipynb')"),
            nbformat.v4.new_code_cell("7 * 6"),
        ]
        nb_path = tmp_path / "test.ipynb"
        nbformat.write(nb, nb_path)

        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "exec", live_session["session_id"], "--file", str(nb_path), "--cell", "0",
        ])
        assert result.exit_code == 0
        assert "from ipynb" in result.output
