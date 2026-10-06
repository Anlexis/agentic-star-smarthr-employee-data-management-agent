"""Regression: an ERROR envelope must not disclose SmartHR record evidence.

Molt source review, 2026-09-04 (wave-8 batch): "the success-gate violation path
has containment, but the independent existing-ERROR branch rebuilds a truthy
response with `record_id` / `record_ref` and does not clear the merged
output-bearing state."

`PostProcessNode.execute()` has two error returns. `_contain()` - the gate
violation path - clears `_OUTPUT_BEARING_FIELDS` correctly. The independent
errored branch above it rebuilt the envelope from `record_id` / `record_ref`
read straight back out of state and returned ONLY that plus `status`, so
`result`, `confirmation`, `employee_id`, `employee_name`, `intent` and
`smarthr_payload` all survived in state untouched. Credential redaction ran on
that envelope, but redaction is a different property from containment: a record
identifier is not credential-shaped, so it passed through unchanged.

Those identifiers ARE the SmartHR write evidence: this node's own gate
(`_security_gate_output`, is_success=True) REFUSES a SUCCESS that lacks them. An
error envelope carrying them tells a caller who is being informed of a FAILURE
that an employee record was nonetheless written, and which one. SmartHR is an HR
system: the crew id, the employee code and the employee name are personal data.

Three properties are asserted, and they are three DIFFERENT properties:
  1. the shipped envelope carries no record evidence, AND stays TRUTHY - a falsy
     `formatted_output` re-opens the framework's `formatted_output or result`
     projection (AgentBaseGraph.get_output() applies no status check) onto
     whatever survived in state;
  2. the returned delta CLEARS the merged output-bearing state fields, so a
     checkpoint or a downstream reader cannot pick them up either;
  3. the error REASONS carry closed-set labels only. `error_log` is the
     internal channel - the shipped envelope is the reason code alone - but the
     audit trail reads it, so a reason that interpolated the employee code
     would still disclose the record there, and in any future projection.

A success-path control is included: without it every containment assertion above
would pass vacuously against a node that simply returned an empty envelope.
"""

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.call_smarthr_api_node import CallSmartHrApiNode
from src.nodes.post_process_node import _REASON_WORKFLOW_FAILED, PostProcessNode
from src.schemas.state import to_json

# Record evidence + the HR data hanging off it.
_RECORD_ID = "crew-1001"
_RECORD_REF = "smarthr://crews/crew-1001"
_EMPLOYEE_ID = "EMP-1001"
_EMPLOYEE_NAME = "Haruka Tanaka"


def _errored_state() -> dict:
    """An error raised AFTER the SmartHR call resolved a record.

    The realistic shape - the call succeeded and a later step failed - and the
    only shape in which record evidence is present on an error at all.
    """
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": ["ConfirmNode: downstream failure after the SmartHR call"],
        "record_id": _RECORD_ID,
        "record_ref": _RECORD_REF,
        "employee_id": _EMPLOYEE_ID,
        "employee_name": _EMPLOYEE_NAME,
        "intent": "update_record",
        "confirmation": f"Updated employee record '{_EMPLOYEE_NAME}' - ref={_RECORD_REF} - id={_RECORD_ID}",
        "smarthr_payload": to_json({"emp_code": _EMPLOYEE_ID, "last_name": _EMPLOYEE_NAME}),
        "result": {"record_id": _RECORD_ID, "confirmation": "done"},
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "pb-error-envelope",
        "session_id": "pb-s1",
        "thread_id": "pb-th1",
        "trace_id": "pb-t1",
        "node_history": [],
        "execution_time": {},
    }


def _flatten(value) -> str:
    """Render every reachable string in a returned value - nesting is not cover."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten(k) + " " + _flatten(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return str(value)


class TestErrorEnvelopeContainment:
    def test_error_envelope_carries_no_record_evidence(self):
        out = PostProcessNode().execute(_errored_state())

        assert out["status"] == AgentStatus.ERROR.value

        # formatted_output must be PRESENT and TRUTHY. The framework projects
        # `formatted_output or result` with no status check, so a falsy value
        # re-opens the fallback onto whatever survived in state.
        assert "formatted_output" in out, "the error path must ship an envelope"
        assert out["formatted_output"], "falsy formatted_output re-opens the `or result` fallback"
        # ... and it is the reason code alone - not the error_log lines.
        assert out["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}

        shipped = _flatten(out["formatted_output"])
        leaked = [
            name
            for name, value in (
                ("record_id", _RECORD_ID),
                ("record_ref", _RECORD_REF),
                ("employee_id", _EMPLOYEE_ID),
                ("employee_name", _EMPLOYEE_NAME),
                ("confirmation", "Updated employee record"),
                ("smarthr scheme", "smarthr://"),
            )
            if value in shipped
        ]
        assert not leaked, f"error envelope leaked SmartHR record evidence: {leaked}"

    def test_error_path_clears_output_bearing_state(self):
        """Omitting a field from ONE envelope is not clearing it from state."""
        out = PostProcessNode().execute(_errored_state())
        retained = [
            field
            for field in (
                "result",
                "confirmation",
                "record_id",
                "record_ref",
                "employee_id",
                "employee_name",
                "smarthr_payload",
                "intent",
            )
            if field not in out or out[field]
        ]
        assert not retained, f"output-bearing state not cleared on the error path: {retained}"

    def test_success_path_still_returns_the_answer(self):
        """CONTROL. Without it every containment assertion above passes
        vacuously - a node that returned an empty envelope would satisfy them
        all. The success path must still carry the record evidence."""
        out = PostProcessNode().execute(
            {
                "status": AgentStatus.SUCCESS.value,
                "record_id": _RECORD_ID,
                "record_ref": _RECORD_REF,
                "employee_id": _EMPLOYEE_ID,
                "employee_name": _EMPLOYEE_NAME,
                "intent": "lookup_record",
                "confirmation": f"Retrieved employee record - id={_RECORD_ID}",
                "smarthr_payload": to_json({"emp_code": _EMPLOYEE_ID}),
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
                "correlation_id": "pb-error-envelope-control",
                "node_history": [],
                "error_log": [],
                "execution_time": {},
            }
        )
        assert out["status"] == AgentStatus.SUCCESS.value
        shipped = _flatten(out["formatted_output"])
        assert _RECORD_ID in shipped, "the success path must still return the record evidence"
        assert _RECORD_REF in shipped
        assert _EMPLOYEE_NAME in shipped


class TestErrorLogNamesNoRecord:
    """The diagnostics the error envelope carries are a channel of their own.

    `error_log` is internal - the caller receives a reason code only - but the
    audit trail reads it, so an upstream reason that interpolates the employee
    code still discloses the record there. Reasons must be closed-set labels:
    what was not found, the HTTP status, the failure layer and the exception
    class.

    The HTTP-status and transport reasons of `CallSmartHrApiNode` were already
    contained; they are pinned here so a future edit cannot quietly re-open them.
    """

    def _state(self, **overrides) -> dict:
        state = {
            "smarthr_payload": to_json({"emp_code": _EMPLOYEE_ID}),
            "intent": "lookup_record",
            "employee_id": _EMPLOYEE_ID,
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
            "correlation_id": "pb-error-reason",
            "session_id": "pb-s1",
            "thread_id": "pb-th1",
            "trace_id": "pb-t1",
            "node_history": [],
            "error_log": [],
            "execution_time": {},
        }
        state.update(overrides)
        return state

    def test_record_not_found_reason_does_not_name_the_employee(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_smarthr_api_node.emit_trace_event", lambda *a, **k: None)

        class _EmptyLookupClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def find_crew(self, employee_id, api_token):
                return {"crews": []}

        monkeypatch.setattr("src.nodes.call_smarthr_api_node.SmartHrClient", _EmptyLookupClient)
        out = CallSmartHrApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert _EMPLOYEE_ID not in joined, "the not-found reason names the employee code"
        # The reason still has to be actionable: it names WHAT was not found,
        # a closed-set label, rather than for whom.
        assert "employee record" in joined

    def test_upstream_api_failure_reason_carries_no_upstream_body(self, monkeypatch):
        """A live tenant's error body is unbounded third-party text; only the
        HTTP status - a closed-set signal - belongs in a caller-facing reason."""
        monkeypatch.setattr("src.nodes.call_smarthr_api_node.emit_trace_event", lambda *a, **k: None)
        from src.services.smarthr_client import SmartHrApiError

        class _ApiErrorClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def find_crew(self, employee_id, api_token):
                raise SmartHrApiError(403, f"denied for {_EMPLOYEE_ID} ({_EMPLOYEE_NAME})")

        monkeypatch.setattr("src.nodes.call_smarthr_api_node.SmartHrClient", _ApiErrorClient)
        out = CallSmartHrApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert "403" in joined, "the HTTP status is the actionable signal - keep it"
        assert _EMPLOYEE_ID not in joined
        assert _EMPLOYEE_NAME not in joined

    def test_transport_failure_reason_carries_no_exception_text(self, monkeypatch):
        """A transport error string can carry the request URL and the record id."""
        monkeypatch.setattr("src.nodes.call_smarthr_api_node.emit_trace_event", lambda *a, **k: None)

        class _TransportErrorClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def find_crew(self, employee_id, api_token):
                raise ConnectionError(f"failed to reach https://api.smarthr.jp/v1/crews?emp_code={_EMPLOYEE_ID}")

        monkeypatch.setattr("src.nodes.call_smarthr_api_node.SmartHrClient", _TransportErrorClient)
        out = CallSmartHrApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert "transport" in joined, "the failure layer is the actionable signal - keep it"
        assert "ConnectionError" in joined, "the exception class is the closed-set part - keep it"
        assert _EMPLOYEE_ID not in joined
        assert "api.smarthr.jp" not in joined
        assert "failed to reach" not in joined
