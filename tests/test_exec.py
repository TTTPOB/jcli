"""Test exec command — inline code and file-based execution."""

import json
import os
import textwrap

from click.testing import CliRunner

from jupyter_jcli.cli import main


def _jsonl_events(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


class TestExecCode:
    """Test inline --code execution.

    Uses mock_execute_code so the CLI path reuses the persistent WebSocket
    instead of opening a new connection for every test.
    """

    def test_print(self, live_session, mock_execute_code):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-s",
                live_session["url"],
                "-t",
                live_session["token"],
                "exec",
                live_session["session_id"],
                "--code",
                "print('hello jcli')",
            ],
        )
        assert result.exit_code == 0
        assert "hello jcli" in result.output

    def test_expression(self, live_session, mock_execute_code):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-s",
                live_session["url"],
                "-t",
                live_session["token"],
                "exec",
                live_session["session_id"],
                "--code",
                "2 + 3",
            ],
        )
        assert result.exit_code == 0
        assert "5" in result.output

    def test_display_mode_applies_to_inline_code(self, live_session, mock_execute_code):
        runner = CliRunner()
        default_result = runner.invoke(
            main,
            [
                "-s",
                live_session["url"],
                "-t",
                live_session["token"],
                "--json",
                "exec",
                live_session["session_id"],
                "--code",
                '"first"\n"second"',
            ],
        )
        all_result = runner.invoke(
            main,
            [
                "-s",
                live_session["url"],
                "-t",
                live_session["token"],
                "--json",
                "exec",
                live_session["session_id"],
                "--code",
                '"first"\n"second"',
                "--display-mode",
                "all",
            ],
        )

        assert default_result.exit_code == 0
        assert all_result.exit_code == 0
        default_outputs = json.loads(default_result.output)["outputs"]
        all_outputs = json.loads(all_result.output)["outputs"]
        assert [output["text"] for output in default_outputs] == ["'second'"]
        assert [output["text"] for output in all_outputs] == ["'first'", "'second'"]

    def test_error_output(self, live_session, mock_execute_code):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-s",
                live_session["url"],
                "-t",
                live_session["token"],
                "exec",
                live_session["session_id"],
                "--code",
                "1/0",
            ],
        )
        assert result.exit_code == 0
        assert "ZeroDivisionError" in result.output

    def test_json_output(self, live_session, mock_execute_code):
        runner = CliRunner()
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
                "--code",
                "print('hi')",
            ],
        )
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
                "--code",
                code,
            ],
        )
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

    def test_py_percent_single_cell(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        script = tmp_path / "test.py"
        script.write_text(
            textwrap.dedent("""\
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
        """)
        )

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
                str(script),
                "--cell",
                "0",
            ],
        )
        assert result.exit_code == 0
        assert "cell zero" in result.output

    def test_py_percent_cell_range(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        script = tmp_path / "test_range.py"
        script.write_text(
            textwrap.dedent("""\
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
        """)
        )

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
                str(script),
                "--cell",
                "0:3",
            ],
        )
        assert result.exit_code == 0
        events = _jsonl_events(result.output)
        cell_events = [event for event in events if "cell" in event]
        assert len(cell_events) == 3
        # cell 1 prints 11
        cell1_out = cell_events[1]["cell"]["outputs"]
        assert any("11" in o.get("text", "") for o in cell1_out)

    def test_py_percent_displays_every_rich_expression(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        script = tmp_path / "tables.py"
        script.write_text(
            textwrap.dedent("""\
            # %%
            class Table:
                def __init__(self, value):
                    self.value = value

                def _repr_html_(self):
                    return f"<table><tr><td>{self.value}</td></tr></table>"

            Table("first")
            Table("second")
        """)
        )

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
                str(script),
                "--display-mode",
                "all",
            ],
        )

        assert result.exit_code == 0
        events = _jsonl_events(result.output)
        html_outputs = [
            output["html"]
            for output in events[0]["cell"]["outputs"]
            if output["type"] == "html"
        ]
        assert len(html_outputs) == 2
        assert "first" in html_outputs[0]
        assert "second" in html_outputs[1]

    def test_plain_script_defaults_to_last_expression(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        script = tmp_path / "plain.py"
        script.write_text('"plain first"\n"plain second"\n')

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
                str(script),
            ],
        )

        assert result.exit_code == 0
        events = _jsonl_events(result.output)
        texts = [
            output["text"]
            for output in events[0]["cell"]["outputs"]
            if output["type"] == "execute_result"
        ]
        assert texts == ["'plain second'"]

    def test_file_execution_restores_interactivity(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        before = mock_kernel_connection.execute(
            "get_ipython().ast_node_interactivity", timeout=10
        )["outputs"][-1]["data"]["text/plain"]
        script = tmp_path / "restore.py"
        script.write_text('"run"\n')

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
                str(script),
            ],
        )

        assert result.exit_code == 0
        after = mock_kernel_connection.execute(
            "get_ipython().ast_node_interactivity", timeout=10
        )["outputs"][-1]["data"]["text/plain"]
        assert after == before

    def test_file_json_output_is_jsonl_per_cell(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        script = tmp_path / "jsonl.py"
        script.write_text(
            textwrap.dedent("""\
            # %%
            print("alpha")

            # %%
            print("beta")
        """)
        )

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
                str(script),
                "--cell",
                "0:2",
            ],
        )

        assert result.exit_code == 0
        lines = _jsonl_events(result.output)
        assert len(lines) == 3
        assert lines[0]["status"] == "ok"
        assert lines[0]["cell"]["cell_index"] == 0
        assert any("alpha" in o.get("text", "") for o in lines[0]["cell"]["outputs"])
        assert lines[0]["notebook_created"] == str(tmp_path / "jsonl.ipynb")
        assert lines[0]["notebook_updated"] == str(tmp_path / "jsonl.ipynb")
        assert lines[1]["cell"]["cell_index"] == 1
        assert any("beta" in o.get("text", "") for o in lines[1]["cell"]["outputs"])
        assert lines[1]["notebook_updated"] == str(tmp_path / "jsonl.ipynb")
        assert lines[2] == {
            "status": "ok",
            "summary": {
                "cells_executed": 2,
                "notebook_updated": str(tmp_path / "jsonl.ipynb"),
            },
        }

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
        assert "from ipynb" in result.output

    def test_ipynb_displays_every_expression(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        runner = CliRunner()
        import nbformat

        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
        nb.cells = [nbformat.v4.new_code_cell('"ipynb first"\n"ipynb second"')]
        nb_path = tmp_path / "display_all.ipynb"
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
                "--display-mode",
                "all",
            ],
        )

        assert result.exit_code == 0
        updated = nbformat.read(nb_path, as_version=4)
        texts = [output["data"]["text/plain"] for output in updated.cells[0].outputs]
        assert texts == ["'ipynb first'", "'ipynb second'"]


class TestExecAutoCreatesIpynb:
    """Test that exec auto-creates a paired .ipynb for py:percent files.

    Uses mock_kernel_connection so the CLI path reuses the persistent WebSocket.
    """

    def test_percent_marker_creates_ipynb(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        """py:percent file with # %% marker auto-creates .ipynb with outputs."""
        import nbformat

        runner = CliRunner()
        script = tmp_path / "new.py"
        script.write_text(
            textwrap.dedent("""\
            # %%
            print("auto created")

            # %%
            2 + 2
        """)
        )
        expected_nb = tmp_path / "new.ipynb"
        assert not expected_nb.exists()

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
                str(script),
            ],
        )
        assert result.exit_code == 0
        assert "auto created" in result.output
        assert expected_nb.exists(), "paired .ipynb should have been created"
        assert "Notebook created" in result.output

        nb = nbformat.read(str(expected_nb), as_version=4)
        assert len(nb.cells) == 2
        # First cell should have stream output with "auto created"
        outputs = nb.cells[0].outputs
        assert any("auto created" in o.get("text", "") for o in outputs)

    def test_front_matter_only_creates_ipynb(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        """py:percent file with only front matter (no # %%) also auto-creates .ipynb."""
        runner = CliRunner()
        script = tmp_path / "fm_only.py"
        script.write_text(
            textwrap.dedent("""\
            # ---
            # jupyter:
            #   kernelspec:
            #     name: python3
            # ---
            print("front matter only")
        """)
        )
        expected_nb = tmp_path / "fm_only.ipynb"
        assert not expected_nb.exists()

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
                str(script),
            ],
        )
        assert result.exit_code == 0
        assert expected_nb.exists(), "paired .ipynb should have been created"

    def test_plain_script_no_ipynb(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        """Plain .py with no markers and no front matter does NOT create .ipynb."""
        runner = CliRunner()
        script = tmp_path / "plain.py"
        script.write_text('print("plain script")\n')

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
                str(script),
            ],
        )
        assert result.exit_code == 0
        assert "plain script" in result.output
        assert not (tmp_path / "plain.ipynb").exists(), (
            "plain script must NOT create .ipynb"
        )

    def test_dummy_py_targets_correct_ipynb(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        """foo.dummy.py should create foo.ipynb, not foo.dummy.ipynb."""
        runner = CliRunner()
        script = tmp_path / "analysis.dummy.py"
        script.write_text(
            textwrap.dedent("""\
            # %%
            print("dummy pair")
        """)
        )

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
                str(script),
            ],
        )
        assert result.exit_code == 0
        assert (tmp_path / "analysis.ipynb").exists(), "should create analysis.ipynb"
        assert not (tmp_path / "analysis.dummy.ipynb").exists()

    def test_existing_ipynb_not_replaced(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        """If foo.ipynb already exists, exec uses the existing path (no auto-create)."""
        import nbformat

        runner = CliRunner()
        script = tmp_path / "existing.py"
        script.write_text(
            textwrap.dedent("""\
            # %%
            print("existing pair")
        """)
        )
        # Pre-create the paired notebook
        nb = nbformat.v4.new_notebook()
        nb.cells = [nbformat.v4.new_code_cell("print('existing pair')")]
        nb_path = tmp_path / "existing.ipynb"
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
                str(script),
            ],
        )
        assert result.exit_code == 0
        events = _jsonl_events(result.output)
        cell_event = next(event for event in events if "cell" in event)
        # Should update, not create
        assert "notebook_created" not in cell_event
        assert cell_event.get("notebook_updated") == str(nb_path)

    def test_json_output_includes_notebook_created(
        self, live_session, mock_kernel_connection, tmp_path
    ):
        """--json output includes notebook_created field when auto-creation triggers."""
        runner = CliRunner()
        script = tmp_path / "json_test.py"
        script.write_text(
            textwrap.dedent("""\
            # %%
            x = 99
        """)
        )

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
                str(script),
            ],
        )
        assert result.exit_code == 0
        events = _jsonl_events(result.output)
        cell_event = next(event for event in events if "cell" in event)
        assert cell_event["status"] == "ok"
        assert "notebook_created" in cell_event
        assert cell_event["notebook_created"].endswith("json_test.ipynb")
