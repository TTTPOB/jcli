# Hook Exit Codes

The internal `j-cli _hooks` commands use process status as part of their hook
contract. The status is independent from the optional JSON decision written to
stdout.

| Exit code | Meaning | Hook behavior |
| --- | --- | --- |
| `0` | Successful operation or normal no-op | No guard decision is required, or a pair was synchronized completely. |
| `1` | Operation failure or visible post-edit diagnostic | Malformed payload, shell/format parsing failure, file I/O failure, Git/subprocess failure, partial pair synchronization, or baseline persistence failure. |
| `2` | Explicit `PreToolUse` guard denial | The tool call must be blocked. The hook keeps its structured deny JSON on stdout and writes the deny reason to stderr. |

`pair-drift-guard-post` never uses `2`: its tool call has already completed.
A conflict, unresolved no-baseline drift, or failed synchronization returns `1`
and emits `additionalContext` when a context payload is available. Consumers
must preserve the completed tool result and append that diagnostic instead of
pretending that the edit was rolled back.

`pair-drift-guard-pre`, `notebook-exec-guard`, `python-run-guard`, and
`notebook-edit-guard` return `2` only after emitting an explicit deny decision.
Malformed or operationally failed pre hooks return `1`; consumers should expose
the stderr diagnostic rather than treating the call as an allow.

## OpenCode Adapter Behavior

OpenCode's `tool.execute.before` callback provides no supported non-blocking
context channel for a failed guard. The adapter therefore raises a clear tool
error for any non-zero pre-hook result: exit `2` is a policy denial, while exit
`1` is an operational guard failure surfaced as a tool error. The exit `1`
path is not relabeled as a policy denial. Post-hook non-zero results are handled
differently because the tool has already completed: the adapter appends the
stderr diagnostic (and any returned context) to the existing tool output while
preserving that result.

The Git pre-commit shim uses `exec`, so the status from
`j-cli _hooks pre-commit-pair-sync` is returned directly to Git. A directory
outside a Git repository is a normal `0` no-op, while a missing or failing Git
operation returns `1`. Pair drift hooks use a strict baseline lookup: Git's
explicit missing-ref, missing-HEAD, and no-history diagnostics remain normal
absence, while other Git/config failures return `1` instead of being treated as
an absent baseline. Pair baselines are advanced only after every required side
is synchronized and baseline persistence succeeds.

When `--debug` is enabled, the per-hook log records the same exit code along
with captured stdin, stdout, stderr, and any exception. Tests isolate both
`HOME` and `DSH_HOME` in temporary directories; hook execution must not write
user-global configuration.
