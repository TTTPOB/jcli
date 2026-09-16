# Deploying j-cli

Read this file only when j-cli is missing, the Jupyter connection is not
configured, the server must be started, or the user explicitly asks to install
an agent integration.

## Install

Check for the command before installing it:

```bash
command -v j-cli > /dev/null && echo "installed" || echo "not installed"
```

Install the base CLI only when it is absent:

```bash
uv tool install jupyter-jcli
j-cli --version
```

The PyPI package is `jupyter-jcli`; the binary is `j-cli`. Claude Code and Codex
need the MCP extra to expose `read_notebook_output`:

```bash
uv tool install 'jupyter-jcli[mcp]'
```

## Connect

Use existing environment configuration when present:

```bash
[ -n "$JCLI_JUPYTER_SERVER_URL" ] && echo "URL: set" || echo "URL: unset"
[ -n "$JCLI_JUPYTER_SERVER_TOKEN" ] && echo "TOKEN: set" || echo "TOKEN: unset"
```

Ask the user only for missing values. Export them without printing the token:

```bash
export JCLI_JUPYTER_SERVER_URL=http://localhost:8888
export JCLI_JUPYTER_SERVER_TOKEN=<token>
```

Commands also accept `-s <url>` and `-t <token>`.

## Start Jupyter

First check whether the configured server is already reachable:

```bash
j-cli healthcheck
```

If it is not running and the user wants a local server, launch a detached
process, then run `j-cli healthcheck` again:

```bash
nohup bash -c "$(j-cli serve-cmd --serve-backend lab)" \
  > /tmp/jupyter_$(date +%Y%m%d_%H%M%S)_$$.log 2>&1 & disown
```

`--serve-backend` accepts `lab`, `server`, or `notebook`. `serve-cmd` keeps the
token as a literal `$JCLI_JUPYTER_SERVER_TOKEN` reference instead of inlining it.

## Install integrations

Install an integration only when the user explicitly requests setup. Do not
repeat setup during normal use.

```bash
j-cli setup claude
j-cli setup codex
j-cli setup dsh
j-cli setup opencode
j-cli setup git
```

Choose only the host the user is using. See the repository
[README](../../README.md#setup-claude) for scopes, generated files, removal,
prerequisites, and hook behavior.
