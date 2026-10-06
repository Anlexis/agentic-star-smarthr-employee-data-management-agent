# CMN-C2-281 - Unit tests: InferSmartHrFieldsNode (inner Step 3)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value. Positive TEXT-channel payloads are PII-free: the
# framework mask rewrites Title-Case bigrams in validated_input, so quoted
# display names use a single-word name. The caller-contract tests below use the
# structured channel, which is not masked - that is the whole point of it.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_smarthr_fields_node import InferSmartHrFieldsNode
from src.schemas.state import from_json, to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_smarthr_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "lookup_record", employee_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "employee_hint": employee_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferSmartHrFieldsNode:
    def setup_method(self):
        self.node = InferSmartHrFieldsNode()

    def test_lookup_extracts_code_from_text(self):
        result = self.node(
            _state("Look up the employee record for employee code 1001 and summarize the record on file.")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["employee_id"] == "1001"
        # smarthr_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["smarthr_payload"], str)
        assert from_json(result["smarthr_payload"], {}) == {"emp_code": "1001"}

    def test_code_shaped_hint_used_when_text_has_no_code(self):
        result = self.node(_state("Show the current record summary", employee_hint="A123"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["employee_id"] == "A123"
        assert from_json(result["smarthr_payload"], {}) == {"emp_code": "A123"}

    def test_create_builds_crew_payload(self):
        # Field values stay lower-case: the framework name mask rewrites
        # Title-Case word pairs even ACROSS newlines ("Sales\nGrade" would be
        # masked before execute() sees the text).
        text = 'Register a new employee named "Alice"\nDepartment: sales\nGrade: 3'
        result = self.node(_state(text, intent="create_record", employee_hint="9002"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["employee_id"] == "9002"
        assert result["employee_name"] == "Alice"
        record = from_json(result["smarthr_payload"], {})
        # SmartHR REST API v1 crew shape: emp_code + last_name + name-keyed custom_fields.
        assert record["emp_code"] == "9002"
        assert record["last_name"] == "Alice"
        assert {"name": "Department", "value": "sales"} in record["custom_fields"]
        assert {"name": "Grade", "value": "3"} in record["custom_fields"]

    def test_update_builds_crew_payload(self):
        text = "Update the record for employee code 1001\nGrade: 4"
        result = self.node(_state(text, intent="update_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        record = from_json(result["smarthr_payload"], {})
        assert record["emp_code"] == "1001"
        assert {"name": "Grade", "value": "4"} in record["custom_fields"]

    def test_unresolved_code_left_empty_never_invented(self):
        result = self.node(_state("Show the record summary for the flagged employee"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["employee_id"] == ""
        assert from_json(result["smarthr_payload"], {}) == {"emp_code": ""}

    def test_non_code_shaped_hint_left_unresolved(self):
        result = self.node(_state("Show the record summary", employee_hint="not a valid code!"))
        assert result["employee_id"] == ""

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]


class TestCallerSuppliedEmployeeDataWins:
    """The validated input_context channel beats text inference.

    The structured channel is the caller's unambiguous instruction and arrives
    unmasked; the text channel is a heuristic read of a string the framework
    has already rewritten to mask personal data.
    """

    def setup_method(self):
        self.node = InferSmartHrFieldsNode()

    def test_caller_code_beats_a_code_mentioned_in_the_text(self):
        result = self.node(
            _state(
                "Look up the employee record for employee code 1001.",
                caller_employee=to_json({"name": "Taro Yamada"}),
                employee_hint="7007",
            )
        )
        assert result["employee_id"] == "7007"

    def test_caller_name_beats_a_quoted_name_in_the_text(self):
        result = self.node(
            _state(
                'Update the record for employee code 1001 named "Alice"',
                intent="update_record",
                caller_employee=to_json({"name": "Taro Yamada"}),
            )
        )
        assert result["employee_name"] == "Taro Yamada"
        assert from_json(result["smarthr_payload"], {})["last_name"] == "Taro Yamada"

    def test_caller_fields_are_merged_and_win_on_a_name_collision(self):
        result = self.node(
            _state(
                "Update the record for employee code 1001\nGrade: 4\nShift: night",
                intent="update_record",
                caller_employee=to_json({"fields": [{"name": "Grade", "value": "9"}]}),
            )
        )
        custom_fields = from_json(result["smarthr_payload"], {})["custom_fields"]
        assert {"name": "Grade", "value": "9"} in custom_fields
        assert {"name": "Grade", "value": "4"} not in custom_fields
        assert {"name": "Shift", "value": "night"} in custom_fields

    def test_absent_caller_data_degrades_to_the_text_baseline(self):
        result = self.node(_state("Look up the employee record for employee code 1001."))
        assert result["employee_id"] == "1001"

    def test_a_malformed_caller_block_is_ignored_not_crashed(self):
        """The bridge only ever carries validated data, so a malformed block
        here means a direct/unit invocation - degrade, never raise."""
        result = self.node(_state("Look up the employee record for employee code 1001.", caller_employee="not-json"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["employee_id"] == "1001"
