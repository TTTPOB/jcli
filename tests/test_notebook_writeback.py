"""Test notebook output writeback — the core differentiating feature."""

import json
import textwrap

import nbformat
from click.testing import CliRunner

from j_cli.cli import main


def _create_session(runner, url, token):
    result = runner.invoke(main, [
        "-s", url, "-t", token,
        "--json", "session", "create", "--kernel", "python3",
    ])
    return json.loads(result.output)


def _kill_session(runner, url, token, sid):
    runner.invoke(main, ["-s", url, "-t", token, "session", "kill", sid])


class TestPyPercentWriteback:
    """Test that exec --file on .py writes outputs to paired .ipynb."""

    def test_writeback_creates_output_in_ipynb(self, jupyter_server, tmp_path):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        try:
            # Create py:percent file
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

            # Create paired ipynb with empty cells
            nb = nbformat.v4.new_notebook()
            nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
            nb.cells = [
                nbformat.v4.new_code_cell('print("hello writeback")'),
                nbformat.v4.new_code_cell("40 + 2"),
            ]
            nb_path = tmp_path / "analysis.ipynb"
            nbformat.write(nb, nb_path)

            # Execute cell 0
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "exec", info["session_id"], "--file", str(py_file), "--cell", "0",
            ])
            assert result.exit_code == 0
            assert "hello writeback" in result.output
            assert "Notebook updated" in result.output

            # Verify ipynb has output
            updated_nb = nbformat.read(nb_path, as_version=4)
            cell0 = updated_nb.cells[0]
            assert len(cell0.outputs) > 0
            assert any("hello writeback" in str(o) for o in cell0.outputs)

        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])

    def test_writeback_multiple_cells(self, jupyter_server, tmp_path):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        try:
            py_file = tmp_path / "multi.py"
            py_file.write_text(textwrap.dedent("""\
                # ---
                # jupyter:
                #   kernelspec:
                #     name: python3
                # ---

                # %%
                x = 10

                # %%
                print(x * 2)

                # %%
                print(x * 3)
            """))

            nb = nbformat.v4.new_notebook()
            nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
            nb.cells = [
                nbformat.v4.new_code_cell("x = 10"),
                nbformat.v4.new_code_cell("print(x * 2)"),
                nbformat.v4.new_code_cell("print(x * 3)"),
            ]
            nb_path = tmp_path / "multi.ipynb"
            nbformat.write(nb, nb_path)

            # Execute all cells
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "--json", "exec", info["session_id"],
                "--file", str(py_file), "--cell", "0:3",
            ])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["notebook_updated"] == str(nb_path)

            # Verify outputs in ipynb
            updated_nb = nbformat.read(nb_path, as_version=4)
            assert any("20" in str(o) for o in updated_nb.cells[1].outputs)
            assert any("30" in str(o) for o in updated_nb.cells[2].outputs)

        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])

    def test_no_writeback_without_paired_ipynb(self, jupyter_server, tmp_path):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        try:
            py_file = tmp_path / "standalone.py"
            py_file.write_text(textwrap.dedent("""\
                # ---
                # jupyter:
                #   kernelspec:
                #     name: python3
                # ---

                # %%
                print("no paired notebook")
            """))

            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "exec", info["session_id"], "--file", str(py_file), "--cell", "0",
            ])
            assert result.exit_code == 0
            assert "no paired notebook" in result.output
            assert "Notebook updated" not in result.output

        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])


class TestIpynbWriteback:
    """Test that exec --file on .ipynb writes outputs back to itself."""

    def test_ipynb_writeback(self, jupyter_server, tmp_path):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        try:
            nb = nbformat.v4.new_notebook()
            nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
            nb.cells = [
                nbformat.v4.new_code_cell("print('ipynb writeback')"),
            ]
            nb_path = tmp_path / "direct.ipynb"
            nbformat.write(nb, nb_path)

            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "exec", info["session_id"], "--file", str(nb_path), "--cell", "0",
            ])
            assert result.exit_code == 0
            assert "Notebook updated" in result.output

            updated_nb = nbformat.read(nb_path, as_version=4)
            assert len(updated_nb.cells[0].outputs) > 0
            assert any("ipynb writeback" in str(o) for o in updated_nb.cells[0].outputs)

        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])

    def test_ipynb_image_writeback(self, jupyter_server, tmp_path):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        try:
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
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "--json", "exec", info["session_id"],
                "--file", str(nb_path), "--cell", "0",
            ])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["notebook_updated"] == str(nb_path)

            # Verify image output in ipynb
            updated_nb = nbformat.read(nb_path, as_version=4)
            outputs = updated_nb.cells[0].outputs
            has_image = any(
                "image/png" in o.get("data", {})
                for o in outputs
                if o.get("output_type") in ("display_data", "execute_result")
            )
            assert has_image

        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])
