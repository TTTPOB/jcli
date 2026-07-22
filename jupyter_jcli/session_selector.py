"""Session selector formatting and resolution."""


class SessionSelectorError(ValueError):
    """Base error raised when a session selector cannot be resolved."""

    code: str


class SessionSelectorNotFound(SessionSelectorError):
    """Raised when a selector matches no active session."""

    code = "SESSION_NOT_FOUND"


class SessionSelectorAmbiguous(SessionSelectorError):
    """Raised when a selector matches multiple active sessions."""

    code = "SESSION_SELECTOR_AMBIGUOUS"


def short_session_ids(sessions: list[dict]) -> dict[str, str]:
    """Return the shortest unique, at-least-three-character prefix per session."""
    session_ids = [str(session["session_id"]) for session in sessions]
    short_ids: dict[str, str] = {}

    for session_id in session_ids:
        for length in range(3, len(session_id) + 1):
            prefix = session_id[:length]
            if sum(other.startswith(prefix) for other in session_ids) == 1:
                short_ids[session_id] = prefix
                break
        else:
            short_ids[session_id] = session_id

    return short_ids


def resolve_session_selector(
    server_url: str, selector: str, token: str | None = None
) -> str:
    """Resolve an ID prefix or exact session name to a full active session ID."""
    from jupyter_jcli.server import list_sessions

    sessions = list_sessions(server_url, token)
    exact_id_matches = [
        session for session in sessions if session["session_id"] == selector
    ]
    id_matches = exact_id_matches or [
        session for session in sessions if session["session_id"].startswith(selector)
    ]
    name_matches = [session for session in sessions if session.get("name") == selector]
    matches = {session["session_id"]: session for session in id_matches + name_matches}

    if not matches:
        raise SessionSelectorNotFound(
            f"No active session matches selector {selector!r}. Run 'j-cli session list'."
        )
    if len(matches) == 1:
        return next(iter(matches))

    short_ids = short_session_ids(sessions)
    descriptions = ", ".join(
        f"{short_ids[session_id]} (name: {session.get('name') or '<unnamed>'})"
        for session_id, session in matches.items()
    )
    raise SessionSelectorAmbiguous(
        f"Session selector {selector!r} matches multiple active sessions: {descriptions}. "
        "Use a longer ID prefix or a unique session name."
    )
