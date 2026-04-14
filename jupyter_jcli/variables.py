"""Kernel variable inspection via DAP debug_request with shell-channel fallback."""

from __future__ import annotations

import itertools
import typing as t
from enum import Enum


class VariableSource(str, Enum):
    """Which mechanism supplied the variable metadata.

    Used for human-readable display only; not dispatched on.
    """

    DAP = "dap"
    FALLBACK = "fallback"


class VariablesUnavailable(Exception):
    """Raised when no variable-inspection path is available for this kernel."""


# Counter for DAP sequence numbers (process-global, monotonically increasing)
_dap_seq = itertools.count(1)


def _supports_debugger(kernel) -> bool:
    """Return True if the kernel advertises debugger support in kernel_info."""
    try:
        info = kernel.kernel_info or {}
        return "debugger" in info.get("supported_features", [])
    except Exception:
        return False


def _dap_inspect_variables(wsc, *, timeout: float) -> list[dict]:
    """Send inspectVariables via DAP on the control channel.

    Args:
        wsc: The WSKernelClient (kernel._manager.client)
        timeout: Seconds to wait for the reply

    Returns:
        List of dicts with at least name/type/value/variablesReference keys.

    Raises:
        TimeoutError: if no reply arrives within timeout
        RuntimeError: if the DAP reply indicates an error
    """
    seq = next(_dap_seq)
    content = {
        "seq": seq,
        "type": "request",
        "command": "inspectVariables",
    }
    msg = wsc.session.msg("debug_request", content)
    msg_id = msg["header"]["msg_id"]
    wsc.control_channel.send(msg)
    reply = wsc._recv_reply(msg_id, channel="control", timeout=timeout)

    reply_content = reply.get("content", {})
    if not reply_content.get("success", False):
        message = reply_content.get("message", "DAP inspectVariables failed")
        raise RuntimeError(message)

    body = reply_content.get("body", {})
    return body.get("variables", [])


def _dap_rich_inspect_variable(wsc, name: str, *, timeout: float) -> dict:
    """Send richInspectVariables DAP request for a single named variable.

    Returns the body dict from the reply.
    """
    seq = next(_dap_seq)
    content = {
        "seq": seq,
        "type": "request",
        "command": "richInspectVariables",
        "arguments": {"variableName": name},
    }
    msg = wsc.session.msg("debug_request", content)
    msg_id = msg["header"]["msg_id"]
    wsc.control_channel.send(msg)
    reply = wsc._recv_reply(msg_id, channel="control", timeout=timeout)

    reply_content = reply.get("content", {})
    if not reply_content.get("success", False):
        message = reply_content.get("message", "DAP richInspectVariables failed")
        raise RuntimeError(message)

    return reply_content.get("body", {})


def _normalise_dap_variable(v: dict) -> dict:
    """Normalise a raw DAP variable dict into our canonical shape."""
    return {
        "name": str(v.get("name", "")),
        "type": str(v.get("type", "")),
        "value": str(v.get("value", "")),
        "variables_reference": v.get("variablesReference", 0),
    }


def _fallback_list_variables(kernel) -> list[dict]:
    """Use the shell-channel snippet (kernel.list_variables()) as fallback.

    Normalises VariableDescription objects into our canonical dict shape.
    """
    raw = kernel.list_variables()
    result = []
    for v in raw:
        # VariableDescription is a TypedDict-like object; access via dict or attr
        if isinstance(v, dict):
            result.append({
                "name": str(v.get("name", "")),
                "type": str(v.get("type", "")),
                "value": str(v.get("value", "")),
                "variables_reference": 0,
            })
        else:
            result.append({
                "name": str(getattr(v, "name", "")),
                "type": str(getattr(v, "type", "")),
                "value": str(getattr(v, "value", "")),
                "variables_reference": 0,
            })
    return result


def list_variables(kernel, *, timeout: float = 5.0) -> dict[str, t.Any]:
    """Return all kernel global variables.

    Tries the DAP inspectVariables path first (if the kernel advertises
    debugger support), then falls back to a shell-channel code snippet.

    Args:
        kernel: A started KernelClient instance.
        timeout: Per-request timeout in seconds.

    Returns:
        Dict with keys:
          - "variables": list of {"name", "type", "value", "variables_reference"}
          - "source": VariableSource (DAP or FALLBACK)

    Raises:
        VariablesUnavailable: if neither path succeeds.

    Notes:
        **Ordering**: Variables are returned in first-definition order (CPython
        dict insertion order). Re-assigning a variable does NOT move it to the
        end; only ``del x; x = ...`` does. Do not infer recency from position.

        **No mtime**: The Jupyter debug protocol does not expose per-variable
        last-modified timestamps. The returned dicts contain name/type/value
        only; no "mtime" or "last_execution_count" field exists in the protocol.
    """
    # Try DAP path
    if _supports_debugger(kernel):
        try:
            wsc = kernel._manager.client
            raw = _dap_inspect_variables(wsc, timeout=timeout)
            variables = [_normalise_dap_variable(v) for v in raw]
            return {"variables": variables, "source": VariableSource.DAP}
        except Exception:
            pass  # fall through to fallback

    # Shell-channel fallback
    try:
        variables = _fallback_list_variables(kernel)
        return {"variables": variables, "source": VariableSource.FALLBACK}
    except ValueError as e:
        raise VariablesUnavailable(str(e)) from e
    except Exception as e:
        raise VariablesUnavailable(f"Variable inspection failed: {e}") from e


def inspect_variable(
    kernel,
    name: str,
    *,
    rich: bool = False,
    timeout: float = 5.0,
) -> dict[str, t.Any]:
    """Inspect a single named variable.

    Args:
        kernel: A started KernelClient instance.
        name: Variable name to inspect.
        rich: If True and the kernel supports it, use richInspectVariables
              (returns MIME-typed data alongside the plain value).
        timeout: Per-request timeout in seconds.

    Returns:
        Dict with keys:
          - "name", "type", "value", "variables_reference"
          - "source": VariableSource (DAP or FALLBACK)
          - "data", "metadata" (only when rich=True and DAP succeeds)

    Raises:
        VariablesUnavailable: if inspection is not possible.
    """
    if _supports_debugger(kernel):
        try:
            wsc = kernel._manager.client

            if rich:
                body = _dap_rich_inspect_variable(wsc, name, timeout=timeout)
                # Also fetch plain variable list to get type/value
                raw_all = _dap_inspect_variables(wsc, timeout=timeout)
                match = next(
                    (v for v in raw_all if v.get("name") == name), {}
                )
                result = _normalise_dap_variable(match) if match else {
                    "name": name, "type": "", "value": "", "variables_reference": 0,
                }
                result["data"] = body.get("data", {})
                result["metadata"] = body.get("metadata", {})
                result["source"] = VariableSource.DAP
                return result
            else:
                raw_all = _dap_inspect_variables(wsc, timeout=timeout)
                match = next(
                    (v for v in raw_all if v.get("name") == name), None
                )
                if match is None:
                    raise VariablesUnavailable(
                        f"Variable '{name}' not found in kernel namespace"
                    )
                result = _normalise_dap_variable(match)
                result["source"] = VariableSource.DAP
                return result
        except VariablesUnavailable:
            raise
        except Exception:
            pass  # fall through

    # Fallback: list all and filter
    try:
        variables = _fallback_list_variables(kernel)
        match = next((v for v in variables if v["name"] == name), None)
        if match is None:
            raise VariablesUnavailable(
                f"Variable '{name}' not found in kernel namespace"
            )
        match["source"] = VariableSource.FALLBACK
        return match
    except VariablesUnavailable:
        raise
    except ValueError as e:
        raise VariablesUnavailable(str(e)) from e
    except Exception as e:
        raise VariablesUnavailable(f"Variable inspection failed: {e}") from e
