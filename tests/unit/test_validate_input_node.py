# CMN-C2-281 - Unit tests: ValidateInputNode (inner Step 1, flag-and-redact)
#
# Canon: nodes are invoked via node(state) - through BaseNode.__call__ (trust
# gate -> input gate -> execute -> output gate) - never bare
# node.execute(state). This inner domain node declares ANONYMOUS, so the state
# builder sets caller_trust_level = TrustLevel.ANONYMOUS.value.
#
# Two redaction layers are exercised here:
#   * the FRAMEWORK mask in __call__ rewrites emails (any '@') in
#     validated_input to "[MASKED]" BEFORE execute() sees the text - the
#     intentional-PII test asserts that [MASKED] path;
#   * the NODE's own deterministic scan handles token-shaped strings the
#     framework mask does not cover (secret_* / sk-* / eyJ*) - flag + [REDACTED].

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.validate_input_node import ValidateInputNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.validate_input_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "validated_input": "Look up the employee record for employee code 1001.",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "validate-input-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestValidateInputNode:
    def setup_method(self):
        self.node = ValidateInputNode()

    def test_success_plain_text(self):
        result = self.node(_state(validated_input="Show the record summary for the flagged employee"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "Show the record summary for the flagged employee"
        assert from_json(result["redaction_flags"], None) == []

    def test_success_serialized_json_input(self):
        payload = json.dumps({"text": "summarize the record on file", "employee_hint": "1001"})
        result = self.node(_state(validated_input=payload))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "summarize the record on file"
        assert result["employee_hint"] == "1001"

    def test_empty_input_errors(self):
        result = self.node(_state(validated_input="  "))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_short_input_errors(self):
        result = self.node(_state(validated_input="ab"))
        assert result["status"] == AgentStatus.ERROR.value

    def test_framework_mask_rewrites_email_before_execute(self):
        """Intentional-PII path: the framework mask in __call__ rewrites the
        email to [MASKED] before execute() runs, so no raw address survives."""
        result = self.node(_state(validated_input="send the summary for employee code 1001 to hr.lead@example.com"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "hr.lead@example.com" not in result["validated_input"]
        assert "[MASKED]" in result["validated_input"]

    def test_node_scan_redacts_token_shaped_string(self):
        """The node's own deterministic scan covers token shapes the framework
        PII mask does not (secret_*): flagged + [REDACTED] before logging."""
        text = "integration key secret_abcdef123456 for employee code 1001"
        result = self.node(_state(validated_input=text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "secret_abcdef123456" not in result["validated_input"]
        assert "[REDACTED]" in result["validated_input"]
        assert "token" in from_json(result["redaction_flags"], [])

    def test_audit_emits_scan_outcome_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.validate_input_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state(validated_input="summarize the record for employee code 1001"))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - flags only, never the text.
        assert payloads["validate_input_complete"]["redaction_flags"] == []
        assert "text" not in payloads["validate_input_complete"]

    def test_state_hint_wins_over_the_copy_embedded_in_the_json(self):
        """The state value was validated by pre_process and carried in unmasked
        by the context bridge; the copy inside validated_input rides a field the
        framework rewrites at every node boundary, so preferring it could
        substitute a rewritten code for the real one."""
        serialized = json.dumps({"text": "Look up the record on file.", "employee_hint": "[MASKED]"})
        result = self.node(_state(validated_input=serialized, employee_hint="7007"))
        assert result["employee_hint"] == "7007"

    def test_json_hint_is_used_when_state_carries_none(self):
        serialized = json.dumps({"text": "Look up the record on file.", "employee_hint": "9009"})
        result = self.node(_state(validated_input=serialized))
        assert result["employee_hint"] == "9009"
