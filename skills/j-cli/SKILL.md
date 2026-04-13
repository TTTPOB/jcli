---
name: j-cli
description: Use this skill whenever the user wants to execute code on a Jupyter server, manage Jupyter sessions or kernels, run notebook cells, or interact with Jupyter Lab from the command line. Triggers include mentions of Jupyter, notebooks, kernels, ipynb files, or requests to run Python/R code on a remote server. Also use when the user wants to check Jupyter server health, create/list/kill sessions, interrupt/restart kernels, write execution outputs back to notebooks, search notebook content with ripgrep, or edit a notebook by editing its py:percent pair.
---

# j-cli — Jupyter CLI for LLM Agents

## Overview

j-cli is a CLI tool that lets you operate Jupyter Lab servers. Use it to execute code in kernels, manage sessions, and write outputs back to notebooks. Always use `--json` (`-j`) flag when you need to parse the output programmatically.

## One-time Claude Code hook install

Run this once per project to prevent Claude from falling back to `jupyter nbconvert --execute` (or `papermill` / `runipy`) instead of j-cli:

```bash
j-cli setup claude --local    # writes .claude/settings.local.json (gitignored, this machine only)
# or:
j-cli setup claude --project  # writes .claude/settings.json       (committed, team-shared)
# or:
j-cli setup claude --user     # writes ~/.claude/settings.json     (global, all projects)
```

The command is idempotent — re-running updates the hook in place without duplicating it.

**What the hooks install:**

- **`notebook-exec-guard`** (Bash, hard deny) — blocks `jupyter nbconvert --execute`, `papermill`, `runipy`, and `ipython <notebook>.ipynb`. These tools bypass j-cli and lose kernel state.
- **`python-run-guard`** (Bash, soft deny) — fires when a command like `python foo.py`, `uv run python foo.py`, `pixi run python foo.py`, or `./foo.py` targets a `.py` file that has a paired `.ipynb` next to it. The guard surfaces a "reconsider" message explaining that running the file as a script discards kernel state and py/ipynb pair sync. The agent is expected to use `j-cli session` + `j-cli exec` instead. Commands on ordinary scripts (no paired `.ipynb`) are never intercepted.
- **`pair-drift-guard`** (Edit/Write and NotebookEdit) — detects and auto-merges drift between `.py` / `.ipynb` pairs; hard-denies `NotebookEdit` (use the py:percent round-trip instead).

## Installing the git pre-commit hook

Run once per repository to keep `.py` / `.ipynb` pairs in sync at commit time:

```bash
j-cli setup git                             # default --project scope
j-cli setup git --project                   # .githooks/pre-commit + core.hooksPath
j-cli setup git --local                     # .git/hooks/pre-commit (this clone only)
j-cli setup git --include 'src/*.py'        # only watch .py files under src/
j-cli setup git --include 'a/*.py' --include 'b/*.py'   # multiple globs (OR logic)
```

**What the installer does:**

- Writes a bash shim at the hook path that delegates to `j-cli _hooks pre-commit-pair-sync`
- `--project` (default): stores the hook under `.githooks/pre-commit` and sets
  `git config --local core.hooksPath .githooks`
- `--local`: writes directly to `.git/hooks/pre-commit`; does not touch `core.hooksPath`
- Injects a managed block into `.gitignore` so `*.ipynb` files are never accidentally committed:

```
# >>> jcli managed (git hooks) >>>
*.ipynb
# <<< jcli managed (git hooks) <<<
```

The installer is idempotent — re-running updates the hook shim and `.gitignore` block in place.

**Hook behaviour at commit time:**

| Situation | Result |
|-----------|--------|
| `.ipynb` staged | Blocked — unstage it, commit only the `.py` pair |
| Pair in sync | Silently allowed |
| One side changed (auto-merge possible) | Merged content written back; `.py` re-staged if updated |
| Both sides changed the **same** cell | Commit blocked — resolve manually |
| No git base (first commit) + drift | Commit blocked — pick a side |

When a conflict is detected, the hook prints the conflicting cell indices and suggests:

```bash
j-cli convert ipynb-to-py <nb.ipynb> <nb.py>   # take ipynb as truth
j-cli convert py-to-ipynb <nb.py> <nb.ipynb>    # take py as truth
```

## Prerequisites

Before using j-cli, check if it is installed:

```bash
command -v j-cli > /dev/null && echo "installed" || echo "not installed"
```

If not installed, install it with:

```bash
uv tool install jupyter-jcli
```

Note: the PyPI package name is `jupyter-jcli`, the binary name is `j-cli`.

## Connection

Before running any j-cli command, check if the environment variables are already set:

```bash
[ -n "$JCLI_JUPYTER_SERVER_URL" ] && echo "URL: set" || echo "URL: unset"
[ -n "$JCLI_JUPYTER_SERVER_TOKEN" ] && echo "TOKEN: set" || echo "TOKEN: unset"
```

- If both are set, proceed directly — do not re-export them.
- If either is unset, ask the user for the missing value(s), then export:

```bash
export JCLI_JUPYTER_SERVER_URL=http://localhost:8888
export JCLI_JUPYTER_SERVER_TOKEN=<token>
```

You can also pass them as flags per-command: `-s <url>` and `-t <token>`.

## Workflow

A typical workflow follows these steps:

1. **Check connectivity** — verify the server is reachable
2. **Detect kernel spec** — if the user provides a `.py` or `.ipynb` file, use the parser module to extract the kernel name:
   ```python
   from jupyter_jcli.parser import parse_file
   parsed = parse_file("analysis.py")  # or "notebook.ipynb"
   print(parsed.kernel_name)  # e.g. "ir", "python3", "julia-1.10"
   ```
   Use `parsed.kernel_name` as the `--kernel` value when creating the session. If it's `None`, fall back to `python3` or ask the user.
3. **Create a session** — use the detected kernel spec (or fall back to `python3`)
4. **Execute code** — run inline code or cells from files
5. **Clean up** — kill the session when done

### Step-by-step Example

```bash
# 1. Healthcheck
j-cli healthcheck
# Output: OK  Jupyter server v2.14.2  0 kernel(s) running

# 2. Detect kernel spec from the file
python -c "from jupyter_jcli.parser import parse_file; print(parse_file('analysis.py').kernel_name)"
# Output: ir

# 3. Create a session with the detected kernel
j-cli -j session create --kernel ir --name analysis
# Output (JSON): {"session_id": "abc-123", "kernel_id": "def-456", "kernel_name": "ir"}

# 4. Execute inline code (use the session_id from step 3)
j-cli exec abc-123 --code "print(1 + 1)"

# 5. Execute cells from a notebook
j-cli exec abc-123 --file analysis.ipynb --cell 0:5

# 6. Execute from a py:percent file (outputs auto-written to paired .ipynb)
j-cli exec abc-123 --file analysis.py

# 7. Clean up
j-cli session kill abc-123
```

## Commands Reference

### `healthcheck`

Check server connectivity and running kernel count.

```bash
j-cli healthcheck
j-cli -j healthcheck
# JSON: {"status": "ok", "version": "2.14.2", "kernels_running": 1}
```

### `kernelspec list`

List available kernel specifications on the server.

```bash
j-cli kernelspec list
j-cli -j kernelspec list
```

### `session create`

Create a new session. Returns the session_id needed for all subsequent commands.

```bash
j-cli session create --kernel python3
j-cli session create --kernel python3 --name my-analysis
j-cli -j session create --kernel python3
# JSON: {"session_id": "...", "kernel_id": "...", "kernel_name": "python3"}
```

### `session list`

List all active sessions with their kernel state.

```bash
j-cli session list
j-cli -j session list
# JSON: {"sessions": [{"session_id": "...", "kernel_id": "...", "kernel_name": "python3", "kernel_state": "idle", "name": "..."}]}
```

### `session kill`

Delete a session and shut down its kernel.

```bash
j-cli session kill <session_id>
```

### `kernel interrupt`

Interrupt a running kernel (e.g., stuck execution).

```bash
j-cli kernel interrupt <session_id>
```

### `kernel restart`

Restart a kernel (clears all state).

```bash
j-cli kernel restart <session_id>
```

### `exec`

Execute code in a kernel session. This is the most important command.

**Inline code:**
```bash
j-cli exec <session_id> --code "print('hello')"
j-cli exec <session_id> -c "import pandas as pd; df = pd.read_csv('data.csv'); df.describe()"
```

**From a file:**
```bash
# All code cells from a notebook (omit --cell to run everything)
j-cli exec <session_id> --file notebook.ipynb

# Single cell (0-indexed)
j-cli exec <session_id> --file notebook.ipynb --cell 3

# Multiple consecutive cells via range
j-cli exec <session_id> --file notebook.ipynb --cell 0:5    # cells 0,1,2,3,4
j-cli exec <session_id> --file notebook.ipynb --cell 3:     # cell 3 to end
j-cli exec <session_id> --file notebook.ipynb --cell :3      # cells 0,1,2

# From py:percent file
j-cli exec <session_id> --file script.py --cell 0
```

Each cell in the range is executed sequentially. Outputs are reported per cell with `--- cell N ---` separators (or per-cell JSON objects with `-j`).

**Timeout** (default 300s):
```bash
j-cli exec <session_id> --code "long_computation()" --timeout 600
```

**JSON output** (for parsing results programmatically):
```bash
j-cli -j exec <session_id> --code "print('hello')"
# JSON: {"status": "ok", "outputs": [{"type": "stream", "stream_name": "stdout", "text": "hello\n"}]}

j-cli -j exec <session_id> --file notebook.ipynb --cell 0:3
# JSON: {"status": "ok", "cells": [{"cell_index": 0, "outputs": [...], "execution_count": 1}, ...], "notebook_updated": "notebook.ipynb"}
```

## Notebook Writeback

When executing from a file, j-cli automatically writes outputs back to the paired `.ipynb`:

- `notebook.ipynb` → outputs written back to itself
- `analysis.py` → outputs written to `analysis.ipynb` (if it exists in the same directory)
- `analysis.dummy.py` → outputs written to `analysis.ipynb`

This keeps notebooks in sync with their execution results.

## Searching notebook content with ripgrep

Use `rg` with the `--pre` flag and the bundled preprocessor to search inside `.ipynb` files:

```bash
# Search all notebooks for a pattern
rg --pre skills/j-cli/scripts/rg_ipynb_preprocessor.py 'pattern' .

# Search only .ipynb files
rg --pre skills/j-cli/scripts/rg_ipynb_preprocessor.py -g '*.ipynb' 'pattern' .

# The preprocessor renders each notebook as plain text: cell sources and outputs
# Binary outputs (images, PDFs) are replaced with a size notice
```

The preprocessor is at `skills/j-cli/scripts/rg_ipynb_preprocessor.py` and has no
external dependencies.

## Py:Percent Format

j-cli supports py:percent format — plain Python files with `# %%` cell markers:

```python
# ---
# jupyter:
#   kernelspec:
#     name: python3
# ---

# %%
import matplotlib.pyplot as plt
import numpy as np

# %%
x = np.linspace(0, 10, 100)
plt.plot(x, np.sin(x))
plt.savefig("sine.png")
plt.show()

# %% [markdown]
# ## Results
# The plot above shows a sine wave.
```

### Editing via py:percent round-trip

**Never edit `.ipynb` files directly** — use the py:percent round-trip to edit notebook
cells safely without losing outputs:

```bash
# 1. Convert notebook to py:percent (outputs are preserved in the .ipynb)
j-cli convert ipynb-to-py analysis.ipynb analysis.py

# 2. Edit analysis.py using normal text tools (Edit tool, etc.)
#    Cell markers: # %% (code), # %% [markdown], # %% [raw]

# 3. Write edited sources back — outputs/metadata in the .ipynb are untouched
j-cli convert py-to-ipynb analysis.py analysis.ipynb
```

If a paired `.py` already exists (same stem), you can go directly to step 2 and then step 3.

The `j-cli convert py-to-ipynb` command detects whether the `.ipynb` already exists:
- **Exists** → source-only update (outputs, execution counts, metadata preserved)
- **Does not exist** → new notebook created from the py cells

> **Policy**: The `NotebookEdit` tool is disabled by the `pair-drift-guard` hook installed
> via `j-cli setup claude`. Always go through the py:percent round-trip instead.

## Error Handling

Errors return structured error codes. In JSON mode:

```json
{"status": "error", "code": "SESSION_NOT_FOUND", "message": "..."}
{"status": "error", "code": "EXECUTION_ERROR", "message": "..."}
{"status": "error", "code": "CONNECTION_FAILED", "message": "..."}
{"status": "error", "code": "PARSE_ERROR", "message": "..."}
```

Error codes: `CONNECTION_FAILED`, `SESSION_NOT_FOUND`, `SESSION_CREATE_FAILED`, `KERNEL_NOT_FOUND`, `EXECUTION_ERROR`, `PARSE_ERROR`.

All errors exit with code 1.

## Tips for Agents

- Always use `-j` (JSON mode) when you need to parse output — it gives structured, machine-readable results.
- Save the `session_id` from `session create` — you need it for every subsequent command.
- Use `--cell` to run specific cells instead of entire notebooks when debugging.
- If execution hangs, use `kernel interrupt` followed by retry.
- If kernel state is corrupted, use `kernel restart` (this clears all variables).
- Images in execution output are automatically extracted to temp files with paths included in the output.
- Clean up sessions with `session kill` when done to free server resources.
