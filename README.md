# jupyter-jcli

CLI tool for LLM agents to operate Jupyter Lab servers.

j-cli enables AI agents (and humans) to remotely control Jupyter servers — execute code in kernels, manage sessions, and write outputs back to notebooks, all from the command line.

## Installation

```bash
# from source
uv sync
```

Requires Python 3.10+.

## Quick Start

```bash
# set connection (or pass via -s / -t flags)
export JCLI_JUPYTER_SERVER_URL=http://localhost:8888
export JCLI_JUPYTER_SERVER_TOKEN=your-token

# check connectivity
j-cli healthcheck

# create a session and execute code
j-cli session create --kernel python3 --name my-session
j-cli exec <session_id> --code "print('hello world')"
```

## Commands

### Global Options

| Flag | Description |
|------|-------------|
| `-s`, `--server-url` | Jupyter server URL (env: `JCLI_JUPYTER_SERVER_URL`, default: `http://localhost:8888`) |
| `-t`, `--token` | Auth token (env: `JCLI_JUPYTER_SERVER_TOKEN`) |
| `-j`, `--json` | Output as JSON for programmatic use |
| `--version` | Show version |

### `healthcheck`

Check server connectivity and running kernel count.

```bash
j-cli healthcheck
```

### `kernelspec list`

List available kernel specifications.

```bash
j-cli kernelspec list
```

### `session`

```bash
j-cli session create --kernel python3 --name my-session
j-cli session list
j-cli session kill <session_id>
```

### `kernel`

```bash
j-cli kernel interrupt <session_id>
j-cli kernel restart <session_id>
```

### `setup claude`

Install a Claude Code `PreToolUse` hook that intercepts `jupyter nbconvert --execute`, `papermill`, `runipy`, and similar notebook-execution bypass tools and redirects Claude to use j-cli instead.

```bash
j-cli setup claude           # default: .claude/settings.local.json (gitignored)
j-cli setup claude --project # .claude/settings.json (committed, team-shared)
j-cli setup claude --user    # ~/.claude/settings.json (global, all projects)
```

The command is idempotent — re-running updates the hook in place without duplicating it.

### `vars`

Inspect variables in a kernel session.

```bash
# list all variables (NAME / TYPE / VALUE table)
j-cli vars <session_id>

# inspect a single variable
j-cli vars <session_id> --name x

# rich inspection (MIME-typed data, DAP kernels only)
j-cli vars <session_id> --name x --rich

# JSON output for programmatic use
j-cli -j vars <session_id>
j-cli -j vars <session_id> --name x
```

**Source**: when the kernel advertises debugger support (`kernel_info_reply.supported_features` contains `"debugger"`), the DAP `inspectVariables` control-channel path is used (`source="dap"`). Otherwise a shell-channel code snippet is executed (`source="fallback"`).

**Ordering caveat**: variables are returned in first-definition order (CPython dict insertion order). Re-assigning a variable does **not** move it to the end; only `del x; x = …` does. Do not infer recency from position in the list.

**No mtime**: the Jupyter debug protocol does not expose per-variable last-modified timestamps. No `mtime` or `last_execution_count` field is available in the protocol.

### `session list` variable preview

By default, `session list` fetches a short variable preview for each idle kernel:

```bash
j-cli session list            # includes VARS column (default)
j-cli session list --no-vars  # faster, skips variable fetch
j-cli session list --vars     # force fetch even when >10 sessions
```

Each session row gets a `VARS` column showing the first 5 variable names. A hint line at the bottom points at `j-cli vars <SESSION_ID>` for the full list.

In JSON mode (`-j`), each session object gains a `vars_preview` key:
```json
{"session_id": "...", "vars_preview": {"names": ["x", "df"], "total": 2}}
```

### `exec`

Execute code in a kernel session. Supports inline code, py:percent files, and Jupyter notebooks.

```bash
# inline code
j-cli exec <session_id> --code "import pandas as pd; df = pd.read_csv('data.csv'); df.head()"

# execute from py:percent file
j-cli exec <session_id> --file analysis.py

# execute specific cells from a notebook
j-cli exec <session_id> --file notebook.ipynb --cell 0:3

# execute a single cell
j-cli exec <session_id> --file notebook.ipynb --cell 5
```

**Cell spec formats** (0-indexed):

| Spec | Meaning |
|------|---------|
| `3` | Cell 3 only |
| `3:7` | Cells 3, 4, 5, 6 |
| `3:` | Cell 3 to end |
| `:5` | Cells 0 through 4 |

**Notebook writeback**: When executing from a file, outputs are automatically written back to the paired `.ipynb` file. For `analysis.py`, j-cli looks for `analysis.ipynb` in the same directory.

## Py:Percent Format

j-cli supports the [py:percent](https://jupytext.readthedocs.io/en/latest/formats-scripts.html#the-percent-format) format — plain Python files with cell markers:

```python
# ---
# jupyter:
#   kernelspec:
#     name: python3
# ---

# %%
import numpy as np

# %%
x = np.random.randn(100)
print(x.mean())
```

## Development

```bash
# install with test dependencies
uv sync --extra test

# run tests (requires a real Jupyter server, started automatically by fixtures)
uv run pytest -v
```

## License

MIT
