"""Typed hook decisions — each class owns its Claude Code output schema.

PreToolUse and PostToolUse have fundamentally different JSON shapes
and semantics. PreToolUse is a permission gate (allow/deny/ask).
PostToolUse can only inject context — the tool already ran, so there
is no meaningful way to "deny" it. Each valid (event, outcome) pair
is modelled as its own dataclass so the emitter has no branches.

See https://code.claude.com/docs/en/hooks for the wire schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class HookEvent(str, Enum):
    """Claude Code hook event names — single source of truth for the
    wire strings that appear in payload's hookEventName field."""
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"


class PreToolUseOutcome(str, Enum):
    """Valid values for PreToolUse permissionDecision."""
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class HookDecision(Protocol):
    """Anything that serializes to a Claude Code hook JSON payload."""
    def to_payload(self) -> dict: ...


@dataclass(frozen=True)
class PreToolUseDecision:
    """Permission gate emitted from a PreToolUse hook.

    - ALLOW/ASK: reason shown to the user
    - DENY:      reason shown to Claude (fed into its context)
    """
    outcome: PreToolUseOutcome
    reason: str

    def to_payload(self) -> dict:
        return {
            "hookSpecificOutput": {
                "hookEventName": HookEvent.PRE_TOOL_USE.value,
                "permissionDecision": self.outcome.value,
                "permissionDecisionReason": self.reason,
            }
        }


@dataclass(frozen=True)
class PostToolUseContext:
    """Non-blocking context injection from a PostToolUse hook.

    The tool already ran — we can only tell Claude what happened.
    Used for both "paired file auto-synced" and "paired file drifted,
    someone else may have edited it" notifications.
    """
    context: str

    def to_payload(self) -> dict:
        return {
            "hookSpecificOutput": {
                "hookEventName": HookEvent.POST_TOOL_USE.value,
                "additionalContext": self.context,
            }
        }
