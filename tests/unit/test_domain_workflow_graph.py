# CMN-C2-281 - Unit tests: inner SmartHRWorkflowGraph (BaseGraph) contract.
# The compiled outer path is exercised end-to-end by
# tests/proof_of_boundary/test_pb_invoke_order.py and
# tests/proof_of_boundary/test_pb_invoke_endpoint.py; this module unit-checks
# the inner graph's identity, config forwarding, the caller-context bridge,
# routing, output contract, and a direct inner invoke on the network-free stub.

from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import SmartHRWorkflowGraph
from src.schemas.state import State, from_json


def _graph(config=None):
    return SmartHRWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "smarthr_employee_data_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_smarthr_config_as_json():
    g = _graph({"configurable": {"smarthr": {"base_url": "https://tenant.smarthr.example.test/api/v1"}}})
    extra = g._extra_initial_state()
    # Forwarded as a JSON string, not a native dict (msgpack-safe).
    assert isinstance(extra["smarthr_config"], str)
    assert from_json(extra["smarthr_config"], {}) == {"base_url": "https://tenant.smarthr.example.test/api/v1"}


def test_extra_initial_state_empty_without_smarthr_section():
    assert _graph()._extra_initial_state() == {}


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"record_id": "crew-1001", "record_ref": "smarthr://crews/crew-1001", "confirmation": "ok"},
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_record",
            "employee_id": "1001",
            "record_id": "crew-1001",
            "record_ref": "smarthr://crews/crew-1001",
            "employee_name": "Employee 1001",
            "confirmation": "ok",
            "smarthr_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "lookup_record"
    assert out["record_ref"] == "smarthr://crews/crew-1001"
    assert out["confirmation"] == "ok"
    assert out["output"] == {"record_id": "crew-1001", "record_ref": "smarthr://crews/crew-1001", "confirmation": "ok"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_lookup_on_v1_stub():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node declares
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm."""
    g = _graph({"configurable": {"smarthr": {"base_url": "https://app.smarthr.jp/api/v1"}}})
    g.compile()
    result = g.invoke(user_input="Look up the employee record for employee code 1001 and summarize the record on file.")
    assert result["status"] == AgentStatus.SUCCESS.value
    # The v1 stub derives synthetic crew ids ("crew-<digest8>") - assert by
    # shape + record_ref derivation, never a retyped hash literal.
    assert result["record_id"].startswith("crew-")
    assert result["record_ref"] == f"smarthr://crews/{result['record_id']}"
    assert result["intent"] == "lookup_record"
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferSmartHrFieldsNode",
        "CallSmartHrApiNode",
        "ConfirmNode",
    ]


def test_extra_initial_state_merges_the_call_deadline():
    g = _graph({"configurable": {"smarthr": {"base_url": "https://app.smarthr.jp/api/v1"}, "timeout_s": 12}})
    assert from_json(g._extra_initial_state()["smarthr_config"], {})["timeout_s"] == 12


def test_extra_initial_state_seeds_the_caller_context_bridge():
    """GraphNode does not forward input_context into the subgraph, so the
    validated caller data crosses the boundary through the ContextVar bridge
    and is seeded here."""
    from src.graph.context_bridge import set_caller_employee_context

    set_caller_employee_context({"employee_hint": "7007", "employee": {"name": "Taro Yamada"}})
    try:
        extra = _graph()._extra_initial_state()
        assert extra["employee_hint"] == "7007"
        assert from_json(extra["caller_employee"], {}) == {"name": "Taro Yamada"}
    finally:
        set_caller_employee_context(None)


def test_bridge_is_empty_when_no_caller_data_was_stashed():
    from src.graph.context_bridge import set_caller_employee_context

    set_caller_employee_context(None)
    extra = _graph()._extra_initial_state()
    assert "employee_hint" not in extra
    assert "caller_employee" not in extra


def test_route_is_annotated_with_this_graphs_own_state():
    """LangGraph reads a path callable's annotation as its input schema and
    projects away every field the annotation does not declare, so a base-state
    annotation would hand route() a state with the domain fields missing."""
    import typing

    assert typing.get_type_hints(SmartHRWorkflowGraph.route)["state"] is State
