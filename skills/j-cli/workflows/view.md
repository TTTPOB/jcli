# View notebook source or saved results

Choose the branch matching the request. These operations do not require a live kernel; do not run healthcheck, create a session, or execute cells merely to inspect a file.

## Find and read source

1. Use `notebook summary` to locate relevant cells.
2. Use `notebook show` to read their complete source.
3. Stop after inspection unless editing or execution was requested.

Summaries use exactly one source field: `full_text` for cells up to 120
characters, otherwise a truncated `preview`. Longer Python cells also report
`imports`, `defines`, `writes`, and qualified `calls` when AST parsing succeeds.

```bash
j-cli notebook summary analysis.py
j-cli notebook show analysis.py --cell 4
j-cli notebook show analysis.py --cell 3:7
j-cli -j notebook summary analysis.ipynb
```

`show --cell` accepts the same 0-indexed specs as `exec`: `3`, `3:7`, `3:`, and
`:5`. Ranges are half-open; negative indices, descending ranges, and specs with
multiple colons are invalid. `show` prints code, markdown, and raw cells without
executing them.

## Search notebook content

Use `rg` with the `--pre` flag and the bundled preprocessor to search inside
`.ipynb` files. First resolve this skill's own directory from the loaded skill,
then use its absolute path; do not assume the current workspace is the j-cli
repository:

```bash
JCLI_SKILL_DIR=/absolute/path/to/the/installed/j-cli-skill

# Search all notebooks for a pattern
rg --pre "$JCLI_SKILL_DIR/scripts/rg_ipynb_preprocessor.py" 'pattern' .

# Search only .ipynb files
rg --pre "$JCLI_SKILL_DIR/scripts/rg_ipynb_preprocessor.py" -g '*.ipynb' 'pattern' .

# The preprocessor renders each notebook as plain text: cell sources and outputs
# Binary outputs (images, PDFs) are replaced with a size notice
```

The bundled preprocessor has no external dependencies.

## Read saved output

For notebook-backed execution, j-cli saves the output to `.ipynb` before formatting the response. Read it without executing again:

```bash
# omit --output to list physical outputs first
j-cli -j notebook outputs analysis.ipynb --cell 4
j-cli -j notebook output analysis.ipynb --cell 4 --output 1
j-cli -j notebook output analysis.ipynb --cell 4 --output 1 --mime text/html

# a py:percent path resolves to its paired notebook only through a reliable match
j-cli -j notebook output analysis.py --cell 4 --output 1
```

Indexes are zero-based and physical. File-execution JSON uses `cell_index` for the executed source-file position and `notebook_cell_index` for the actual saved notebook position when different. A `.py` cell maps only by a unique stable ID or unique, non-conflicting cell type and source; positional and similarity guesses fail.

Saved output may be older than the current source. Reading does not execute, synchronize pairs, write a baseline, create a cache, or start a daemon. HTML and SVG remain original source text, JSON remains structured, and only existing raster MIME representations are images.

Claude Code, Codex, DSH, and OpenCode expose the same single `read_notebook_output` call. Omit `output_index` to list and provide it to read, with optional `mime_type`, `offset`, and `limit`. Prefer this tool inside a configured host; use the CLI directly otherwise.

Inline code and plain `.py` files have no notebook target. Short text-only results stay inline and create no `.j-cli`; rich output or more than 4,000 text characters is stored under the real cwd and returned as an absolute `output_manifest`:

```bash
j-cli -j output show /absolute/path/to/manifest.json
j-cli -j output show /absolute/path/to/manifest.json --output 0
j-cli output clean --dry-run --days 7 --max-runs 50
```

The default cleanup limits are 7 days and the newest 50 runs. Override them with `JCLI_OUTPUT_RETENTION_DAYS` and `JCLI_OUTPUT_MAX_RUNS`. Cleanup is opportunistic or explicit, never a background daemon, and `.j-cli` is working data rather than permanent archival storage.

Do not rerun a cell just because a display summary is truncated. Read the saved notebook output or returned manifest instead. To inspect live kernel variables rather than saved results, load [session](session.md).
