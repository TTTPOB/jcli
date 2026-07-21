# jupyter-jcli

CLI tool for LLM agents to operate Jupyter Lab servers.

j-cli enables AI agents (and humans) to remotely control Jupyter servers — execute code in kernels, manage sessions, and write outputs back to notebooks, all from the command line.

## Installation

```bash
# latest release
uv tool install jupyter-jcli

# latest dev version
uv tool install git+https://github.com/tttpob/jcli.git

# verify the installed CLI
j-cli --version
```

Requires Python 3.10+.

Note: the PyPI package name is `jupyter-jcli`, while the installed binary is `j-cli`.

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

Install hooks for your coding agent so it redirects notebook edits through j-cli:

```bash
j-cli setup claude
# or
j-cli setup codex
# or
j-cli setup opencode
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
| `-j`, `--json` | Output as JSON for programmatic use; `exec --file` streams JSON Lines |
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

### `kernelspec inspect-file`

Inspect kernel metadata declared by a py:percent file or notebook.

```bash
j-cli -j kernelspec inspect-file analysis.py
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

Install Claude Code hooks (`PreToolUse` and `PostToolUse`) that intercept notebook-execution bypass tools and keep `.py` / `.ipynb` pairs in sync, redirecting Claude to use j-cli instead.

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

### `setup codex`

Install Codex hooks (`PreToolUse` and `PostToolUse`) that intercept notebook-execution bypass tools and keep `.py` / `.ipynb` pairs in sync, redirecting Codex to use j-cli instead.

```bash
j-cli setup codex             # writes .codex/hooks.json (default)
j-cli setup codex --project   # same as default
j-cli setup codex --user      # writes ~/.codex/hooks.json (global, all projects)

# remove all j-cli managed hooks from the target file
j-cli setup codex --remove
j-cli setup codex --project --remove
```

**Prerequisites:** Codex hooks require `[features]\ncodex_hooks = true` in `.codex/config.toml`. `setup codex` checks for this and warns if missing. See [Codex hooks docs](https://developers.openai.com/codex/hooks).

The install command is idempotent — re-running updates hooks in place without duplicating them. `--remove` prunes only j-cli managed entries, preserving any unrelated user hooks.

**What gets installed (4 hooks):**

| Hook | Event | Trigger | Action |
|------|-------|---------|--------|
| `notebook-exec-guard` | PreToolUse (Bash) | `jupyter nbconvert --execute`, `papermill`, `runipy`, `ipython <.ipynb>` | Hard deny, redirect to j-cli |
| `python-run-guard` | PreToolUse (Bash) | Bash command targeting a `.py` with a paired `.ipynb` | Soft deny, suggest j-cli session |
| `pair-drift-guard-pre` | PreToolUse (apply_patch) | `apply_patch` touching a paired `.py` / `.ipynb` | Detect drift, auto-merge, deny stale edits |
| `pair-drift-guard-post` | PostToolUse (apply_patch) | After `apply_patch` completes | Auto-sync the other side of the pair |

> `notebook-edit-guard` is not installed for Codex — Codex has no `NotebookEdit` tool; file edits go through `apply_patch` instead.

### `setup opencode`

Install a self-contained OpenCode plugin that intercepts notebook-execution bypass tools and keeps `.py` / `.ipynb` pairs in sync. OpenCode loads JavaScript files from its plugin directories at startup.

```bash
j-cli setup opencode             # writes .opencode/plugins/jcli.js (default)
j-cli setup opencode --project   # same as default
j-cli setup opencode --user      # writes ~/.config/opencode/plugins/jcli.js

# remove the j-cli managed plugin
j-cli setup opencode --remove
j-cli setup opencode --user --remove
```

The installer updates only files carrying the j-cli managed marker. It refuses to overwrite or remove an unrelated `jcli.js`. Avoid installing both project and user copies because OpenCode loads both plugin directories.

The plugin covers OpenCode's `bash`, `edit`, `write`, and `apply_patch` tools. It resolves `bash` paths against the tool's `workdir`, passes edits through the existing j-cli guards, converts deny decisions into tool errors, and appends post-edit sync notices to the tool output.

The plugin runs `j-cli` from `PATH`. Set `JCLI_BIN=/absolute/path/to/j-cli` before starting OpenCode when the executable is installed in another environment.

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

# display every top-level expression in inline code
j-cli exec <session_id> --code $'df.head()\ndf.describe()' --display-mode all

# execute from py:percent file
j-cli exec <session_id> --file analysis.py

# display every top-level expression instead of only the last one
j-cli exec <session_id> --file analysis.py --display-mode all

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

Inline code and file execution default to `--display-mode last_expr`, matching VS Code notebook behavior by displaying only the final expression. Use `--display-mode all` to display every top-level table or figure expression, or `--display-mode last_expr_or_assign` to also display a final assignment. For file execution, each selected cell runs sequentially; j-cli prints and writes back its outputs before starting the next cell. j-cli restores the kernel's previous display mode after execution.

**Execution timeout**: Without `--timeout`, j-cli gives each cell a 10-second deadline. An explicit `--timeout` sets one total budget for all selected cells. When the deadline expires during a cell, j-cli interrupts the remote execution, waits for the kernel to report `idle`, and returns `TIMEOUT`. The kernel process, session, and variables created before the interrupted cell remain available. If the interrupt request fails, j-cli returns `INTERRUPT_FAILED`; check `session list --no-vars` before deciding whether to interrupt or restart the kernel.

Human mode is intended for direct reading by people and agents. Use `--json` when a script needs structured output; `j-cli --json exec --file ...` streams JSON Lines, one object per completed cell plus a final summary object.

**Notebook writeback**: When executing from a py:percent file (one with `# %%` cell markers or a `# ---` front matter block), each completed cell's outputs are automatically written back to the paired `.ipynb`. If `analysis.ipynb` does not yet exist, j-cli creates it automatically before the first cell executes. Plain Python scripts without markers are executed normally without creating a notebook.

**Convert baseline refresh**: When `j-cli convert` syncs a canonical managed pair (`foo.py` ↔ `foo.ipynb`, or `foo.dummy.py` ↔ `foo.ipynb`) inside a git repo, it also refreshes the sticky pair baseline under `refs/jcli/pair-sync/*`. This lets later drift checks compare against the last successful pair sync instead of falling back to an older `HEAD`.

If you convert to a non-canonical output path such as `foo.py -> custom.ipynb` or `nb.ipynb -> custom.py`, j-cli treats that as an export/conversion only and does **not** refresh the sticky baseline.

## Troubleshooting Hooks

If a hook appears to run but produces no visible effect (silent `exit 0` with no
sync, no deny message), enable the per-hook debug log to capture stdin/stdout/stderr.

Edit `.claude/settings.local.json` and append ` --debug` to the hook command you
want to inspect, e.g.:

    "command": "j-cli _hooks pair-drift-guard-post --debug"

Trigger the hook, then inspect the log:

    ls /tmp/jcli-$UID/
    cat /tmp/jcli-$UID/pair-drift-guard-post-*.log | jq .

Each invocation writes one JSON file containing the incoming payload, outgoing
decision (if any), stderr, exit code, and any exception. Remove `--debug` when
done — log files accumulate in `/tmp` and are not rotated.

Override the log directory with `JCLI_DEBUG_LOG_DIR=/path/to/dir` if `/tmp` is
not writable or you want the logs elsewhere.

For OpenCode, inspect the OpenCode application log for entries with service
`j-cli`. The plugin logs subprocess startup failures, non-zero exits, stderr,
and malformed guard output while allowing the tool call to continue.

If `refs/jcli/pair-sync/*` accumulates over time, clean stale entries with:

    j-cli _hooks gc-pair-sync-refs
    j-cli _hooks gc-pair-sync-refs --dry-run

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
