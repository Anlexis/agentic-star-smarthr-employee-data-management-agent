# CMN-C2-281 - Unit tests: ClassifyIntentNode (inner Step 2)
# Intents: lookup_record / create_record / update_record. A deterministic
# keyword heuristic is always computed first; an Azure OpenAI call then
# attempts to override it (test-double injection only below - no real network
# call). Unknown keyword signal falls back to the read-only lookup.
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


class _FakeLLM:
    """Test-double for AzureOpenAIClient - same `complete(messages) -> dict` contract."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> dict[str, object]:
        self.calls.append(messages)
        return {"content": self._content}


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_lookup_record(self):
        result = self.node(_state("Look up the employee record for employee code 1001."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_record"

    def test_keyword_create_record(self):
        result = self.node(_state("Register a new employee with code A123"))
        assert result["intent"] == "create_record"

    def test_keyword_update_record(self):
        result = self.node(_state("Update the record for employee code 1001 with the new grade"))
        assert result["intent"] == "update_record"

    def test_write_keyword_wins_over_lookup(self):
        # Priority order is writes-first: an "update ... then show it" style
        # request classifies as the write, never the read.
        result = self.node(_state("Update the grade for employee code 1001 and show the record"))
        assert result["intent"] == "update_record"

    def test_no_signal_defaults_to_readonly_lookup(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_record"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to lookup_record" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up the employee record for employee code 1001."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "lookup_record"
        assert payloads["classify_intent_complete"]["defaulted"] is False
        assert payloads["classify_intent_complete"]["llm_used"] is False


class TestClassifyIntentNodeLLM:
    """LLM-backed intent classification (test-double injection - no real Azure call)."""

    def test_llm_response_overrides_keyword_heuristic(self):
        """Valid LLM JSON overrides the keyword classification."""
        llm = _FakeLLM('{"intent": "create_record"}')
        node = ClassifyIntentNode(llm=llm)
        # Keyword heuristic alone would say lookup_record ("show"); the LLM
        # override takes precedence.
        result = node(_state("show me how to set up onboarding for the new hire"))

        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "create_record"
        assert "error_log" not in result
        assert len(llm.calls) == 1
        assert llm.calls[0][0]["role"] == "system"

    def test_llm_prose_wrapped_json_is_extracted(self):
        """LLM output wrapped in prose/markdown still parses (extract_json_object)."""
        llm = _FakeLLM('Sure, here is my analysis:\n```json\n{"intent": "update_record"}\n```')
        result = ClassifyIntentNode(llm=llm)(_state("please handle this for the team"))
        assert result["intent"] == "update_record"

    def test_malformed_llm_response_falls_back_to_keyword_heuristic(self):
        """Invalid shape (no 'intent' key) -> keyword result, no crash."""
        llm = _FakeLLM('{"confidence": 0.9}')
        result = ClassifyIntentNode(llm=llm)(_state("Look up the employee record for employee code 1001."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_record"

    def test_llm_invalid_intent_value_falls_back_to_keyword_heuristic(self):
        """A well-formed JSON object whose intent value is not one of the
        three valid labels must not be accepted."""
        llm = _FakeLLM('{"intent": "delete_record"}')
        result = ClassifyIntentNode(llm=llm)(_state("Update the record for employee code 1001 with the new grade"))
        assert result["intent"] == "update_record"  # keyword result, LLM value rejected

    def test_llm_exception_falls_back_to_keyword_heuristic(self):
        """LLM call raising (e.g. API error) -> keyword result, no crash."""

        class _RaisingLLM:
            def complete(self, messages):
                raise RuntimeError("simulated Azure OpenAI API error")

        result = ClassifyIntentNode(llm=_RaisingLLM())(_state("Register a new employee with code A123"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "create_record"

    def test_no_secret_configured_falls_back_to_keyword_heuristic(self):
        """No llm= injected and no secret provisioned -> keyword result, no crash.

        This is the real production shape when register_nodes() never passes
        llm= and ctx.secrets.require("AZURE_OPENAI_API_KEY") has nothing bound
        (e.g. local/dev/CI with no Azure key configured)."""
        result = ClassifyIntentNode()(_state("Look up the employee record for employee code 1001."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_record"

    def test_empty_input_skips_llm_call(self):
        """No validated_input -> execute() errors before _classify_via_llm ever runs."""
        llm = _FakeLLM('{"intent": "lookup_record"}')
        ClassifyIntentNode(llm=llm)(_state(""))
        assert llm.calls == []

    def test_low_confidence_note_cleared_when_llm_overrides(self):
        """No keyword signal (would default to lookup_record with a note), but
        the LLM confidently classifies it as a write - the low-confidence
        note must not survive since the LLM result is the one returned."""
        llm = _FakeLLM('{"intent": "update_record"}')
        result = ClassifyIntentNode(llm=llm)(_state("please handle this for the team"))
        assert result["intent"] == "update_record"
        assert "error_log" not in result
