"""Jupyter Server REST API client wrapping jupyter-server-client."""

from jupyter_server_client import JupyterServerClient


def get_client(server_url: str, token: str | None = None) -> JupyterServerClient:
    """Create a JupyterServerClient instance."""
    client = JupyterServerClient(base_url=server_url, token=token, verify_ssl=False)
    # Bypass proxy for local connections
    client.http_client.session.trust_env = False
    return client


def healthcheck(server_url: str, token: str | None = None) -> dict:
    """Check server status. Returns dict with version and kernel count."""
    client = get_client(server_url, token)
    version_info = client.get_version()
    kernels = client.kernels.list_kernels()
    return {
        "version": version_info.version,
        "kernels_running": len(kernels),
    }


def list_kernelspecs(server_url: str, token: str | None = None) -> list[dict]:
    """List available kernel specs."""
    client = get_client(server_url, token)
    specs = client.kernelspecs.list_kernelspecs()
    result = []
    for name, ks in specs.kernelspecs.items():
        result.append({
            "name": name,
            "display_name": ks.spec.display_name,
            "language": ks.spec.language,
        })
    return result


def create_session(
    server_url: str,
    kernel_name: str,
    session_name: str | None = None,
    token: str | None = None,
) -> dict:
    """Create a new session with the given kernel spec."""
    client = get_client(server_url, token)
    session = client.sessions.create_session(
        path=session_name or "",
        kernel={"name": kernel_name},
        name=session_name,
    )
    return {
        "session_id": session.id,
        "kernel_id": session.kernel.id,
        "kernel_name": session.kernel.name,
    }


def list_sessions(server_url: str, token: str | None = None) -> list[dict]:
    """List active sessions."""
    client = get_client(server_url, token)
    sessions = client.sessions.list_sessions()
    result = []
    for s in sessions:
        result.append({
            "session_id": s.id,
            "name": s.name or "",
            "kernel_id": s.kernel.id,
            "kernel_name": s.kernel.name,
            "kernel_state": getattr(s.kernel, "execution_state", "unknown"),
        })
    return result


def delete_session(server_url: str, session_id: str, token: str | None = None) -> None:
    """Delete (kill) a session."""
    client = get_client(server_url, token)
    client.sessions.delete_session(session_id)


def get_kernel_id_for_session(
    server_url: str, session_id: str, token: str | None = None,
) -> str:
    """Get kernel_id from a session_id."""
    client = get_client(server_url, token)
    session = client.sessions.get_session(session_id)
    return session.kernel.id


def interrupt_kernel(server_url: str, kernel_id: str, token: str | None = None) -> None:
    """Interrupt a running kernel via REST API."""
    client = get_client(server_url, token)
    client.http_client.post(f"/api/kernels/{kernel_id}/interrupt")


def restart_kernel(server_url: str, kernel_id: str, token: str | None = None) -> None:
    """Restart a kernel via REST API."""
    client = get_client(server_url, token)
    client.http_client.post(f"/api/kernels/{kernel_id}/restart")
