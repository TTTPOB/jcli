---
name: jcli
description: Use this skill whenever the user wants to execute code on a Jupyter server, manage Jupyter sessions or kernels, run notebook cells, or interact with Jupyter Lab from the command line. Triggers include mentions of Jupyter, notebooks, kernels, ipynb files, or requests to run Python/R code on a remote server. Also use when the user wants to check Jupyter server health, create/list/kill sessions, interrupt/restart kernels, or write execution outputs back to notebooks.
---

# jcli — Jupyter CLI for LLM Agents

## Overview

jcli is a CLI tool that lets you operate Jupyter Lab servers. Use it to execute code in kernels, manage sessions, and write outputs back to notebooks. Always use `--json` (`-j`) flag when you need to parse the output programmatically.

## Prerequisites

Before using jcli, check if it is installed:

```bash
command -v jcli > /dev/null && echo "installed" || echo "not installed"
```

If not installed, install it with:

```bash
uv tool install j-cli
```

Note: the PyPI package name is `j-cli` (not `jcli`, which is occupied by another package).

## Connection

Before running any jcli command, check if the environment variables are already set:

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
   from jcli.parser import parse_file
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
jcli healthcheck
# Output: OK  Jupyter server v2.14.2  0 kernel(s) running

# 2. Detect kernel spec from the file
python -c "from jcli.parser import parse_file; print(parse_file('analysis.py').kernel_name)"
# Output: ir

# 3. Create a session with the detected kernel
jcli -j session create --kernel ir --name analysis
# Output (JSON): {"session_id": "abc-123", "kernel_id": "def-456", "kernel_name": "ir"}

# 4. Execute inline code (use the session_id from step 3)
jcli exec abc-123 --code "print(1 + 1)"

# 5. Execute cells from a notebook
jcli exec abc-123 --file analysis.ipynb --cell 0:5

# 6. Execute from a py:percent file (outputs auto-written to paired .ipynb)
jcli exec abc-123 --file analysis.py

# 7. Clean up
jcli session kill abc-123
```

## Commands Reference

### `healthcheck`

Check server connectivity and running kernel count.

```bash
jcli healthcheck
jcli -j healthcheck
# JSON: {"status": "ok", "version": "2.14.2", "kernels_running": 1}
```

### `kernelspec list`

List available kernel specifications on the server.

```bash
jcli kernelspec list
jcli -j kernelspec list
```

### `session create`

Create a new session. Returns the session_id needed for all subsequent commands.

```bash
jcli session create --kernel python3
jcli session create --kernel python3 --name my-analysis
jcli -j session create --kernel python3
# JSON: {"session_id": "...", "kernel_id": "...", "kernel_name": "python3"}
```

### `session list`

List all active sessions with their kernel state.

```bash
jcli session list
jcli -j session list
# JSON: {"sessions": [{"session_id": "...", "kernel_id": "...", "kernel_name": "python3", "kernel_state": "idle", "name": "..."}]}
```

### `session kill`

Delete a session and shut down its kernel.

```bash
jcli session kill <session_id>
```

### `kernel interrupt`

Interrupt a running kernel (e.g., stuck execution).

```bash
jcli kernel interrupt <session_id>
```

### `kernel restart`

Restart a kernel (clears all state).

```bash
jcli kernel restart <session_id>
```

### `exec`

Execute code in a kernel session. This is the most important command.

**Inline code:**
```bash
jcli exec <session_id> --code "print('hello')"
jcli exec <session_id> -c "import pandas as pd; df = pd.read_csv('data.csv'); df.describe()"
```

**From a file:**
```bash
# All code cells from a notebook (omit --cell to run everything)
jcli exec <session_id> --file notebook.ipynb

# Single cell (0-indexed)
jcli exec <session_id> --file notebook.ipynb --cell 3

# Multiple consecutive cells via range
jcli exec <session_id> --file notebook.ipynb --cell 0:5    # cells 0,1,2,3,4
jcli exec <session_id> --file notebook.ipynb --cell 3:     # cell 3 to end
jcli exec <session_id> --file notebook.ipynb --cell :3      # cells 0,1,2

# From py:percent file
jcli exec <session_id> --file script.py --cell 0
```

Each cell in the range is executed sequentially. Outputs are reported per cell with `--- cell N ---` separators (or per-cell JSON objects with `-j`).

**Timeout** (default 300s):
```bash
jcli exec <session_id> --code "long_computation()" --timeout 600
```

**JSON output** (for parsing results programmatically):
```bash
jcli -j exec <session_id> --code "print('hello')"
# JSON: {"status": "ok", "outputs": [{"type": "stream", "stream_name": "stdout", "text": "hello\n"}]}

jcli -j exec <session_id> --file notebook.ipynb --cell 0:3
# JSON: {"status": "ok", "cells": [{"cell_index": 0, "outputs": [...], "execution_count": 1}, ...], "notebook_updated": "notebook.ipynb"}
```

## Notebook Writeback

When executing from a file, jcli automatically writes outputs back to the paired `.ipynb`:

- `notebook.ipynb` → outputs written back to itself
- `analysis.py` → outputs written to `analysis.ipynb` (if it exists in the same directory)
- `analysis.dummy.py` → outputs written to `analysis.ipynb`

This keeps notebooks in sync with their execution results.

## Py:Percent Format

jcli supports py:percent format — plain Python files with `# %%` cell markers:

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
