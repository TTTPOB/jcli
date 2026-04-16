# jupyter-jcli

CLI tool for LLM agents to operate Jupyter Lab servers.

j-cli enables AI agents (and humans) to remotely control Jupyter servers — execute code in kernels, manage sessions, and write outputs back to notebooks, all from the command line.

## Installation

```bash
# latest release
uv tool install jupyter-jcli

# latest dev version
uv tool install git+https://github.com/tttpob/jcli.git
```

Requires Python 3.10+.

## Recommended Workflow

### 1. Set up environment variables

Use [direnv](https://direnv.net/) so the env vars are loaded automatically whenever you enter the project directory:

```bash
# .envrc
export JCLI_JUPYTER_SERVER_URL=http://localhost:8888
export JCLI_JUPYTER_SERVER_TOKEN=your-token
```

```bash
direnv allow
```

### 2. Launch Jupyter

```bash
# stdout is pipe-safe — the hint line goes to stderr
$(j-cli serve-cmd --serve-backend lab)
```

This prints (and immediately executes) a command like:

```
jupyter lab --ServerApp.token="$JCLI_JUPYTER_SERVER_TOKEN" \
    --ServerApp.ip=localhost --ServerApp.port=8888 --no-browser
```

The token value is never inlined; it is always referenced as `$JCLI_JUPYTER_SERVER_TOKEN`.

### 3. Verify connectivity

```bash
j-cli healthcheck
```

### 4. Set up hooks (once per project)

Install Claude Code hooks so the AI redirects notebook edits through j-cli:

```bash
j-cli setup claude
```

Install the git `pre-commit` hook to keep `.py` / `.ipynb` pairs in sync:

```bash
j-cli setup git
```

If your notebooks live in a subdirectory, limit pair detection to that path
(avoids false positives elsewhere in the repo). `--include` can be repeated:

```bash
j-cli setup git --include "notebooks/*"
# or multiple directories
j-cli setup git --include "notebooks/*" --include "experiments/*"
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

Install Claude Code `PreToolUse` hooks that intercept notebook-execution bypass tools and pair-drift between `.py` and `.ipynb` files, redirecting Claude to use j-cli instead.

```bash
j-cli setup claude           # default: .claude/settings.local.json (gitignored)
j-cli setup claude --project # .claude/settings.json (committed, team-shared)
j-cli setup claude --user    # ~/.claude/settings.json (global, all projects)

# remove all j-cli managed hooks from the target file
j-cli setup claude --remove
j-cli setup claude --project --remove
```

The install command is idempotent — re-running updates hooks in place without duplicating them. `--remove` prunes only j-cli managed entries, preserving any unrelated user hooks. If the settings file becomes empty after removal it is deleted.

### `setup git`

Install a `pre-commit` hook shim that runs `j-cli _hooks pre-commit-pair-sync` and update `.gitignore` to exclude paired `.ipynb` files.

```bash
j-cli setup git              # default: .githooks/pre-commit + set core.hooksPath
j-cli setup git --local      # .git/hooks/pre-commit (this clone only)
j-cli setup git --include "src/*.py"  # only sync matching files

# remove the managed hook and gitignore block
j-cli setup git --remove
j-cli setup git --local --remove
```

`--remove` deletes the hook only if it was written by j-cli, leaves `core.hooksPath` alone if it points to a non-j-cli directory, and removes the managed `.gitignore` block. Unrecognised hooks are skipped with a warning.

### `setup codex` (not yet available)

Codex hook support is blocked upstream: the Codex hook API currently only
reports the tool name `bash`, which means we cannot match `NotebookEdit` or
`Edit|Write` the way we do for Claude Code. Once Codex exposes per-tool
names we will add `j-cli setup codex`. Track upstream progress at
<https://developers.openai.com/codex/hooks>.

### `serve-cmd`

Print a copy-pasteable Jupyter launch command that references the token via an environment variable rather than inlining it.

```bash
# set env vars (token is never echoed to the terminal)
export JCLI_JUPYTER_SERVER_URL=http://localhost:8888
export JCLI_JUPYTER_SERVER_TOKEN=your-token

j-cli serve-cmd --serve-backend lab
# → jupyter lab --ServerApp.token="$JCLI_JUPYTER_SERVER_TOKEN" \
#       --ServerApp.ip=localhost --ServerApp.port=8888 --no-browser

# override host / port / root dir
j-cli serve-cmd --serve-backend lab --ip 0.0.0.0 --port 9000 --root-dir /work

# remove --no-browser (useful for desktop Jupyter)
j-cli serve-cmd --serve-backend notebook --browser

# JSON output (for programmatic use)
j-cli -j serve-cmd --serve-backend server
```

The hint line (`# paste this into a shell …`) is written to **stderr** so the command itself can be used safely in `$()` substitution. The token reference `"$JCLI_JUPYTER_SERVER_TOKEN"` is always a literal shell variable reference — the actual token value is never inlined.

`--serve-backend` must be one of `lab`, `server`, or `notebook`.

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

**Notebook writeback**: When executing from a py:percent file (one with `# %%` cell markers or a `# ---` front matter block), outputs are automatically written back to the paired `.ipynb`. If `analysis.ipynb` does not yet exist, j-cli creates it automatically. Plain Python scripts without markers are executed normally without creating a notebook.

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
