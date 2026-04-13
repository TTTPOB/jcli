"""Tests for jupyter_jcli.hooks_parser — AST-based shell command parser."""

import pytest

from jupyter_jcli.hooks_parser import (
    SimpleCommand,
    extract_script_target,
    iter_simple_commands,
    unwrap_runner,
)


# ---------------------------------------------------------------------------
# iter_simple_commands — basic parsing
# ---------------------------------------------------------------------------

class TestIterSimpleCommands:
    def test_single_command(self):
        cmds = iter_simple_commands("python foo.py")
        assert len(cmds) == 1
        sc = cmds[0]
        assert sc.name == "python"
        assert sc.args == ("foo.py",)
        assert sc.assigns == {}

    def test_flags_and_args(self):
        cmds = iter_simple_commands("python -u foo.py")
        assert len(cmds) == 1
        assert cmds[0].name == "python"
        assert cmds[0].args == ("-u", "foo.py")

    def test_env_var_prefix(self):
        """FOO=bar python foo.py → assigns captured, name is python."""
        cmds = iter_simple_commands("FOO=bar python foo.py")
        assert len(cmds) == 1
        sc = cmds[0]
        assert sc.name == "python"
        assert sc.assigns == {"FOO": "bar"}
        assert sc.args == ("foo.py",)

    def test_multiple_env_vars(self):
        cmds = iter_simple_commands("A=1 B=2 python foo.py")
        assert len(cmds) == 1
        sc = cmds[0]
        assert sc.name == "python"
        assert sc.assigns == {"A": "1", "B": "2"}

    def test_pipeline_two_commands(self):
        cmds = iter_simple_commands("cat x.py | python process.py")
        assert len(cmds) == 2
        assert cmds[0].name == "cat"
        assert cmds[1].name == "python"

    def test_semicolon_two_commands(self):
        cmds = iter_simple_commands("cd /tmp; python foo.py")
        assert len(cmds) == 2
        assert cmds[0].name == "cd"
        assert cmds[1].name == "python"

    def test_and_operator_two_commands(self):
        cmds = iter_simple_commands("cd /tmp && python foo.py")
        assert len(cmds) == 2
        assert cmds[0].name == "cd"
        assert cmds[1].name == "python"

    def test_empty_string(self):
        assert iter_simple_commands("") == []

    def test_shebang_command(self):
        cmds = iter_simple_commands("./foo.py")
        assert len(cmds) == 1
        assert cmds[0].name == "./foo.py"
        assert cmds[0].args == ()

    def test_double_quoted_arg_not_a_command(self):
        """echo \"python foo.py\" → 1 command (echo); the string is only an arg."""
        cmds = iter_simple_commands('echo "python foo.py"')
        assert len(cmds) == 1
        assert cmds[0].name == "echo"
        # The quoted string appears as an argument, not a new command
        assert "python foo.py" in cmds[0].args

    def test_single_quoted_arg_not_a_command(self):
        """echo 'ls /etc' → 1 command; inner single-quoted content ignored."""
        cmds = iter_simple_commands("echo 'ls /etc'")
        assert len(cmds) == 1
        assert cmds[0].name == "echo"

    def test_quote_stripped_from_double_quoted_arg(self):
        """Double-quoted string args should have outer quotes stripped."""
        cmds = iter_simple_commands('echo "hello world"')
        assert cmds[0].args == ("hello world",)

    def test_quote_stripped_from_single_quoted_arg(self):
        cmds = iter_simple_commands("echo 'hello world'")
        assert cmds[0].args == ("hello world",)

    def test_false_positive_prevention_semicolon(self):
        """Two separate commands: second command's flags must not bleed into first."""
        # Pattern: ls x.txt; echo --flag
        # The --flag is in a separate command and must not affect the first.
        cmds = iter_simple_commands("ls x.txt; echo --flag")
        assert len(cmds) == 2
        assert cmds[0].name == "ls"
        assert "--flag" not in cmds[0].args
        assert cmds[1].name == "echo"
        assert "--flag" in cmds[1].args


# ---------------------------------------------------------------------------
# unwrap_runner
# ---------------------------------------------------------------------------

class TestUnwrapRunner:
    def _sc(self, name: str, *args: str, assigns=None) -> SimpleCommand:
        return SimpleCommand(
            name=name,
            args=tuple(args),
            assigns=assigns or {},
            raw=" ".join([name] + list(args)),
        )

    def test_non_wrapper_passthrough(self):
        sc = self._sc("python", "foo.py")
        assert unwrap_runner(sc) is sc

    # --- uv ---

    def test_uv_run_python(self):
        cmds = iter_simple_commands("uv run python foo.py")
        inner = unwrap_runner(cmds[0])
        assert inner.name == "python"
        assert inner.args == ("foo.py",)

    def test_uv_run_with_python_flag(self):
        """uv run -p 3.12 python foo.py → python foo.py"""
        cmds = iter_simple_commands("uv run -p 3.12 python foo.py")
        inner = unwrap_runner(cmds[0])
        assert inner.name == "python"
        assert inner.args == ("foo.py",)

    def test_uv_without_run_unchanged(self):
        """uv sync should not be peeled."""
        sc = self._sc("uv", "sync")
        assert unwrap_runner(sc) is sc

    # --- pixi ---

    def test_pixi_run_python(self):
        cmds = iter_simple_commands("pixi run python foo.py")
        inner = unwrap_runner(cmds[0])
        assert inner.name == "python"

    def test_pixi_run_with_env_flag(self):
        """pixi run -e dev python foo.py → python foo.py"""
        cmds = iter_simple_commands("pixi run -e dev python foo.py")
        inner = unwrap_runner(cmds[0])
        assert inner.name == "python"
        assert inner.args == ("foo.py",)

    # --- conda / poetry ---

    def test_conda_run_python(self):
        cmds = iter_simple_commands("conda run python foo.py")
        inner = unwrap_runner(cmds[0])
        assert inner.name == "python"

    def test_conda_run_with_name_flag(self):
        cmds = iter_simple_commands("conda run -n myenv python foo.py")
        inner = unwrap_runner(cmds[0])
        assert inner.name == "python"

    def test_poetry_run_python(self):
        cmds = iter_simple_commands("poetry run python foo.py")
        inner = unwrap_runner(cmds[0])
        assert inner.name == "python"

    # --- env / nohup ---

    def test_env_python(self):
        cmds = iter_simple_commands("env python foo.py")
        inner = unwrap_runner(cmds[0])
        assert inner.name == "python"

    def test_nohup_python(self):
        cmds = iter_simple_commands("nohup python foo.py")
        inner = unwrap_runner(cmds[0])
        assert inner.name == "python"

    def test_env_inline_assignment(self):
        """env FOO=bar python foo.py → assigns preserved, name=python."""
        cmds = iter_simple_commands("env FOO=bar python foo.py")
        inner = unwrap_runner(cmds[0])
        assert inner.name == "python"
        assert inner.assigns.get("FOO") == "bar"

    def test_double_dash_separator(self):
        """env -- python foo.py → python foo.py"""
        cmds = iter_simple_commands("env -- python foo.py")
        inner = unwrap_runner(cmds[0])
        assert inner.name == "python"

    def test_nested_wrappers(self):
        """env nohup python foo.py → python foo.py (two levels)."""
        cmds = iter_simple_commands("env nohup python foo.py")
        inner = unwrap_runner(cmds[0])
        assert inner.name == "python"


# ---------------------------------------------------------------------------
# extract_script_target
# ---------------------------------------------------------------------------

class TestExtractScriptTarget:
    def _sc(self, name: str, *args: str) -> SimpleCommand:
        return SimpleCommand(name=name, args=tuple(args), assigns={}, raw="")

    def test_python_plain(self):
        assert extract_script_target(self._sc("python", "foo.py")) == "foo.py"

    def test_python3(self):
        assert extract_script_target(self._sc("python3", "foo.py")) == "foo.py"

    def test_python_versioned(self):
        assert extract_script_target(self._sc("python3.12", "foo.py")) == "foo.py"

    def test_python_with_flag_before_py(self):
        """-u flag before the .py should be skipped."""
        assert extract_script_target(self._sc("python", "-u", "foo.py")) == "foo.py"

    def test_python_with_multiple_flags(self):
        assert extract_script_target(self._sc("python", "-W", "ignore", "foo.py")) is None
        # -W takes a value; next arg is "ignore" (not .py), so stops

    def test_python_minus_c_not_script(self):
        """-c means one-liner; first positional is code, not a file."""
        assert extract_script_target(self._sc("python", "-c", "print(1)")) is None

    def test_python_minus_m_not_script(self):
        assert extract_script_target(self._sc("python", "-m", "pytest")) is None

    def test_shebang_style(self):
        assert extract_script_target(self._sc("./foo.py")) == "./foo.py"

    def test_shebang_subdir(self):
        assert extract_script_target(self._sc("./scripts/run.py")) == "./scripts/run.py"

    def test_non_python_command(self):
        assert extract_script_target(self._sc("pytest", "foo.py")) is None

    def test_no_py_arg(self):
        assert extract_script_target(self._sc("python")) is None
