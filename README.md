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

Note: the PyPI package name is `jupyter-jcli`, while the installed binary is `j-cli`. MCP support for Claude Code and Codex notebook-output integration is included in the default installation.

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
bash -c "$(j-cli serve-cmd --serve-backend lab)"
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
j-cli setup dsh
# or
j-cli setup opencode
```

Install the git `pre-commit` hook to keep `.py` / `.ipynb` pairs in sync:

```bash
j-cli setup git
```

After a post-edit pair sync with a git baseline, the agent hook appends a cell
summary to its context. `~` marks edited cells, `+` marks inserted cells, and
`- old:N` records deleted baseline cells at their current insertion point.

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
j-cli session kill <session_selector>
```

JSON output from `session create` and `session list` includes both the stable full
`session_id` and the shortest unique `session_selector` accepted by subsequent
commands. Human output uses the same short selector.

### `kernel`

```bash
j-cli kernel interrupt <session_selector>
j-cli kernel restart <session_selector>
```

### `notebook summary`, `notebook show`, and `notebook map`

Inspect notebook cells without executing them. `summary` shows the complete source
for short cells. For longer Python cells, it extracts imports, definitions, writes,
and calls and includes a source preview. `show` prints complete source for a cell
or range. `map` returns the cell alignment for a paired `.py` / `.ipynb`,
including stable IDs, Python source line ranges, pair changes, and each side's
change relative to the latest pair baseline.

```bash
j-cli notebook summary analysis.py
j-cli notebook show analysis.py --cell 4
j-cli notebook show analysis.py --cell 3:7
j-cli -j notebook summary analysis.ipynb
j-cli -j notebook map analysis.py
```

`map` reports `alignment` as `id`, `content`, `position`, or `null` for an
unpaired cell. `change`, `python_change`, and `notebook_change` use `equal`,
`edited`, `inserted`, and `deleted`; baseline changes are `null` when no git or
sticky pair baseline exists.

Cell specs are 0-indexed and use the same half-open range syntax as `exec`.

### Saved notebook outputs

Read output already saved in an `.ipynb` without executing a kernel:

```bash
# list physical outputs for cell 4
j-cli -j notebook outputs analysis.ipynb --cell 4

# read output 1, or request one exact MIME representation
j-cli -j notebook output analysis.ipynb --cell 4 --output 1
j-cli -j notebook output analysis.ipynb --cell 4 --output 1 --mime text/html

# a py:percent path maps to its paired notebook only when the match is reliable
j-cli -j notebook output analysis.py --cell 4 --output 1
```

Cell and output indexes are physical, zero-based indexes in the supplied file and saved notebook. For `.py`, j-cli accepts only a unique stable cell ID or a unique non-conflicting `(cell type, source)` match; it never guesses by position or similarity. File-execution JSON keeps `cell_index` as the executed source-file index and reports `notebook_cell_index` when the actual saved `.ipynb` position differs.

These commands read saved state, which may be older than the current source. They do not execute, synchronize pairs, write baselines, create caches, or start a background process. HTML and SVG are returned as original source text, JSON remains structured, and only existing raster MIME data is returned as an image.

### Persisted inline outputs

Inline code and plain Python files have no notebook writeback target. Short text-only results remain inline and do not create `.j-cli`. Rich output or more than 4,000 text characters is saved below the command's real current working directory:

```text
<cwd>/.j-cli/outputs/<run-id>/manifest.json
```

The command reports this absolute path as `output_manifest`. Read it later with:

```bash
j-cli -j output show /absolute/path/to/manifest.json
j-cli -j output show /absolute/path/to/manifest.json --output 0
j-cli -j output show /absolute/path/to/manifest.json --output 0 --mime text/html
```

Persisted output is working data, not a permanent archive. A new persisted run triggers opportunistic cleanup with a 7-day retention limit and a newest-50-run limit; exceeding either makes a complete managed run eligible. There is no cleanup daemon. Preview or override cleanup from the workspace whose `.j-cli` directory should be managed:

```bash
j-cli output clean --dry-run
j-cli output clean --days 7 --max-runs 50

# equivalent defaults/overrides for automatic and manual cleanup
export JCLI_OUTPUT_RETENTION_DAYS=7
export JCLI_OUTPUT_MAX_RUNS=50
```

Cleanup retains unrecognized or uncertain entries. `j-cli setup git` adds `**/.j-cli/` to its managed `.gitignore` block. See [output migration](docs/output-migration.md) for the change from temporary image paths and [output protocol](docs/output-protocol.md) for MIME, paging, and transport details.

### Agent host setup components

`setup claude`, `setup codex`, `setup dsh`, and `setup opencode` install all three components by default: the bundled `j-cli` skill, notebook guards (`hook`), and the notebook-output integration (`tool`). Every command defaults to local scope. Use repeatable `--only skill|hook|tool` for exact incremental operations; unspecified components are preserved, and `--remove` applies to the same selection (all three when `--only` is absent). Plugin and MCP details are implementation details of `tool`, not additional selectable components.

For hosts without a native local layer, local scope uses the normal project paths plus exact j-cli-managed entries in the closest config folder's `.gitignore`; it never ignores the whole host folder or changes user ignore lines. Switching a selected component to `--project` removes its managed ignore block. If a selected target is already Git-tracked, local setup fails before writing and tells you to untrack it explicitly; it never runs `git rm`.

Skills use the host's discovery paths, preferring the shared `.agents/skills/j-cli` project location when supported. Removing a skill at a shared path affects every host that discovers that path. Override only the skill root with `--skill-dir PATH` (the target becomes `PATH/j-cli`); this does not force-overwrite conflicts and does not relocate hook/tool configuration.

| Host | Project/local skill directory | User skill directory |
|---|---|---|
| Claude | `.claude/skills/j-cli` | `~/.claude/skills/j-cli` |
| Codex | `.agents/skills/j-cli` | `$CODEX_HOME/skills/j-cli` (default `~/.codex/skills/j-cli`) |
| DSH | `.agents/skills/j-cli` | `$DSH_AGENTS_HOME/skills/j-cli` (default `~/.agents/skills/j-cli`) |
| OpenCode | `.agents/skills/j-cli` | `~/.agents/skills/j-cli` |

The skill is bundled with the Python package and installs offline without Node, an external skill installer, or a source checkout. After upgrading j-cli, rerun `setup <host> --only skill` with the same scope and optional `--skill-dir` to update the installed copy. Without `--force`, existing unmanaged directories, symbolic links left by other installers, and modified managed files are not overwritten. Normal uninstall preserves extra user files.

Project/local installation checks the selected components in the host's known global discovery/configuration locations and warns if a matching global integration exists. It still installs at the requested scope, never modifies the global copy, and leaves loading precedence to the host. The check includes alternate supported skill directories, not just j-cli's preferred installation path; for example, [OpenCode discovers global skills](https://opencode.ai/docs/skills/#place-files) in `.agents`, `.claude`, and its own config directory. Warnings go to stderr so JSON stdout remains parseable.

Use `--force` to take over conflicting targets for the selected components:

```bash
j-cli setup dsh --force                    # replace selected targets at local scope
j-cli setup claude --only tool --force     # take over the named MCP entry only
j-cli setup dsh --only skill --force       # replace the complete target j-cli skill
j-cli setup dsh --user --only skill --force # explicitly target the global skill
```

**Back up local changes first.** Forced skill installation replaces the entire target `j-cli/` directory, including extra files. If that target is a symbolic link, it replaces the link itself without modifying the linked source. For hooks, MCP and plugins, force remains confined to j-cli's hook entries, the `jcli-notebook-output` server, or the dedicated `jcli.js`/`jcli.ts` plugin and its corresponding configuration row; unrelated user configuration is preserved. It does not bypass invalid configuration or the local-scope Git tracking check, and cannot be combined with `--remove`. Finding a global copy never expands the force operation to that copy.

DSH and OpenCode share one plugin file between `hook` and `tool`. Selecting one capability does not remove the other, including its local ignore rules. To make the entire plugin project-shared, select both with `--project --only hook --only tool` (or use `--project` for all components). User-written ignore rules are never removed; check them if project files remain ignored.

### `setup claude`

Install the bundled skill, Claude Code native hooks, and the `jcli-notebook-output` MCP tool.

```bash
j-cli setup claude                         # local skill + hook + tool (default)
j-cli setup claude --project               # shared project components
j-cli setup claude --user                  # user-global components
j-cli setup claude --only skill            # no Claude CLI or MCP prerequisite
j-cli setup claude --only hook --only tool # exact two-component update
j-cli setup claude --remove --only tool    # preserve skill and hooks
j-cli setup claude --only skill --skill-dir ./agent-skills
```

The install command is idempotent — re-running updates hooks in place without duplicating them. It uses the official `claude mcp add` command and rejects an existing `jcli-notebook-output` entry pointing to another command unless installation explicitly uses `--force`. The server exposes one read-only call, `read_notebook_output`, which lists a cell's saved outputs when `output_index` is omitted and reads one output when it is provided. Project and local installs explicitly allow the current project root; user installs defer root discovery to the MCP client's roots capability instead of binding the setup directory. A user-scoped server returns `ROOTS_REQUIRED` if the client provides no roots. `--remove` removes the selected managed components (skill, hooks, and the matching MCP entry by default), preserving unrelated user configuration and notebook output data. If the hook settings file becomes empty after removal it is deleted.

The notebook-output server is included in the default `jupyter-jcli` installation; no extra is needed.

**What gets installed (5 hooks):**

| Hook | Event | Trigger | Action |
|------|-------|---------|--------|
| `notebook-exec-guard` | PreToolUse (Bash) | `jupyter nbconvert --execute`, `papermill`, `runipy`, `ipython <.ipynb>` | Hard deny, redirect to j-cli |
| `python-run-guard` | PreToolUse (Bash) | Shell command targeting a `.py` with a paired `.ipynb` | Soft deny, suggest a j-cli session |
| `pair-drift-guard` | PreToolUse (Edit/Write) | Edit targeting a paired `.py` / `.ipynb` | Detect existing drift, auto-merge, deny stale edits |
| `pair-drift-guard-post` | PostToolUse (Edit/Write) | After an edit completes | Auto-sync the other side of the pair |
| `notebook-edit-guard` | PreToolUse (NotebookEdit) | Direct notebook edit | Hard deny, require the py:percent workflow |

### `setup git`

Install a `pre-commit` hook shim that runs `j-cli _hooks pre-commit-pair-sync` and update `.gitignore` to exclude paired `.ipynb` files and workspace-local `**/.j-cli/` output data.

```bash
j-cli setup git              # default: .githooks/pre-commit + set core.hooksPath
j-cli setup git --local      # .git/hooks/pre-commit (this clone only)
j-cli setup git --include "src/*.py"  # only sync matching files

# remove the managed hook and gitignore block
j-cli setup git --remove
j-cli setup git --local --remove
```

`--remove` deletes the hook only if it was written by j-cli, leaves `core.hooksPath` alone if it points to a non-j-cli directory, and removes the managed `.gitignore` block. Unrecognised hooks are skipped with a warning.

The project installer writes a shim at `.githooks/pre-commit`, sets the local
`core.hooksPath`, and adds a managed `.gitignore` block for `*.ipynb` and
`**/.j-cli/`. The local installer writes `.git/hooks/pre-commit` without
changing `core.hooksPath`. Re-running either form updates its managed content
without duplication.

| Commit-time situation | Result |
|-----------------------|--------|
| `.ipynb` staged | Blocked; unstage it and commit only the `.py` pair |
| Pair in sync | Silently allowed |
| One side changed and auto-merge succeeds | Merge both sides and re-stage an updated `.py` |
| Both sides changed the same cell | Block with conflict markers for manual resolution |
| New `.py` has no baseline and differs from its pair | Block with a two-way diff until one side is selected |

Resolve a conflict by explicitly choosing the source of truth:

```bash
j-cli convert ipynb-to-py <nb.ipynb> <nb.py>   # take ipynb as truth
j-cli convert py-to-ipynb <nb.py> <nb.ipynb>   # take py as truth
```

### `setup codex`

Install the shared `.agents` skill, Codex native hooks, and the `jcli-notebook-output` MCP tool.

```bash
j-cli setup codex                       # local all; exact files are gitignored
j-cli setup codex --project             # shared project files
j-cli setup codex --user                # $CODEX_HOME (default ~/.codex)
j-cli setup codex --only skill          # project .agents/skills/j-cli
j-cli setup codex --remove --only hook  # preserve skill and MCP tool
```

**Prerequisites:** Codex hooks require `[features]\ncodex_hooks = true` in `.codex/config.toml`. `setup codex` checks for this and warns if missing. See [Codex hooks docs](https://developers.openai.com/codex/hooks).

The install command is idempotent — re-running updates hooks in place without duplicating them. Codex's own MCP CLI writes the selected `config.toml`, preserving unrelated TOML content; an existing `jcli-notebook-output` entry with another command is rejected unless installation explicitly uses `--force`. The server exposes the same single `read_notebook_output` call as Claude setup. Project installs pass the current project as an explicit allowed root, while user installs defer to client-provided MCP roots and return `ROOTS_REQUIRED` if none are available. `--remove` removes the selected managed components (skill, hooks, and the matching MCP entry by default), preserving unrelated configuration and notebook output data. MCP support is included in the default `jupyter-jcli` installation.

**What gets installed (4 hooks):**

| Hook | Event | Trigger | Action |
|------|-------|---------|--------|
| `notebook-exec-guard` | PreToolUse (Bash) | `jupyter nbconvert --execute`, `papermill`, `runipy`, `ipython <.ipynb>` | Hard deny, redirect to j-cli |
| `python-run-guard` | PreToolUse (Bash) | Bash command targeting a `.py` with a paired `.ipynb` | Soft deny, suggest j-cli session |
| `pair-drift-guard-pre` | PreToolUse (apply_patch) | `apply_patch` touching a paired `.py` / `.ipynb` | Detect drift, auto-merge, deny stale edits |
| `pair-drift-guard-post` | PostToolUse (apply_patch) | After `apply_patch` completes | Auto-sync the other side of the pair |

> `notebook-edit-guard` is not installed for Codex — Codex has no `NotebookEdit` tool; file edits go through `apply_patch` instead.

### `setup dsh`

Install the self-contained native TypeScript adapter for DeepSeek Harness (DSH).
The adapter is packaged inside `jupyter-jcli`, has no official DSH hook-bridge
dependency, and invokes the installed `j-cli` guards through DSH's native shell
injection. It does not require `tsc`, `tsx`, npm, or a separate Node project.
Workspace installation still requires `dsh-workspace-overlay` to mount
`.dsh/cordis.yml`.

```bash
j-cli setup dsh                       # local skill + hook + tool
j-cli setup dsh --project             # shared workspace files
j-cli setup dsh --proj                # alias for --project
j-cli setup dsh --global              # alias for --user
j-cli setup dsh --only hook           # enable guards independently
j-cli setup dsh --remove --only tool  # keep plugin while hooks remain
```

| Scope | Flags | Skill | Cordis file | Native adapter |
|---|---|---|---|---|
| Workspace local | default, `--local` | `<cwd>/.agents/skills/j-cli` | `<cwd>/.dsh/cordis.yml` | `<cwd>/.dsh/plugins/jcli.ts` |
| Workspace shared | `--project`, `--proj` | `<cwd>/.agents/skills/j-cli` | same workspace paths | same workspace path |
| User | `--global`, `--user` | `${DSH_AGENTS_HOME:-~/.agents}/skills/j-cli` | `$DSH_HOME/cordis.patch.yml` | `$DSH_HOME/plugins/jcli.ts` |

`$DSH_HOME` defaults to `~/.dsh`; `$DSH_AGENTS_HOME` defaults to `~/.agents`.
Workspace paths use the canonical current working directory. The workspace module name is deliberately relative to the
`.dsh` composition file, so the whole workspace can be moved together. Global
and workspace scopes always use separate adapter paths.
The installer validates the packaged resource, every existing YAML/JSON input,
and TS ownership before writing. Adapter replacement is atomic and an existing
TS file without the j-cli managed header is overwritten only with `--force`; removal still requires ownership.
Re-running is idempotent; YAML comments, `!!js` tags, explicit document markers,
empty sequences, unrelated rows, and unrelated legacy settings are preserved.
When both scopes are present, setup warns because DSH could run both adapters.
Ensure the DSH runtime uses a Node release that supports direct TypeScript
modules (Node 24.19 is the tested runtime), and ensure its `PATH` resolves the
updated `j-cli` installation. No compiler or package manager is needed at
runtime. The native adapter also exposes the single read-only
`read_notebook_output` tool with the same list-or-read arguments as the
Claude/Codex MCP integration; it invokes the installed `j-cli` and needs no
separate MCP dependency installation.

Re-running `setup dsh` migrates a managed legacy bridge row in place to the
native row. An old `.dsh/jcli-hooks.json` (or the global file with the same name)
is cleaned only of j-cli-managed entries; user entries remain in place. If custom
entries remain,
setup warns that the native adapter does not execute those legacy hooks and they
must be configured separately. `--remove` also understands this old layout,
never deletes user configuration, and does not delete notebooks or saved output
data. See [hook exit codes](docs/hook-exit-codes.md):
`0` means allow/success, `2` means an explicit pre-hook refusal, and `1` means
parse, I/O, Git, synchronization, or baseline failure. Post-hook failures keep
the tool result and add a diagnostic.

### `setup opencode`

Install the shared `.agents` skill and a self-contained OpenCode plugin. The plugin can enable guards and the notebook-output tool independently.

```bash
j-cli setup opencode                       # local skill + hook + tool
j-cli setup opencode --project             # shared project components
j-cli setup opencode --user                # user-global components
j-cli setup opencode --only tool           # output tool without guards
j-cli setup opencode --remove --only hook  # preserve tool and skill
```

The installer normally updates only files carrying the j-cli managed marker. `--force` allows replacement of an unrelated `jcli.js` with the selected capabilities, but removal still requires ownership. Avoid installing both project and user copies because OpenCode loads both plugin directories.

The plugin covers OpenCode's `bash`, `edit`, `write`, and `apply_patch` tools. It resolves `bash` paths against the tool's `workdir`, passes edits through the existing j-cli guards, converts deny decisions into tool errors, and appends post-edit sync notices to the tool output. It also exposes the single read-only `read_notebook_output` tool, using OpenCode's normal read permission check and the shared list-or-read contract; it needs no separate MCP dependency installation.

The plugin runs `j-cli` from `PATH`. Set `JCLI_BIN=/absolute/path/to/j-cli` before starting OpenCode when the executable is installed in another environment. Removing the plugin removes managed integration files only; it does not delete notebooks or saved output data.

The internal `_hooks --platform` option selects the **hook input format**, not
all supported integrations. It accepts `claude` (default), `codex`, and `dsh`;
unknown values are rejected before reading the payload. OpenCode converts
`bash`, `edit`, and `write` events to the default Claude format and
`apply_patch` events to the Codex format, so it needs no separate `opencode` value.

`notebook-edit-guard` accepts all three input formats and checks `tool_name`
for `NotebookEdit` in each. Only Claude setup installs this guard. Codex, DSH,
and OpenCode integrations protect notebook file edits through
`pair-drift-guard-pre` instead.

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
j-cli vars <session_selector>

# inspect a single variable
j-cli vars <session_selector> --name x

# rich inspection (MIME-typed data, DAP kernels only)
j-cli vars <session_selector> --name x --rich

# JSON output for programmatic use
j-cli -j vars <session_selector>
j-cli -j vars <session_selector> --name x
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

Human output shows the shortest unique session ID prefix, with at least three characters. Commands accept a session selector: a full ID, the displayed short ID, or an exact unique session name. If a selector matches multiple sessions, the command exits without choosing one. A hint line at the bottom points at `j-cli vars <SESSION_SELECTOR>` for the full variable list.

In JSON mode (`-j`), each session object includes the shortest unique
`session_selector` and gains a `vars_preview` key:
```json
{"session_id": "...", "session_selector": "abc", "vars_preview": {"names": ["x", "df"], "total": 2}}
```

### `exec`

Execute code in a kernel session. Supports inline code, py:percent files, and Jupyter notebooks.

```bash
# inline code
j-cli exec <session_selector> --code "import pandas as pd; df = pd.read_csv('data.csv'); df.head()"

# display every top-level expression in inline code
j-cli exec <session_selector> --code $'df.head()\ndf.describe()' --display-mode all

# execute from py:percent file
j-cli exec <session_selector> --file analysis.py

# display every top-level expression instead of only the last one
j-cli exec <session_selector> --file analysis.py --display-mode all

# execute specific cells from a notebook
j-cli exec <session_selector> --file notebook.ipynb --cell 0:3

# execute a single cell
j-cli exec <session_selector> --file notebook.ipynb --cell 5
```

**Cell spec formats** (0-indexed):

| Spec | Meaning |
|------|---------|
| `3` | Cell 3 only |
| `3:7` | Cells 3, 4, 5, 6 |
| `3:` | Cell 3 to end |
| `:5` | Cells 0 through 4 |

Inline code and file execution default to `--display-mode last_expr`, matching VS Code notebook behavior by displaying only the final expression. Use `--display-mode all` to display every top-level table or figure expression, or `--display-mode last_expr_or_assign` to also display a final assignment. For file execution, each selected cell runs sequentially; j-cli prints and writes back its outputs before starting the next cell. If a cell fails, j-cli writes back its error output and stops before executing later cells. j-cli restores the kernel's previous display mode after execution.

**Execution timeout**: Without `--timeout`, j-cli gives each cell a 10-second deadline. An explicit `--timeout` sets one total budget for all selected cells. When the deadline expires during a cell, j-cli interrupts the remote execution, waits for the kernel to report `idle`, and returns `TIMEOUT`. The kernel process, session, and variables created before the interrupted cell remain available. If the interrupt request fails, j-cli returns `INTERRUPT_FAILED`; check `session list --no-vars` before deciding whether to interrupt or restart the kernel.

Human mode is intended for direct reading by people and agents. Use `--json` when a script needs structured output; `j-cli --json exec --file ...` streams one JSON object per completed cell to stdout. A successful run ends with a summary object. A failed run omits the summary and writes its structured error to stderr. Display summaries are bounded by a total budget and a per-entry limit; when entries are omitted, the response reports that omission. Treat the saved notebook or `output_manifest`, not the display summary, as the complete result.

**Notebook writeback**: When executing from a py:percent file (one with `# %%` cell markers or a `# ---` front matter block), each completed cell's outputs are automatically written back to the paired `.ipynb` before j-cli formats that cell's response. If later display formatting fails, the diagnostic states that the notebook output was already saved. If `analysis.ipynb` does not yet exist, j-cli creates it automatically before the first cell executes. Plain Python scripts without markers are executed normally without creating a notebook; their rich or oversized output uses `output_manifest` storage instead.

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

# %% id="imports"
import numpy as np

# %% id="summary"
x = np.random.randn(100)
print(x.mean())
```

j-cli stores nbformat cell IDs on markers as `id="..."`. Keep the ID when
editing an existing cell. Legacy markers without IDs remain supported; j-cli
uses content alignment for them.

Assign missing IDs in place before converting a mixed-ID file. When a paired
notebook exists, the command reuses its aligned cell IDs:

```bash
j-cli convert assign-ids analysis.py
```

j-cli comments IPython magic commands in py:percent files so Python tools can
parse them, then restores the commands when syncing to `.ipynb`. Python-body
cell magics such as `%%timeit` and `%%writefile` keep their body as Python code;
other cell magics are commented through the end of the cell.

## Development

```bash
# install with test dependencies
uv sync --extra test

# run tests (requires a real Jupyter server, started automatically by fixtures)
uv run pytest -n 4 --dist loadscope -v
```

## License

MIT
