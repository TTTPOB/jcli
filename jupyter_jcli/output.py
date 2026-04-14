"""Output formatting: human-readable (default) or JSON."""

import json
import sys

import click

from jupyter_jcli._enums import ResponseStatus


def emit(data: dict, use_json: bool = False) -> None:
    """Print data as JSON or human-readable."""
    if use_json:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2))
        return
    # Human-readable: delegate to caller via data["_human"]
    if "_human" in data:
        click.echo(data["_human"])
    else:
        # Fallback to JSON if no human format provided
        click.echo(json.dumps(data, ensure_ascii=False, indent=2))


def emit_error(code: str, message: str, use_json: bool = False) -> None:
    """Print error and exit with code 1."""
    if use_json:
        click.echo(
            json.dumps({"status": ResponseStatus.ERROR, "code": code, "message": message}),
            err=True,
        )
    else:
        click.echo(f"ERROR [{code}]: {message}", err=True)
    sys.exit(1)
