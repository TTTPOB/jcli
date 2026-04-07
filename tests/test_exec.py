"""Test exec command — inline code and file-based execution."""

import json
import os
import textwrap

from click.testing import CliRunner

from jcli.cli import main


def _create_session(runner, url, token):
    result = runner.invoke(main, [
        "-s", url, "-t", token,
        "--json", "session", "create", "--kernel", "python3",
    ])
    return json.loads(result.output)


def _kill_session(runner, url, token, sid):
    runner.invoke(main, ["-s", url, "-t", token, "session", "kill", sid])


class TestExecCode:
    """Test inline --code execution."""

    def test_print(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        try:
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "exec", info["session_id"], "--code", "print('hello jcli')",
            ])
            assert result.exit_code == 0
            assert "hello jcli" in result.output
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])

    def test_expression(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        try:
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "exec", info["session_id"], "--code", "2 + 3",
            ])
            assert result.exit_code == 0
            assert "5" in result.output
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])

    def test_error_output(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        try:
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "exec", info["session_id"], "--code", "1/0",
            ])
            assert result.exit_code == 0
            assert "ZeroDivisionError" in result.output
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])

    def test_json_output(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        try:
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "--json", "exec", info["session_id"], "--code", "print('hi')",
            ])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["status"] == "ok"
            assert any(o["type"] == "stream" and "hi" in o["text"] for o in data["outputs"])
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])

    def test_image_output(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        try:
            code = textwrap.dedent("""\
                %matplotlib inline
                import matplotlib.pyplot as plt
                plt.plot([1,2],[3,4])
                plt.show()
            """)
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "--json", "exec", info["session_id"], "--code", code,
            ])
            assert result.exit_code == 0
            data = json.loads(result.output)
            images = [o for o in data["outputs"] if o["type"] == "image"]
            assert len(images) >= 1
            assert os.path.isfile(images[0]["path"])
            assert images[0]["path"].endswith(".png")
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])


class TestExecFile:
    """Test file-based --file execution."""

    def test_py_percent_single_cell(self, jupyter_server, tmp_path):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        try:
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
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "exec", info["session_id"], "--file", str(script), "--cell", "0",
            ])
            assert result.exit_code == 0
            assert "cell zero" in result.output
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])

    def test_py_percent_cell_range(self, jupyter_server, tmp_path):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        try:
            script = tmp_path / "test_range.py"
            script.write_text(textwrap.dedent("""\
                # ---
                # jupyter:
                #   kernelspec:
                #     name: python3
                # ---

                # %%
                a = 10

                # %%
                print(a + 1)

                # %%
                print(a + 2)
            """))

            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "--json", "exec", info["session_id"],
                "--file", str(script), "--cell", "0:3",
            ])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert len(data["cells"]) == 3
            # cell 1 prints 11
            cell1_out = data["cells"][1]["outputs"]
            assert any("11" in o.get("text", "") for o in cell1_out)
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])

    def test_ipynb_execution(self, jupyter_server, tmp_path):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        try:
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
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "exec", info["session_id"], "--file", str(nb_path), "--cell", "0",
            ])
            assert result.exit_code == 0
            assert "from ipynb" in result.output
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])
