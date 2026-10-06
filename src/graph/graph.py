"""AgentCore Platform v1.0 - CMN-C2-281 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in SmartHRWorkflowGraphNode (`main`
slot), which wraps the inner SmartHRWorkflowGraph (validate -> classify -> infer ->
call -> confirm). add_edges() is NOT overridden - backbone wiring is the
framework's concern.
"""

from pathlib import Path
from typing import Any, ClassVar

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from src.graph.context_bridge import set_caller_employee_context
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State, from_json

# Repo-root runtime config: src/graph/graph.py -> parents[2] = repo root.
# config/agent.yaml is the static registry manifest (flat, every key at root
# level - no `agent:` block); runtime parameters (max_retry, timeout_s) and the
# `smarthr` integration section live in config/config.yaml, which is what the
# graph reads at run time.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


class SmartHRWorkflowGraphNode(GraphNode):
    """Wraps the inner SmartHR workflow graph; assigned to the `main` slot.

    No constructor arguments (nodes are no-arg) - configuration reaches the
    subgraph via _parent_config(), which loads the runtime config.
    """

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> "Any":
        from src.graph.domain_workflow_graph import SmartHRWorkflowGraph

        return SmartHRWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # Hand the validated caller employee data across the graph boundary
        # (set before subgraph.invoke; the inner _extra_initial_state() reads
        # it). The data must not ride only inside the validated_input JSON: the
        # framework's PII mask rewrites that field at node boundaries and real
        # employee data (a display name written as two Title Case words such as
        # "Taro Yamada") trips the masking heuristics - see context_bridge.py.
        set_caller_employee_context(
            {
                "employee_hint": str(state.get("employee_hint") or ""),
                "employee": from_json(state.get("caller_employee"), {}) or {},
            }
        )
        # pre_process serialized the request into validated_input (JSON string);
        # structured params travel as JSON and the first inner node parses them back.
        return str(state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: "dict[str, Any]") -> "dict[str, Any]":
        # Map only the keys this node changes back into the outer state.
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "employee_id": sub_result.get("employee_id", ""),
            "record_id": sub_result.get("record_id", ""),
            "record_ref": sub_result.get("record_ref", ""),
            "employee_name": sub_result.get("employee_name", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "smarthr_payload": sub_result.get("smarthr_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> "dict[str, Any]":
        """Forward the runtime config to the inner graph under config["configurable"].

        Loads config/config.yaml and forwards the `smarthr:` integration
        section, the `llm:` section when declared (the documented
        LLM-synthesis follow-up) and the SmartHR-call deadline `timeout_s` -
        never an empty {} (an empty _parent_config() would silently make every
        declared setting dead). `max_retry` is not re-forwarded: it is consumed
        by the outer graph itself, which the framework run loop reads from the
        config passed to the graph constructor.
        """
        try:
            runtime = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            # Missing/unreadable config: inner nodes fall back to safe
            # defaults (documented network-free stub client).
            runtime = {}
        configurable: "dict[str, Any]" = {}
        for key in ("smarthr", "llm"):
            if key in runtime:
                configurable[key] = runtime[key]
        if "timeout_s" in runtime:
            configurable["timeout_s"] = runtime["timeout_s"]
        return {"configurable": configurable}


class SmartHREmployeeDataAgent(AgentBaseGraph):
    """CMN-C2-281 outer graph - SmartHR Employee Data Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in SmartHRWorkflowGraphNode (`main` slot); SmartHR
    settings flow from config/config.yaml via _parent_config().
    """

    @property
    def name(self) -> str:
        return "cmn_c2_281"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = SmartHRWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.
