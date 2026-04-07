"""Configuration: environment variables and defaults."""

import os


def get_server_url(cli_value: str | None = None) -> str:
    """Resolve server URL. Priority: CLI flag > env var > default."""
    if cli_value:
        return cli_value
    return os.environ.get("JCLI_JUPYTER_SERVER_URL", "http://localhost:8888")


def get_token(cli_value: str | None = None) -> str | None:
    """Resolve auth token. Priority: CLI flag > env var > None."""
    if cli_value:
        return cli_value
    return os.environ.get("JCLI_JUPYTER_SERVER_TOKEN")
