# ADR 0001: Lightweight entry points for pair guards

Status: Accepted

## Context

Pair guards protect edits to paired Python/notebook files. Most ordinary Python
edits have no notebook pair, yet starting the full CLI for both pre- and
post-edit hooks adds process and import costs to each edit.

The four integrations have two execution models: DSH and OpenCode run plugins
inside the host; Claude Code and Codex invoke command hooks.

## Decision

Use the same selection rules through two lightweight entry points:

- **DSH and OpenCode:** check paths inside the host before starting a guard
  subprocess. Setup inlines a shared JavaScript helper into each deployed plugin.
- **Claude Code and Codex:** invoke `j-cli-hook`, a Python standard-library-only
  entry point. When a guard is needed, load the existing CLI in the same process
  and replay the original input.

The Python runtime already required by j-cli is sufficient for command hooks.
No additional runtime or persistent service is required.

### Selection rules

A Python file pairs with the notebook obtained by replacing `.py` with `.ipynb`
in the same directory. The Python file itself may be new.

| Edited path | Before editing | After editing |
| --- | --- | --- |
| `.ipynb` | Run the guard to refuse direct editing | Skip |
| `.py` with a sibling `.ipynb` | Run the guard | Run the guard |
| `.py` without a sibling `.ipynb` | Skip | Skip |
| Other file | Skip | Skip |

Relative paths use the session/tool working directory in both the prefilter and
original guard. Patch selection examines every file directive, including move
destinations. If any path needs checking, the original guard receives the whole
patch.

Pre and post perform separate checks so a newly created pair is visible.
Only an explicit missing-file or not-a-directory result permits skipping a
Python path. Uncertain input, path resolution failures, and other filesystem
errors defer to the original guard.

The prefilter reads no Python or notebook contents and does not invoke Git.
Drift detection, synchronization, output, and error reporting remain in the
existing guard. Diagnostic options such as `--debug` use that guard directly.

## Validation

Shared Python/JavaScript cases check selection and path handling. Host tests
check guard process counts; fresh-process entry tests check that skipped calls
avoid importing the CLI, Click, and configuration. Integration tests cover
notebook refusal, multi-file patches, move targets, changing pair availability,
and preservation of hook output and exit codes.

A representative measurement used the same unpaired Python path, two warm-up
iterations, and twelve measured iterations per case. The native path reports
the median of complete pre/post callbacks; the command path sums the separately
measured pre- and post-hook medians:

| Execution path | Before | After |
| --- | ---: | ---: |
| Native plugin callbacks, including their shell launcher | 548.09 ms | 0.048 ms |
| Command hooks, including Python startup | 200.44 ms | 150.75 ms |

The native path started two guard processes before the change and none after.
Command hooks retained two interpreter starts and reduced elapsed time by about
25%. These measurements describe the tested launch paths, not a latency target;
interpreter, filesystem, and shell startup costs vary.

## Deployment

Package upgrades provide the new entry point. Re-run the relevant
`j-cli setup <platform> --only hook` command with the existing installation scope
to refresh copied plugins and managed hook commands, then reload the host's
integrations. Skills, output tools, and shell guards retain their existing roles.
