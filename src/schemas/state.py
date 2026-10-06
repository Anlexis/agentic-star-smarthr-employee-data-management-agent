"""AgentCore Platform v1.0 - CMN-C2-281 SmartHR Employee Data Agent state."""

# State must be a flat TypedDict - never a Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization; Pydantic objects (and nested dict/list
# containers) are not msgpack-safe. Extend AgentState with agent-specific fields
# only, and declare every domain field NotRequired[...] (fields are absent until
# their producer node writes them). smarthr_payload / smarthr_config /
# caller_employee / redaction_flags are dicts/lists at the point of use but are
# stored in State as JSON strings via to_json/from_json below. Do NOT add
# credentials, secrets, or Pydantic models. The SmartHR integration token is
# NEVER stored here - it is read via ctx.secrets in CallSmartHrApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact, msgpack-safe JSON string.

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """SmartHR Employee Data agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only SmartHR-workflow fields are added below, all NotRequired (the state
    contract). All values are JSON/msgpack-serializable primitives - the
    SmartHR integration token is NEVER stored here (accessed via ctx.secrets).
    """

    # Caller-supplied target employee code, validated by PreProcessNode against
    # the bounded inert shape. Never inferred; resolution to a SmartHR employee
    # code is explicit-only (the request text may also carry one).
    employee_hint: NotRequired[str]
    employee_id: NotRequired[str]  # resolved SmartHR employee code (emp_code)

    # The caller's VALIDATED structured employee data (display name +
    # employee-data fields), carried across the graph boundary by
    # src/graph/context_bridge.py because GraphNode does not forward
    # input_context into the subgraph. JSON string (msgpack-safe).
    caller_employee: NotRequired[Optional[str]]

    # ValidateInput (deterministic content scan)
    # JSON list[str] of patterns redacted from the text before logging
    # (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferSmartHrFields
    employee_name: NotRequired[str]  # employee display name / record label
    # JSON - assembled SmartHR REST API v1 request body (stored as a JSON
    # string, not a native dict; (de)serialize via to_json/from_json).
    smarthr_payload: NotRequired[Optional[str]]

    # Runtime `smarthr:` section forwarded by _parent_config() and injected
    # by the inner graph's _extra_initial_state() (JSON string).
    smarthr_config: NotRequired[Optional[str]]

    # CallSmartHrApi
    record_id: NotRequired[str]  # crew id / employee code returned by SmartHR
    record_ref: NotRequired[str]  # human-readable reference (smarthr://crews/<id>)

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
