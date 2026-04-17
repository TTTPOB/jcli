"""Unit tests for jupyter_jcli.hook_decision typed decision classes."""

import pytest

from jupyter_jcli.hook_decision import (
    HookEvent,
    PostToolUseContext,
    PreToolUseDecision,
    PreToolUseOutcome,
)


class TestHookEvent:
    def test_pre_tool_use_value(self):
        assert HookEvent.PRE_TOOL_USE == "PreToolUse"
        assert isinstance(HookEvent.PRE_TOOL_USE, str)

    def test_post_tool_use_value(self):
        assert HookEvent.POST_TOOL_USE == "PostToolUse"
        assert isinstance(HookEvent.POST_TOOL_USE, str)

    def test_invalid_event_raises(self):
        with pytest.raises(ValueError):
            HookEvent("bogus")


class TestPreToolUseOutcome:
    def test_allow(self):
        assert PreToolUseOutcome.ALLOW == "allow"
        assert isinstance(PreToolUseOutcome.ALLOW, str)

    def test_deny(self):
        assert PreToolUseOutcome.DENY == "deny"

    def test_ask(self):
        assert PreToolUseOutcome.ASK == "ask"

    def test_invalid_outcome_raises(self):
        with pytest.raises(ValueError):
            PreToolUseOutcome("bogus")


class TestPreToolUseDecision:
    def test_deny_payload(self):
        d = PreToolUseDecision(PreToolUseOutcome.DENY, "you shall not pass")
        p = d.to_payload()
        assert p == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "you shall not pass",
            }
        }

    def test_allow_payload(self):
        d = PreToolUseDecision(PreToolUseOutcome.ALLOW, "go ahead")
        p = d.to_payload()
        assert p["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert p["hookSpecificOutput"]["permissionDecisionReason"] == "go ahead"
        assert "additionalContext" not in p["hookSpecificOutput"]

    def test_ask_payload(self):
        d = PreToolUseDecision(PreToolUseOutcome.ASK, "are you sure?")
        p = d.to_payload()
        assert p["hookSpecificOutput"]["permissionDecision"] == "ask"
        assert p["hookSpecificOutput"]["hookEventName"] == "PreToolUse"

    def test_no_additional_context_in_pre(self):
        d = PreToolUseDecision(PreToolUseOutcome.DENY, "reason")
        p = d.to_payload()
        assert "additionalContext" not in p["hookSpecificOutput"]
        assert "decision" not in p


class TestPostToolUseContext:
    def test_payload_shape(self):
        c = PostToolUseContext("paired notebook drifted, run j-cli convert")
        p = c.to_payload()
        assert p == {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "paired notebook drifted, run j-cli convert",
            }
        }

    def test_no_permission_decision_in_post(self):
        c = PostToolUseContext("some context")
        p = c.to_payload()
        assert "permissionDecision" not in p["hookSpecificOutput"]
        assert "permissionDecisionReason" not in p["hookSpecificOutput"]
        assert "decision" not in p

    def test_auto_synced_context(self):
        c = PostToolUseContext("Auto-synced foo.py to bar.ipynb. Pair is now in sync.")
        p = c.to_payload()
        assert "Auto-synced" in p["hookSpecificOutput"]["additionalContext"]
