"""Hook debug logger — captures stdin/stdout/stderr/exceptions to a JSON log file.

Usage:
    with HookDebugLogger("my-hook", enabled=debug_flag) as log:
        payload = read_hook_stdin(log)
        ...

When disabled (enabled=False), all operations are no-ops.
"""

from __future__ import annotations

import contextlib
import getpass
import io
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _log_dir() -> Path:
    """Return the directory for debug log files, honouring JCLI_DEBUG_LOG_DIR."""
    override = os.environ.get("JCLI_DEBUG_LOG_DIR", "")
    if override:
        return Path(override)
    try:
        uid = os.getuid()  # type: ignore[attr-defined]
        user_part = str(uid)
    except AttributeError:
        try:
            user_part = getpass.getuser()
        except Exception:  # noqa: BLE001
            user_part = "unknown"
    return Path("/tmp") / f"jcli-{user_part}"


def _ensure_log_dir(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(log_dir, 0o700)
    except OSError:
        pass


class _TeeStream(io.TextIOBase):
    """Write to both a StringIO buffer and the original stream."""

    def __init__(self, original: io.TextIOBase) -> None:
        self._orig = original
        self._buf = io.StringIO()

    def write(self, s: str) -> int:
        self._orig.write(s)
        self._orig.flush()
        return self._buf.write(s)

    def flush(self) -> None:
        self._orig.flush()

    def getvalue(self) -> str:
        return self._buf.getvalue()


class HookDebugLogger:
    """Context manager that captures hook I/O and writes a JSON log on exit.

    When enabled=False this is a complete no-op so there is zero overhead in
    production use.
    """

    def __init__(self, hook_name: str, enabled: bool) -> None:
        self._hook_name = hook_name
        self._enabled = enabled
        self._stdin_raw: str = ""
        self._stdin_parsed: Any = None
        self._stdout_raw: str = ""
        self._stdout_parsed: Any = None
        self._stderr_capture: str = ""
        self._exception: dict | None = None
        self._exit_code: int = 0
        self._start: float = 0.0
        self._tee: _TeeStream | None = None
        self._redirect_ctx: contextlib.AbstractContextManager | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "HookDebugLogger":
        if not self._enabled:
            return self
        self._start = time.monotonic()
        self._tee = _TeeStream(sys.stderr)  # type: ignore[arg-type]
        self._redirect_ctx = contextlib.redirect_stderr(self._tee)  # type: ignore[arg-type]
        self._redirect_ctx.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, tb) -> bool:
        if not self._enabled:
            return False

        if self._redirect_ctx is not None:
            self._redirect_ctx.__exit__(exc_type, exc_val, tb)
        if self._tee is not None:
            self._stderr_capture = self._tee.getvalue()

        if exc_type is not None:
            self.record_exception(exc_val)

        # Determine exit code: SystemExit carries it; others map to 1.
        if exc_type is SystemExit:
            try:
                self._exit_code = int(exc_val.code) if exc_val.code is not None else 0
            except (TypeError, ValueError):
                self._exit_code = 1
        elif exc_type is not None:
            self._exit_code = 1

        try:
            self._flush()
        except Exception:  # noqa: BLE001 — log write must never crash the hook
            pass

        # Do not suppress exceptions — let the hook's normal flow handle them.
        return False

    # ------------------------------------------------------------------
    # State setters
    # ------------------------------------------------------------------

    def set_stdin(self, raw: str, parsed: Any) -> None:
        if not self._enabled:
            return
        self._stdin_raw = raw
        self._stdin_parsed = parsed

    def set_stdout(self, raw: str, parsed: Any) -> None:
        if not self._enabled:
            return
        self._stdout_raw = raw
        self._stdout_parsed = parsed

    def record_exception(self, exc: BaseException) -> None:
        if not self._enabled:
            return
        self._exception = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }

    # ------------------------------------------------------------------
    # Internal flush
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        duration_ms = int((time.monotonic() - self._start) * 1000)
        ts = datetime.now(tz=timezone.utc).astimezone()
        ts_str = ts.strftime("%Y%m%dT%H%M%S-") + f"{ts.microsecond:06d}"

        log_dir = _log_dir()
        _ensure_log_dir(log_dir)
        log_path = log_dir / f"{self._hook_name}-{ts_str}.log"

        record = {
            "hook": self._hook_name,
            "pid": os.getpid(),
            "timestamp": ts.isoformat(),
            "duration_ms": duration_ms,
            "exit_code": self._exit_code,
            "stdin_raw": self._stdin_raw,
            "stdin_parsed": self._stdin_parsed,
            "stdout_raw": self._stdout_raw,
            "stdout_parsed": self._stdout_parsed,
            "stderr": self._stderr_capture,
            "exception": self._exception,
        }

        log_path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")


def read_hook_stdin(logger: HookDebugLogger) -> dict:
    """Read and parse stdin JSON, recording raw bytes in the logger first.

    Replaces ``json.load(sys.stdin)`` everywhere so that the logger captures
    the raw payload even when JSON parsing fails.
    """
    raw = sys.stdin.read()
    logger.set_stdin(raw, None)
    parsed = json.loads(raw)  # may raise; caller handles
    logger.set_stdin(raw, parsed)
    return parsed
