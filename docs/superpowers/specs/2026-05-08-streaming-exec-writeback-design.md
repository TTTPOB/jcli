# Streaming Exec Writeback Design

## Context

`j-cli exec --file` currently executes all selected code cells, stores their results in memory, writes notebook outputs once after the loop finishes, then prints the accumulated output. This means long notebook runs give no stdout progress until the end, and a timeout or unexpected kernel/client exception before the final write loses already completed cell outputs.

The requested behavior is option 1 from the design discussion: after each selected cell finishes, j-cli writes that cell's outputs back to the target notebook and prints that cell's result to stdout immediately.

## Goals

- Write completed cell outputs to the paired `.ipynb` immediately after each cell finishes.
- Print completed cell outputs to stdout immediately after each cell finishes.
- Preserve existing inline `--code` behavior.
- Make `--json` file execution use JSON Lines so scripts can parse streaming progress one object per line.
- Document that human mode is the default mode agents should use for direct inspection; JSON mode is for script parsing, not for LLMs simply reading output.
- Keep timeout and error behavior simple: failures still exit non-zero, while completed cells remain written and visible.

## Non-Goals

- Do not stream partial output from a still-running cell. Output is emitted after the kernel reports the cell execution result.
- Do not add a new CLI flag for streaming. Streaming becomes the default behavior for file execution.
- Do not change plain script detection, paired notebook creation rules, or inline `--code` output format.
- Do not preserve the old single JSON object response for `--json exec --file`; there are currently no script consumers that require compatibility.

## Architecture

The change stays inside the existing exec/writeback boundary:

- `jupyter_jcli.commands.exec_cmd` owns execution order, stdout emission, notebook creation, and per-cell writeback timing.
- `jupyter_jcli.notebook_writer.write_outputs_to_notebook` remains the writeback primitive. It already accepts a list of cell results, so the exec command can call it with a single-result list after each completed cell.
- `jupyter_jcli.executor.process_outputs` and `format_outputs_human` remain the output normalization and human formatting path.
- `jupyter_jcli.output.emit` remains the final printing helper, but file execution can call it once per cell instead of once at the end.

No new persistent state or background process is needed.

## Data Flow

For `j-cli exec <session> --file <path>`:

1. Parse the input file and select code cells as today.
2. Before executing cells, determine the notebook writeback target.
3. If the input is a py:percent file without an existing pair, create the paired `.ipynb` before the first cell executes so every completed cell can write back immediately.
4. Execute selected cells sequentially through the existing kernel connection.
5. After each cell returns:
   - Normalize outputs with `process_outputs`.
   - Build a single `cell_result`.
   - Write that cell back with `write_outputs_to_notebook(ipynb_path, [cell_result])` when a notebook target exists.
   - Print the cell result immediately.
6. After all selected cells finish, print a small completion record only in JSON mode.

Plain Python scripts that are not py:percent files still print to stdout only and never create a notebook.

## Human Output

Human mode should remain optimized for direct reading by an agent or a person:

```text
--- cell 0 ---
hello
Notebook updated: analysis.ipynb
--- cell 1 ---
42
Notebook updated: analysis.ipynb
```

If the notebook was auto-created, the first cell's human output includes `Notebook created: ...` before or near the first `Notebook updated: ...` message. Repeating `Notebook updated` per cell is acceptable because it makes writeback timing explicit.

## JSON Output

For file execution only, `--json` changes from one pretty-printed JSON document to JSON Lines. Each completed cell prints one compact JSON object on its own line:

```jsonl
{"status":"ok","cell":{"cell_index":0,"source_preview":"print('hello')","outputs":[...],"execution_count":1},"notebook_created":"analysis.ipynb","notebook_updated":"analysis.ipynb"}
{"status":"ok","cell":{"cell_index":1,"source_preview":"42","outputs":[...],"execution_count":2},"notebook_updated":"analysis.ipynb"}
{"status":"ok","summary":{"cells_executed":2,"notebook_updated":"analysis.ipynb"}}
```

Inline `--code --json` stays as the existing single JSON object because it has only one execution unit and no notebook writeback loop.

The skill documentation must make this usage expectation explicit: agents should normally use human mode when they are reading command output themselves. JSON/JSONL mode is for scripts and tools that parse structured output.

## Error Handling

- If no code cells are selected, behavior stays the same: print a parse error and exit non-zero.
- If the total timeout expires before starting a later cell, previously completed cells have already been written and printed; j-cli emits the timeout error and exits non-zero.
- If kernel execution raises an exception outside normal cell error output, previously completed cells remain written and printed; j-cli emits the execution error and exits non-zero.
- If a notebook cell itself produces an error output and the kernel client returns it as normal outputs, j-cli writes and prints that error output for that cell, then continues following the existing kernel-client behavior.
- If per-cell writeback fails, j-cli should emit an execution error and exit non-zero rather than continuing and misleading the caller about notebook state.

## Documentation

Update all user-facing docs that describe file execution and writeback:

- `README.md`: describe per-cell writeback, immediate stdout, and JSONL for `--json exec --file`.
- `skills/j-cli/SKILL.md`: mirror the README behavior and add the agent guidance that direct LLM use should prefer human mode.
- CLI help text where needed: clarify that `--json` produces JSON Lines for file execution.

## Tests

Add focused tests for the changed behavior:

- Human file execution writes back and emits stdout after each cell. A practical unit test can monkeypatch writeback/emit or inspect call order.
- JSON file execution emits parseable JSONL with one object per cell and a final summary object.
- If a later cell times out or raises through the kernel client, earlier completed cells are already written to the notebook.
- Existing writeback tests for py:percent, direct `.ipynb`, images, and plain scripts continue to pass.

## Acceptance Criteria

- Running a multi-cell file execution updates the notebook after each completed cell, not only after all cells finish.
- stdout shows each completed cell promptly in human mode.
- `--json exec --file` produces one JSON object per line for streaming script consumers.
- Inline `--code` behavior is unchanged.
- README and j-cli skill docs clearly describe the new behavior and agent guidance.
- Relevant tests pass with `uv run pytest`.
