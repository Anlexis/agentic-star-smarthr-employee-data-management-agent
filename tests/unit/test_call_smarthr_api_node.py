# CMN-C2-281 - Unit tests: CallSmartHrApiNode (inner Step 4, tool side-effect)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value. The ONE documented exception: the config-override
# call passes a 2nd (config) argument, which __call__ cannot forward - that
# single test stays a DIRECT execute(state, config=...) call (an ANONYMOUS node,
# so the trust gate is not the subject there).
#
# The node builds its client locally (nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's SmartHrClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).
#
# The default SmartHR v1 stub derives synthetic crew ids from a request hash
# ("crew-<digest8>"), so record evidence is asserted by shape (prefix +
# record_ref derivation), never by a retyped hash literal.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_smarthr_api_node import CallSmartHrApiNode
from src.services.smarthr_client import SmartHrApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_smarthr_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "smarthr_payload": to_json({"emp_code": "1001"}),
        "intent": "lookup_record",
        "employee_id": "1001",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-smarthr-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for SmartHrClient: lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def find_crew(self, emp_code, api_token):
        raise SmartHrApiError(403, "forbidden by integration permissions")


class _FakeEmptyClient:
    """Stands in for SmartHrClient: lookup returns no matching crew."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def find_crew(self, emp_code, api_token):
        return {"crews": []}


class _FakeLiveClient:
    """Stands in for SmartHrClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def find_crew(self, emp_code, api_token):
        _FakeLiveClient.captured = {"emp_code": emp_code, "api_token": api_token}
        return {"crews": [{"id": f"crew-{emp_code}", "emp_code": emp_code, "last_name": f"Employee {emp_code}"}]}


class TestCallSmartHrApiNode:
    def setup_method(self):
        self.node = CallSmartHrApiNode()

    def test_lookup_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free v1 stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"].startswith("crew-")
        assert result["record_ref"] == f"smarthr://crews/{result['record_id']}"
        assert result["employee_id"] == "1001"
        assert result["employee_name"] == "Employee 1001"

    def test_create_success_via_default_v1_stub(self):
        state = _state(
            intent="create_record",
            employee_id="9002",
            smarthr_payload=to_json({"emp_code": "9002", "last_name": "Alice"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"].startswith("crew-")
        assert result["record_ref"] == f"smarthr://crews/{result['record_id']}"

    def test_update_success_via_default_v1_stub(self):
        state = _state(
            intent="update_record",
            employee_id="1001",
            smarthr_payload=to_json({"emp_code": "1001"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"].startswith("crew-")

    def test_smarthr_config_state_field_sets_base_url(self):
        # The inner graph injects the manifest `smarthr:` section as the JSON
        # smarthr_config state field; the stub transport still serves the call.
        state = _state(smarthr_config=to_json({"base_url": "https://tenant.smarthr.example.test/api/v1"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"].startswith("smarthr://crews/")

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"smarthr": {"base_url": "https://tenant.smarthr.example.test/api/v1"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"].startswith("crew-")

    def test_missing_payload_errors(self):
        result = self.node(_state(smarthr_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_lookup_with_unresolved_code_errors(self):
        state = _state(employee_id="", smarthr_payload=to_json({"emp_code": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved employee code" in entry for entry in result["error_log"])

    def test_update_with_unresolved_code_errors(self):
        state = _state(
            intent="update_record",
            employee_id="",
            smarthr_payload=to_json({"emp_code": ""}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_record"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_smarthr_api_node.SmartHrClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])

    def test_lookup_no_matching_record_errors(self, monkeypatch):
        # SmartHR-specific branch: GET /crews returns an empty list -> a
        # well-formed status=error, never a fabricated record.
        monkeypatch.setattr("src.nodes.call_smarthr_api_node.SmartHrClient", _FakeEmptyClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("no employee record found" in entry for entry in result["error_log"])

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing SMARTHR_TOKEN is a hard error -
        # a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_smarthr_api_node.SmartHrClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_smarthr_api_node.SmartHrClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"SMARTHR_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["emp_code"] == "1001"

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_smarthr_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_smarthr_api_complete"]
        assert payload["intent"] == "lookup_record"
        assert payload["has_record_id"] is True
        assert payload["stub_transport"] is True


class TestCallDeadline:
    """The declared `timeout_s` must be a live consumer of the runtime config,
    not a dead declaration: a result that arrives after the deadline is
    discarded rather than surfaced."""

    def setup_method(self):
        self.node = CallSmartHrApiNode()

    def test_a_slow_call_is_discarded_at_the_deadline(self, monkeypatch):
        import time as _time

        class _SlowClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def find_crew(self, emp_code, api_token):
                _time.sleep(0.02)
                return {"crews": [{"id": "crew-slow", "emp_code": emp_code}]}

        monkeypatch.setattr("src.nodes.call_smarthr_api_node.SmartHrClient", _SlowClient)
        state = _state(smarthr_config=to_json({"timeout_s": 1}))
        # A 1s deadline is not exceeded by a 20ms call: the control proves the
        # deadline path is reached only when it should be.
        assert self.node(state)["status"] == AgentStatus.SUCCESS.value

        monkeypatch.setattr("src.nodes.call_smarthr_api_node._DEFAULT_TIMEOUT_S", 0)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("deadline" in entry for entry in result["error_log"])

    def test_an_unusable_declared_deadline_falls_back_to_the_default(self):
        for bad in ("NaN", float("inf"), 0, -5, 10**6, True):
            result = self.node(_state(smarthr_config=to_json({"timeout_s": bad})))
            assert result["status"] == AgentStatus.SUCCESS.value, bad

    def test_an_api_error_does_not_echo_the_upstream_body(self, monkeypatch):
        """An HR API error page can quote request content back; the status code
        is the actionable part."""
        monkeypatch.setattr("src.nodes.call_smarthr_api_node.SmartHrClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        joined = "\n".join(result["error_log"])
        assert "403" in joined
        assert "forbidden by integration permissions" not in joined
