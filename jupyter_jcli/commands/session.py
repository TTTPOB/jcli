"""jcli session — session management."""

from __future__ import annotations

import concurrent.futures
from enum import Enum

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import Context, pass_ctx
from jupyter_jcli.output import emit, emit_error


class KernelState(str, Enum):
    """Jupyter kernel execution state.

    Values come from the Jupyter server REST API kernel status field.
    Unknown values from the server are normalised to UNKNOWN by
    _coerce_state() to avoid crashing on new states added by future
    Jupyter versions.
    Source: https://jupyter-server.readthedocs.io/en/latest/operators/public-api.html
    """
    IDLE = "idle"
    BUSY = "busy"
    STARTING = "starting"
    DEAD = "dead"
    UNKNOWN = "unknown"


def _coerce_state(raw: str) -> KernelState:
    """Coerce a raw kernel state string to KernelState, falling back to UNKNOWN."""
    try:
        return KernelState(raw)
    except ValueError:
        return KernelState.UNKNOWN

# Max sessions before we skip auto-var-fetch unless --vars is forced
_AUTO_FETCH_LIMIT = 10
# Timeout per kernel when fetching vars in session list
_LIST_VARS_TIMEOUT = 2.0
# Number of variable names to show inline
_PREVIEW_N = 5


@click.group()
def session():
    """Manage Jupyter sessions."""


@session.command("create")
@click.option("--kernel", "-k", required=True, help="Kernel spec name")
@click.option("--name", "-n", default=None, help="Session name")
@pass_ctx
def create(ctx: Context, kernel: str, name: str | None):
    """Create a new session with the given kernel."""
    try:
        from jupyter_jcli.server import create_session

        info = create_session(ctx.server_url, kernel, name, ctx.token)
        emit(
            {
                **info,
                "_human": f"Created session {info['session_id']} (kernel: {info['kernel_id']}, spec: {info['kernel_name']})",
            },
            use_json=ctx.use_json,
        )
    except Exception as e:
        emit_error("SESSION_CREATE_FAILED", str(e), ctx.use_json)


@session.command("list")
@click.option(
    "--no-vars", "skip_vars", is_flag=True, default=False,
    help="Skip variable preview fetch (faster, matches pre-vars behaviour).",
)
@click.option(
    "--vars", "force_vars", is_flag=True, default=False,
    help="Force variable preview even when there are more than 10 sessions.",
)
@pass_ctx
def list_sessions(ctx: Context, skip_vars: bool, force_vars: bool):
    """List active sessions.

    By default, a short variable preview is fetched for each idle kernel and
    appended as a VARS column.  Pass --no-vars to skip this (faster).

    Variable names are shown in first-definition order (not modification order).
    Run 'j-cli vars <SESSION_ID>' for the full variable list.
    """
    try:
        from jupyter_jcli.server import list_sessions

        sessions = list_sessions(ctx.server_url, ctx.token)

        # Decide whether to fetch vars
        fetch_vars = not skip_vars
        if fetch_vars and len(sessions) > _AUTO_FETCH_LIMIT and not force_vars:
            fetch_vars = False
            _warn_skipped = True
        else:
            _warn_skipped = False

        # Enrich with variable previews
        if fetch_vars and sessions:
            _enrich_with_vars(ctx, sessions)

        if ctx.use_json:
            payload: dict = {"sessions": sessions}
            if _warn_skipped:
                payload["vars_skipped"] = True
                payload["vars_skip_reason"] = (
                    f"More than {_AUTO_FETCH_LIMIT} sessions; "
                    "use --vars to force fetching."
                )
            emit(payload, use_json=True)
        else:
            if not sessions:
                emit({"_human": "No active sessions"}, use_json=False)
                return

            if fetch_vars:
                header = (
                    f"{'SESSION_ID':<40} {'KERNEL':<20} {'STATE':<10} "
                    f"{'NAME':<20} {'VARS'}"
                )
                lines = [header]
                for s in sessions:
                    preview = s.get("vars_preview", {})
                    vars_col = _format_vars_preview(preview)
                    lines.append(
                        f"{s['session_id']:<40} {s['kernel_name']:<20} "
                        f"{s['kernel_state']:<10} {s['name']:<20} {vars_col}"
                    )
                lines.append("")
                lines.append(
                    "hint: run 'j-cli vars <SESSION_ID>' for full variable list"
                )
                if _warn_skipped:
                    lines.append(
                        f"note: variable preview skipped (>{_AUTO_FETCH_LIMIT} sessions); "
                        "use --vars to force"
                    )
            else:
                header = (
                    f"{'SESSION_ID':<40} {'KERNEL_ID':<40} "
                    f"{'KERNEL':<20} {'STATE':<10} {'NAME':<20}"
                )
                lines = [header]
                for s in sessions:
                    lines.append(
                        f"{s['session_id']:<40} {s['kernel_id']:<40} "
                        f"{s['kernel_name']:<20} {s['kernel_state']:<10} {s['name']:<20}"
                    )

            emit({"_human": "\n".join(lines)}, use_json=False)

    except Exception as e:
        emit_error("CONNECTION_FAILED", str(e), ctx.use_json)


def _enrich_with_vars(ctx: Context, sessions: list[dict]) -> None:
    """Fan out variable fetches in parallel and attach vars_preview to each session dict."""
    from jupyter_jcli.kernel import kernel_connection
    from jupyter_jcli.variables import VariablesUnavailable, list_variables

    eligible = [
        s for s in sessions
        if _coerce_state(s.get("kernel_state", "")) not in (
            KernelState.BUSY, KernelState.DEAD, KernelState.UNKNOWN
        )
    ]

    def _fetch(s: dict) -> tuple[str, dict]:
        sid = s["session_id"]
        kid = s["kernel_id"]
        try:
            with kernel_connection(ctx.server_url, ctx.token, kid) as kernel:
                result = list_variables(kernel, timeout=_LIST_VARS_TIMEOUT)
            variables = result["variables"]
            names = [v["name"] for v in variables]
            return sid, {"names": names[:_PREVIEW_N], "total": len(names)}
        except Exception:
            return sid, {"names": [], "total": -1, "unavailable": True}

    previews: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch, s): s["session_id"] for s in eligible}
        for future in concurrent.futures.as_completed(futures):
            try:
                sid, preview = future.result()
                previews[sid] = preview
            except Exception:
                sid = futures[future]
                previews[sid] = {"names": [], "total": -1, "unavailable": True}

    for s in sessions:
        sid = s["session_id"]
        if sid in previews:
            s["vars_preview"] = previews[sid]
        elif _coerce_state(s.get("kernel_state", "")) in (
            KernelState.BUSY, KernelState.DEAD, KernelState.UNKNOWN
        ):
            s["vars_preview"] = {"names": [], "total": -1, "unavailable": True}
        else:
            s["vars_preview"] = {"names": [], "total": 0}


def _format_vars_preview(preview: dict) -> str:
    if not preview or preview.get("unavailable"):
        return "<unavailable>"
    names = preview.get("names", [])
    total = preview.get("total", 0)
    if total == 0 and not names:
        return "(none)"
    text = ", ".join(names)
    extra = total - len(names)
    if extra > 0:
        text += f" … (+{extra} more)"
    return text


@session.command("kill")
@click.argument("session_id")
@pass_ctx
def kill(ctx: Context, session_id: str):
    """Kill (delete) a session."""
    try:
        from jupyter_jcli.server import delete_session

        delete_session(ctx.server_url, session_id, ctx.token)
        emit(
            {"status": ResponseStatus.OK, "_human": f"Killed session {session_id}"},
            use_json=ctx.use_json,
        )
    except Exception as e:
        emit_error("SESSION_NOT_FOUND", str(e), ctx.use_json)
