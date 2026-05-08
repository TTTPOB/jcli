# Streaming Exec Writeback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `j-cli exec --file` write notebook outputs and emit stdout after each completed cell.

**Architecture:** Keep the change in `jupyter_jcli.commands.exec_cmd`: determine the notebook target before the loop, execute cells sequentially, and call writeback plus stdout emission after each cell returns. Reuse the existing notebook writer and output formatting helpers; only file execution JSON changes to JSON Lines.

**Tech Stack:** Python, Click, nbformat, pytest, uv.

---

### Task 1: Add Streaming Behavior Tests

**Files:**
- Modify: `tests/test_notebook_writeback.py`
- Modify: `tests/test_exec.py`

- [ ] **Step 1: Add a per-cell writeback timing test**

Append this import block near the existing imports in `tests/test_notebook_writeback.py`:

```python
from unittest.mock import patch
```

Add this test method to `TestPyPercentWriteback`:

```python
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
```

- [ ] **Step 2: Add a JSONL output test**

Add this test method to `TestExecFile` in `tests/test_exec.py`:

```python
def test_file_json_output_is_jsonl_per_cell(self, live_session, mock_kernel_connection, tmp_path):
    runner = CliRunner()
    script = tmp_path / "jsonl.py"
    script.write_text(textwrap.dedent("""\
        # %%
        print("alpha")

        # %%
        print("beta")
    """))

    result = runner.invoke(main, [
        "-s", live_session["url"], "-t", live_session["token"],
        "--json", "exec", live_session["session_id"],
        "--file", str(script), "--cell", "0:2",
    ])

    assert result.exit_code == 0
    lines = [json.loads(line) for line in result.output.splitlines() if line.strip()]
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
```

- [ ] **Step 3: Add a partial-writeback-on-error test**

Add this test method to `TestPyPercentWriteback`:

```python
def test_completed_cells_are_written_before_later_kernel_exception(self, live_session, mock_kernel_connection, tmp_path):
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

    def _execute_then_raise(source, timeout=10):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("simulated kernel transport failure")
        return original_execute(source, timeout=timeout)

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
```

- [ ] **Step 4: Run the focused tests and verify they fail for the expected reason**

Run:

```bash
uv run pytest tests/test_notebook_writeback.py::TestPyPercentWriteback::test_writeback_happens_after_each_completed_cell tests/test_notebook_writeback.py::TestPyPercentWriteback::test_completed_cells_are_written_before_later_kernel_exception tests/test_exec.py::TestExecFile::test_file_json_output_is_jsonl_per_cell -q
```

Expected: at least the writeback timing test fails with `writeback_sizes == [2]` or equivalent, and the JSONL test fails because current output is one JSON object.

### Task 2: Implement Per-Cell Writeback And Output

**Files:**
- Modify: `jupyter_jcli/commands/exec_cmd.py`

- [ ] **Step 1: Move notebook target creation before the cell loop**

In `_exec_file`, after selecting cells and before opening `kernel_connection`, add logic equivalent to:

```python
notebook_created = None
ipynb_path = parsed.paired_ipynb
if ipynb_path is None and parsed.is_py_percent and file_path.endswith(".py"):
    from jupyter_jcli.parser import ipynb_path_for_py
    from jupyter_jcli.pair_io import create_ipynb_from_parsed
    import nbformat as _nbformat

    target = ipynb_path_for_py(Path(file_path))
    nb = create_ipynb_from_parsed(parsed)
    _nbformat.write(nb, str(target))
    parsed.paired_ipynb = str(target)
    ipynb_path = str(target)
    notebook_created = str(target)
```

Remove the old post-loop auto-create block.

- [ ] **Step 2: Emit one cell result inside the loop**

Replace the post-loop accumulation path with per-cell handling:

```python
cells_executed = 0
last_notebook_updated = None

with kernel_connection(ctx.server_url, ctx.token, kernel_id) as kernel:
    for cell in selected:
        ...
        cell_result = {
            "cell_index": cell.index,
            "source_preview": cell.source[:80].replace("\n", " "),
            "outputs": outputs,
            "raw_outputs": raw_outputs,
            "execution_count": result.get("execution_count"),
        }

        notebook_updated = None
        if ipynb_path:
            notebook_updated = write_outputs_to_notebook(ipynb_path, [cell_result])
            last_notebook_updated = notebook_updated

        cells_executed += 1
        _emit_file_cell_result(ctx, cell_result, notebook_created, notebook_updated)
        notebook_created = None
```

Create helper functions in the same module:

```python
def _emit_file_cell_result(ctx: Context, cell_result: dict, notebook_created: str | None, notebook_updated: str | None) -> None:
    if ctx.use_json:
        cell_payload = {k: v for k, v in cell_result.items() if k != "raw_outputs"}
        data = {"status": ResponseStatus.OK, "cell": cell_payload}
        if notebook_created:
            data["notebook_created"] = notebook_created
        if notebook_updated:
            data["notebook_updated"] = notebook_updated
        emit(data, use_json=True, compact_json=True)
        return

    parts = [f"--- cell {cell_result['cell_index']} ---"]
    text = format_outputs_human(cell_result["outputs"])
    if text:
        parts.append(text)
    if notebook_created:
        parts.append(f"Notebook created: {notebook_created}")
    if notebook_updated:
        parts.append(f"Notebook updated: {notebook_updated}")
    emit({"_human": "\n".join(parts)}, use_json=False)
```

If adding `compact_json` to `emit` is too broad, print compact JSON directly with `json.dumps(..., ensure_ascii=False)` inside this helper.

- [ ] **Step 3: Emit a JSON summary after the loop**

After the loop, emit only for JSON mode:

```python
if ctx.use_json:
    summary = {"cells_executed": cells_executed}
    if last_notebook_updated:
        summary["notebook_updated"] = last_notebook_updated
    emit({"status": ResponseStatus.OK, "summary": summary}, use_json=True, compact_json=True)
```

Do not emit a final human summary.

- [ ] **Step 4: Run focused tests and verify they pass**

Run:

```bash
uv run pytest tests/test_notebook_writeback.py::TestPyPercentWriteback::test_writeback_happens_after_each_completed_cell tests/test_notebook_writeback.py::TestPyPercentWriteback::test_completed_cells_are_written_before_later_kernel_exception tests/test_exec.py::TestExecFile::test_file_json_output_is_jsonl_per_cell -q
```

Expected: all selected tests pass.

### Task 3: Update Existing Tests For JSONL Contract

**Files:**
- Modify: `tests/test_notebook_writeback.py`
- Modify: `tests/test_exec.py`

- [ ] **Step 1: Replace old `json.loads(result.output)` file-exec assertions**

For existing `--json exec --file` tests, parse JSONL:

```python
lines = [json.loads(line) for line in result.output.splitlines() if line.strip()]
cell_events = [line for line in lines if "cell" in line]
summary = next(line["summary"] for line in lines if "summary" in line)
```

Update assertions to read `cell_events[N]["cell"]` and `summary`.

- [ ] **Step 2: Run exec and writeback tests**

Run:

```bash
uv run pytest tests/test_exec.py tests/test_notebook_writeback.py -q
```

Expected: both files pass.

### Task 4: Update Docs And CLI Help

**Files:**
- Modify: `jupyter_jcli/cli.py`
- Modify: `README.md`
- Modify: `skills/j-cli/SKILL.md`

- [ ] **Step 1: Update global JSON help**

Change the `--json` help text in `jupyter_jcli/cli.py` from:

```python
help="Output as JSON instead of human-readable text",
```

to:

```python
help="Output as JSON for commands; exec --file streams JSON Lines for scripts",
```

- [ ] **Step 2: Update README exec/writeback docs**

In `README.md`, update the `exec` section to say:

```markdown
For file execution, each selected cell is executed sequentially. After a cell finishes, j-cli immediately prints that cell's output and writes that cell's outputs back to the target notebook when writeback applies.

Human mode is intended for direct reading by people and agents. Use `--json` when a script needs structured output; `j-cli --json exec --file ...` streams JSON Lines, one object per completed cell plus a final summary object.
```

Update the notebook writeback paragraph to mention per-cell writeback.

- [ ] **Step 3: Update `skills/j-cli/SKILL.md`**

In the `exec` command section, update the JSON examples to show JSONL for file execution and add:

```markdown
When you are an LLM/agent reading the output yourself, prefer the default human mode. Do not use `--json` just because you are a machine; JSON/JSONL mode is for scripts or tools that need to parse output programmatically.
```

Also update the Notebook Writeback section to say writeback happens after each completed cell for file execution.

- [ ] **Step 4: Run docs/help smoke checks**

Run:

```bash
uv run python -m jupyter_jcli --help
```

Expected: help includes the updated JSON help text.

### Task 5: Full Verification And Commit

**Files:**
- All modified files

- [ ] **Step 1: Run full test suite**

Run:

```bash
uv run pytest
```

Expected: all tests pass.

- [ ] **Step 2: Check git diff**

Run:

```bash
git diff --stat
git diff -- jupyter_jcli/commands/exec_cmd.py jupyter_jcli/cli.py README.md skills/j-cli/SKILL.md tests/test_exec.py tests/test_notebook_writeback.py
```

Expected: only the streaming exec/writeback behavior, tests, and docs changed.

- [ ] **Step 3: Commit atomically**

Run:

```bash
git add jupyter_jcli/commands/exec_cmd.py jupyter_jcli/cli.py README.md skills/j-cli/SKILL.md tests/test_exec.py tests/test_notebook_writeback.py
git commit -m "feat: stream exec file writeback per cell"
```

Expected: one feature commit containing implementation, tests, and related docs.
