"""Jupyter Server REST API client wrapping jupyter-server-client."""

from jupyter_server_client import JupyterServerClient

from jupyter_jcli.session_selector import resolve_session_selector, short_session_ids


class ServerClient:
    """Expose jcli server operations through one reusable HTTP client."""

    def __init__(self, server_url: str, token: str | None = None):
        self._client = JupyterServerClient(
            base_url=server_url,
            token=token,
            verify_ssl=False,
        )
        # Bypass proxy for local connections
        self._client.http_client.session.trust_env = False

    def healthcheck(self) -> dict:
        """Check server status. Returns dict with version and kernel count."""
        version_info = self._client.get_version()
        kernels = self._client.kernels.list_kernels()
        return {
            "version": version_info.version,
            "kernels_running": len(kernels),
        }

    def list_kernelspecs(self) -> list[dict]:
        """List available kernel specs."""
        specs = self._client.kernelspecs.list_kernelspecs()
        result = []
        for name, ks in specs.kernelspecs.items():
            result.append(
                {
                    "name": name,
                    "display_name": ks.spec.display_name,
                    "language": ks.spec.language,
                }
            )
        return result

    def create_session(
        self,
        kernel_name: str,
        session_name: str | None = None,
    ) -> dict:
        """Create a new session with the given kernel spec."""
        session = self._client.sessions.create_session(
            path=session_name or "",
            kernel={"name": kernel_name},
            name=session_name,
        )
        return {
            "session_id": session.id,
            "kernel_id": session.kernel.id,
            "kernel_name": session.kernel.name,
        }

    def list_sessions(self) -> list[dict]:
        """List active sessions."""
        sessions = self._client.sessions.list_sessions()
        result = []
        for session in sessions:
            result.append(
                {
                    "session_id": session.id,
                    "name": session.name or "",
                    "kernel_id": session.kernel.id,
                    "kernel_name": session.kernel.name,
                    "kernel_state": getattr(
                        session.kernel, "execution_state", "unknown"
                    ),
                }
            )
        return result

    def delete_session(self, session_id: str) -> None:
        """Delete (kill) a session."""
        self._client.sessions.delete_session(session_id)

    def get_kernel_id_for_session(self, session_id: str) -> str:
        """Get kernel_id from a session_id."""
        session = self._client.sessions.get_session(session_id)
        return session.kernel.id

    def resolve_session(self, selector: str) -> str:
        """Resolve a session selector against active sessions."""
        return resolve_session_selector(self.list_sessions(), selector)

    def resolve_kernel(self, selector: str) -> tuple[str, str]:
        """Resolve a session selector and return its session and kernel IDs."""
        session_id = self.resolve_session(selector)
        kernel_id = self.get_kernel_id_for_session(session_id)
        return session_id, kernel_id

    def get_session_selector(self, session_id: str) -> str:
        """Return the shortest unique selector for an active session ID."""
        return short_session_ids(self.list_sessions())[session_id]

    def interrupt_kernel(self, kernel_id: str) -> None:
        """Interrupt a running kernel via REST API."""
        self._client.http_client.post(f"/api/kernels/{kernel_id}/interrupt")

    def restart_kernel(self, kernel_id: str) -> None:
        """Restart a kernel via REST API."""
        self._client.http_client.post(f"/api/kernels/{kernel_id}/restart")
