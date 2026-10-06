"""AgentCore Platform v1.0 - caller-context bridge across the graph boundary."""

# Why this exists: GraphNode.execute() invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)` and does NOT forward
# outer state fields or the caller's input_context, so the validated employee
# data collected by PreProcessNode (the employee code and the structured
# employee-data fields) would never reach the inner workflow on its own. The
# sanctioned subclass hooks bridge it:
#
#   SmartHRWorkflowGraphNode.extract_input(state)  [runs BEFORE subgraph.invoke]
#       -> set_caller_employee_context({"employee_hint": ..., "employee": ...})
#   SmartHRWorkflowGraph._extra_initial_state()    [runs INSIDE subgraph.invoke]
#       -> seeds {"employee_hint": ..., "caller_employee": <JSON>}
#
# What crosses the bridge is the VALIDATED caller contract produced by
# PreProcessNode - the code already passed the bounded, inert shape check and
# every employee-data field passed its own bounds - never the raw request body.
#
# The alternative, smuggling the data inside the validated_input JSON, is not
# reliable here: the framework masks that field at node boundaries, and real
# employee data trips the masking heuristics - a display name written as two
# Title Case words ("Taro Yamada") is rewritten to "[MASKED]" and a long
# numeric code is rewritten as a digit group, so the SmartHR write would carry
# corrupted caller data. This channel is not masked.
#
# A ContextVar keeps the hand-off correct per thread/task, so concurrent
# invocations in one process cannot see each other's employee data.

from contextvars import ContextVar
from typing import Any

_CALLER_EMPLOYEE_CONTEXT: ContextVar["dict[str, Any] | None"] = ContextVar(
    "cmn_c2_281_caller_employee_context", default=None
)


def set_caller_employee_context(employee_context: "dict[str, Any] | None") -> None:
    """Stash the validated caller employee data for the imminent inner-graph invoke."""
    _CALLER_EMPLOYEE_CONTEXT.set(dict(employee_context) if employee_context else {})


def get_caller_employee_context() -> "dict[str, Any]":
    """Read (without consuming) the stashed employee data; {} when none was set."""
    return _CALLER_EMPLOYEE_CONTEXT.get() or {}
