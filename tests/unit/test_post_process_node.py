# CMN-C2-281 - Unit tests: PostProcessNode (outer backbone, domain output gate)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); this backbone formatter declares ANONYMOUS -> the
# state builder sets caller_trust_level = TrustLevel.ANONYMOUS.value. The domain
# output gate is the MODULE-LEVEL _security_gate_output() helper (the framework
# gate methods are @final and the real SDK auto-wraps _extra_ hooks), so the
# helper is also unit-tested directly as a plain function.
#
# The errored-state branch is driven through execute() DIRECTLY where noted:
# BaseNode.__call__ short-circuits on an incoming errored state and the
# backbone routes an error straight to finalize, so that branch is only
# reachable from inside - and it must still be a closed set.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import (
    ERROR_REASONS,
    _OUTPUT_BEARING_FIELDS,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    _UNNAMEABLE_KEY,
    PostProcessNode,
    _contain,
    _security_gate_output,
)
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "crew-1001",
        "record_ref": "smarthr://crews/crew-1001",
        "employee_name": "Employee 1001",
        "intent": "lookup_record",
        "confirmation": "Retrieved employee record 'Employee 1001' - ref=smarthr://crews/crew-1001 - id=crew-1001",
        "smarthr_payload": to_json({"emp_code": "1001"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


def _bearer(fill: str = "a") -> str:
    # Built at runtime so no credential-shaped literal is committed.
    return "Bearer " + fill * 24


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    token-like value inside an echoed response body. Assembled at runtime so no
    credential-shaped literal is committed - and deliberately NOT a shape any
    credential detector fires on, so it travels as ordinary text and only the
    envelope construction itself can keep it away from the caller."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'customer':'A. Tanaka','token':'" + token + "'}"


# Fragments of the sentinel that must survive nowhere in a returned mapping.
_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")


def _leaves(value):
    """Every key and scalar inside `value`, rendered as text, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _leaves(item)
    else:
        yield str(value)


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == "crew-1001"
        assert out["record_ref"] == "smarthr://crews/crew-1001"
        assert out["employee_name"] == "Employee 1001"
        assert out["intent"] == "lookup_record"
        assert out["confirmation"].startswith("Retrieved employee record")
        # The JSON smarthr_payload string surfaces parsed.
        assert out["smarthr_payload"] == {"emp_code": "1001"}
        # The reason code is an error-envelope key only.
        assert "reason" not in out

    def test_status_is_plain_string_not_enum(self):
        """State status must be the `.value` string, never a bare AgentStatus
        enum member (str-enum equality masks the difference in `==` asserts, so
        pin the concrete type here)."""
        result = self.node(_state())
        assert result["status"].__class__ is str
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallSmartHrApiNode: SmartHR returned HTTP 403"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "HTTP 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_gate_blocks_success_without_record_evidence(self):
        """Full node path: a SUCCESS output missing record_id/record_ref is blocked."""
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output gate" in entry for entry in result["error_log"])

    def test_gate_violation_clears_every_output_bearing_field(self):
        """Returning an error status is not containment on its own.

        The base graph shapes its response as `formatted_output or result`, so
        a blocked answer left in `result` still ships inside the error
        envelope. Every output-bearing field must come back cleared.
        """
        result = self.node(_state(record_id="", record_ref=""))
        for field in _OUTPUT_BEARING_FIELDS:
            assert field in result, field
            assert not result[field], field
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "crew-1001", "record_ref": "smarthr://crews/crew-1001", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "", "record_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "record_id/record_ref" in violations[0]

    def test_blocks_credential_shaped_value(self):
        violations = _security_gate_output(
            {"record_id": "crew-1001", "confirmation": _bearer()},
            is_success=True,
        )
        assert any("confirmation" in v for v in violations)

    def test_blocks_credential_nested_inside_the_request_body(self):
        """The scan walks the WHOLE structure, not just top-level strings.

        The assembled SmartHR request body is a mapping with a list of
        employee-data fields inside it, so a credential riding two levels down
        is the case that matters; a top-level-only scan reports zero findings
        on exactly that input.
        """
        token_like = "sk-" + "b" * 24
        nested = {
            "record_id": "crew-1001",
            "smarthr_payload": {
                "emp_code": "1001",
                "custom_fields": [{"name": "Note", "value": token_like}],
            },
        }
        violations = _security_gate_output(nested, is_success=True)
        assert any("custom_fields[0].value" in v for v in violations)
        # The violation names the PATH, never the value.
        assert all(token_like not in v for v in violations)
        # Control: the same verifier reports nothing on a clean nested body,
        # so a finding above means "gate saw it", not "probe always fires".
        clean = {
            "record_id": "crew-1001",
            "smarthr_payload": {"emp_code": "1001", "custom_fields": [{"name": "Note", "value": "on leave"}]},
        }
        assert _security_gate_output(clean, is_success=True) == []

    def test_credential_shaped_mapping_key_is_caught_and_withheld_from_the_label(self):
        """Keys are scanned like values. A credential-shaped key is refused AND
        kept out of the label: the label travels in error_log, where the
        framework's own credential scan raises on the node result and replaces
        the cleared result with a bare error - re-opening the `result`
        fallback the clearing closed."""
        jwt_like = "eyJ" + "c" * 40
        violations = _security_gate_output(
            {"record_id": "crew-1001", "smarthr_payload": {jwt_like: "x"}}, is_success=True
        )
        assert violations
        assert all(jwt_like not in v for v in violations)
        assert any(_UNNAMEABLE_KEY in v for v in violations)

    def test_blocks_a_field_outside_the_declared_output_contract(self):
        """Bulk record data must not ride out on an undeclared field."""
        violations = _security_gate_output(
            {"record_id": "crew-1001", "crew": {"resident_card_number": "1234"}},
            is_success=True,
        )
        assert any("declared output contract" in v for v in violations)

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False)
        assert violations == []


# Every non-success path this node has. `via` says how the path is reached:
# node(state) runs the framework pipeline; execute() is used for an errored
# state because BaseNode.__call__ short-circuits on it (and the backbone routes
# an error straight to finalize), so that branch is only reachable from inside.
_ERROR_PATHS = [
    pytest.param(
        {"status": AgentStatus.ERROR.value, "record_id": "", "record_ref": ""},
        "execute",
        id="inner-workflow-error",
    ),
    pytest.param(
        {
            "status": AgentStatus.ERROR.value,
            "result": {"record_id": "crew-1001", "confirmation": "Retrieved employee record 'Employee 1001'"},
        },
        "execute",
        id="inner-workflow-error-with-answer-in-result",
    ),
    pytest.param(
        {
            "status": AgentStatus.ERROR.value,
            "error_log": [_sentinel(), "CallSmartHrApiNode: rejected " + _bearer()],
        },
        "execute",
        id="inner-workflow-error-with-credential-in-error-log",
    ),
    pytest.param({"record_id": "", "record_ref": ""}, "call", id="gate-missing-record-evidence"),
    pytest.param(
        {"smarthr_payload": to_json({"emp_code": "1001", "custom_fields": [{"name": "Note", "value": _bearer()}]})},
        "call",
        id="gate-credential-nested-in-payload",
    ),
    pytest.param(
        {"smarthr_payload": to_json({"emp_code": "1001", "sk-" + "a" * 20: _bearer()})},
        "call",
        id="gate-credential-shaped-mapping-key",
    ),
]


class TestErrorEnvelopeIsClosedSet:
    """Whatever the non-success path, the caller-visible envelope is made of
    this module's own constants - never of node-authored text.

    error_log is seeded with a recognisable sentinel on every path: a name and
    a token-like value inside an echoed upstream body, which is exactly what an
    API-call failure can put there. Truncating or redacting such a line is not
    a closed set, so it must appear nowhere in what the node returns.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def _drive(self, overrides: dict, via: str) -> dict:
        state = _state(**{"error_log": [_sentinel()], **overrides})
        return self.node.execute(state) if via == "execute" else self.node(state)

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_envelope_values_are_declared_constants(self, overrides, via):
        result = self._drive(overrides, via)

        assert result["status"] == AgentStatus.ERROR.value
        envelope = result["formatted_output"]
        assert set(envelope) == {"reason"}, envelope
        assert set(envelope.values()) <= ERROR_REASONS, envelope

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_envelope_stays_truthy_and_the_answer_is_cleared(self, overrides, via):
        """`formatted_output or result`: a falsy envelope re-opens the fallback,
        and a surviving `result` is what it would fall back onto."""
        result = self._drive(overrides, via)

        assert result["formatted_output"], "a falsy formatted_output re-opens the `result` fallback"
        assert result["result"] is None
        cleared = {field: result[field] for field in _OUTPUT_BEARING_FIELDS if field != "result"}
        assert not any(cleared.values()), cleared

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_seeded_error_text_appears_nowhere_in_the_returned_mapping(self, overrides, via):
        result = self._drive(overrides, via)

        leaves = list(_leaves(result))
        for fragment in _SENTINEL_FRAGMENTS:
            assert not any(fragment in leaf for leaf in leaves), (fragment, result)
        rendered = json.dumps(result, default=str)
        assert not any(fragment in rendered for fragment in _SENTINEL_FRAGMENTS), rendered

    def test_the_reason_names_the_path_taken(self):
        errored = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))
        withheld = self.node(_state(record_id="", record_ref="", error_log=[_sentinel()]))

        assert errored["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert withheld["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_inner_error_entries_are_not_re_emitted(self):
        """The state reducer appends error_log; re-emitting the inner entries
        would duplicate every line, and the caller never sees them anyway."""
        result = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))

        assert "error_log" not in result

    def test_gate_violations_travel_in_error_log_only(self):
        result = self.node(_state(record_id="", record_ref="", error_log=[_sentinel()]))

        assert any("output gate" in entry for entry in result["error_log"])
        assert "output gate" not in json.dumps(result["formatted_output"])

    def test_the_helper_is_the_single_error_shape(self):
        """Both paths return exactly what _contain() builds - nothing added."""
        errored = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))
        withheld = self.node(_state(record_id="", record_ref="", error_log=[_sentinel()]))

        assert errored == _contain(_REASON_WORKFLOW_FAILED)
        expected = _contain(_REASON_OUTPUT_WITHHELD, withheld["error_log"])
        assert {k: v for k, v in withheld.items() if k not in ("node_history", "execution_time")} == expected

    def test_a_caller_field_name_lands_in_a_value_and_the_label_names_fixed_keys_only(self):
        """The assembled payload's mapping keys are fixed by the field-inference
        step; a caller's field NAME becomes the `name` VALUE of a custom-field
        entry. So the violation label is built from fixed keys and indices and
        cannot carry caller text - and the caller receives the reason code
        only, so it could not reach them through the envelope anyway."""
        field_name = "A. Tanaka <a.tanaka@" + "example.com>"
        payload = {"emp_code": "1001", "custom_fields": [{"name": field_name, "value": _bearer()}]}
        result = self.node(_state(smarthr_payload=to_json(payload), error_log=[_sentinel()]))

        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        label = " ".join(result["error_log"])
        assert "formatted_output['smarthr_payload.custom_fields[0].value']" in label
        assert "Tanaka" not in label
        assert _bearer() not in label

    def test_a_credential_shaped_mapping_key_is_withheld_from_the_label(self):
        """A key that is itself credential-shaped is refused AND kept out of
        the label: quoted, it would travel in error_log, where the framework
        credential scan raises on the node result and replaces the cleared
        result - restoring the leak the clearing just closed. The clearing
        surviving is the proof that nothing raised."""
        credential_shaped_key = "sk-" + "a" * 20
        payload = {"emp_code": "1001", credential_shaped_key: "x"}
        result = self.node(_state(smarthr_payload=to_json(payload)))

        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert result["result"] is None
        assert credential_shaped_key not in json.dumps(result, default=str)
        assert any(_UNNAMEABLE_KEY in entry for entry in result["error_log"])
