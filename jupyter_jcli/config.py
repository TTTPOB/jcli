"""Application configuration resolved from environment variables and CLI flags."""

import getpass
import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_SERVER_URL = "http://localhost:8888"


def _default_debug_log_dir() -> Path:
    try:
        user_part = str(os.getuid())  # type: ignore[attr-defined]
    except AttributeError:
        try:
            user_part = getpass.getuser()
        except Exception:  # noqa: BLE001
            user_part = "unknown"
    return Path("/tmp") / f"jcli-{user_part}"


@dataclass(frozen=True)
class AppConfig:
    """Immutable application configuration resolved once at startup."""

    server_url: str
    token: str | None
    debug_log_dir: Path

    @classmethod
    def from_env(
        cls,
        server_url: str | None = None,
        token: str | None = None,
    ) -> "AppConfig":
        """Resolve configuration with CLI values taking precedence over env vars."""
        return cls(
            server_url=server_url
            or os.environ.get("JCLI_JUPYTER_SERVER_URL", _DEFAULT_SERVER_URL),
            token=token or os.environ.get("JCLI_JUPYTER_SERVER_TOKEN"),
            debug_log_dir=Path(
                os.environ.get("JCLI_DEBUG_LOG_DIR") or _default_debug_log_dir()
            ),
        )
