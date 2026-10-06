"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner SmartHR workflow
graph has run. GraphNode.merge_output() maps the inner result into the outer
state; this node shapes the caller-facing `formatted_output`.

The domain output gate is the MODULE-LEVEL `_security_gate_output()` below,
called from execute(). It is deliberately NOT an instance method and NOT the
framework `_extra_security_gate_output` hook - the framework gate methods are
@final on FunctionNode and the real SDK auto-wraps `_extra_` hooks (which
breaks the .invoke() chain), so domain checks live in a module-level helper
invoked inline.

The stated output invariant (docs/02_design.md, "Output contract") is this
template's own; there is no rounding grid to enforce, because the agent reports
employee-record identifiers and labels, not monetary aggregates. What it does
enforce, for every representation:

  1. the caller-facing output carries EXACTLY the declared keys - a SmartHR
     response object is projected into them, never spread wholesale, so bulk
     record data cannot ride out on a field nobody declared;
  2. a SUCCESS response carries record evidence (record_id / record_ref) -
     otherwise it would misrepresent the outcome of a write against a real HR
     system;
  3. no credential-shaped string appears ANYWHERE in the output, including
     strings nested inside the assembled SmartHR request body (a mapping, with
     a list of employee-data fields inside it) and the mapping KEYS themselves.
     A top-level-only scan reports zero findings on exactly the case that
     matters.

EVERY non-success return - a gate violation AND a pre-existing inner-workflow
error - goes through the one module-level `_contain()` helper. It CLEARS every
output-bearing state field instead of merely returning an error status: the
base graph's output shaping falls back to `state["result"]` when
`formatted_output` is absent or falsy, so a path that only raised - or that
returned an error without clearing - would still ship the un-gated inner
answer inside the error envelope. Containment means the answer is gone, not
merely relabelled, and the replacement `formatted_output` is a TRUTHY mapping
so the fallback never fires.

What the ERROR envelope may say: closed-set labels only. It carries a constant
reason code chosen by this module (one of `ERROR_REASONS`) and nothing else -
never `error_log`, never the gate's violation entries, never any other
node-authored text. Those lines can embed an upstream SmartHR response body,
identifiers, names or caller-derived fragments, and truncating or redacting
them is not a closed set. `error_log` stays the INTERNAL channel: the state
reducer appends to it and the audit trail needs it; it is simply never
projected to the caller. Gate violations are written to `error_log` naming the
offending PATH (fixed keys and indices, never the value; a credential-shaped
key is withheld from the label), and the audit event carries a count.

No error envelope carries SmartHR record evidence either. `record_id` /
`record_ref` are this agent's WRITE EVIDENCE - the SUCCESS branch of the gate
above REFUSES an output that lacks them - so returning them under an ERROR
status would tell a caller being informed of failure that an employee record
was nonetheless written, and which one. SmartHR is an HR system: the crew id,
the employee code and the employee name are personal data.
"""

import re
from typing import Any, Iterator

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

# Credential-shaped strings that must never reach the caller (defence in depth -
# the framework's own output-side credential scan in FunctionNode also runs on
# every result).
_CREDENTIAL_LIKE_RE = re.compile(r"eyJ[A-Za-z0-9._-]{10,}|sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{16,}")

# Stand-in for a mapping key that cannot itself be written into a path label.
# The label travels in error_log, where the framework's own credential scan
# would raise on a credential-shaped key and replace the cleared result with a
# bare error - re-opening the `result` fallback the clearing closed.
_UNNAMEABLE_KEY = "<withheld>"

# Reason codes - the ONLY values the caller-visible ERROR envelope may carry.
# Chosen here, never derived from state, so the envelope is a closed set: it
# says WHAT happened, never to which record and never in whose words.
_REASON_WORKFLOW_FAILED = "smarthr_workflow_failed"  # the inner workflow reported an error
_REASON_OUTPUT_WITHHELD = "output_withheld_by_gate"  # the output gate refused the response
ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})

# The declared caller-facing response keys. The gate refuses anything else, so
# a future change that spreads a SmartHR response object into the output fails
# here instead of shipping the whole employee record.
_DECLARED_OUTPUT_KEYS = frozenset(
    {"record_id", "record_ref", "employee_name", "intent", "confirmation", "smarthr_payload"}
)

# Every state field that can carry released answer text. On EVERY non-success
# return all of them are cleared, so nothing downstream can fall back to one of
# them - and the record evidence cannot be recovered out of state by a
# checkpoint or a downstream reader after the caller has been told the
# operation failed.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "confirmation",
    "record_id",
    "record_ref",
    "employee_id",
    "employee_name",
    "smarthr_payload",
    # Rendered in the caller-facing success envelope and merged into the outer
    # state by SmartHrWorkflowGraphNode.merge_output() like the fields above.
    "intent",
)


def _cleared_output_state() -> "dict[str, Any]":
    """The blanked value for every output-bearing field, as a state delta.

    Shared by BOTH non-success returns - the gate violation and the
    pre-existing inner-workflow error - because both must leave the same
    nothing behind.
    """
    return {field: (None if field in ("result", "smarthr_payload") else "") for field in _OUTPUT_BEARING_FIELDS}


def _contain(reason: str, new_errors: "list[str] | None" = None) -> "dict[str, Any]":
    """The node result for ANY non-success outcome - the single error shape.

    Error status, every output-bearing field cleared, and an envelope made of
    closed-set labels only: `reason` is one of ERROR_REASONS. `new_errors`
    (gate violations - path labels only) go to `error_log`, the internal
    channel the state reducer accumulates, and never enter the envelope.
    Nothing is read out of state: not the record, not `error_log` - the inner
    entries are already there, and re-emitting them would duplicate every
    line.

    The constant `reason` key keeps the mapping TRUTHY, so the framework's
    `formatted_output or result` projection (AgentBaseGraph.get_output()
    applies no status check) serves this envelope and never whatever survived
    in `result`.
    """
    contained: "dict[str, Any]" = _cleared_output_state()
    contained["formatted_output"] = {"reason": reason}
    contained["status"] = AgentStatus.ERROR.value
    if new_errors:
        contained["error_log"] = list(new_errors)
    return contained


def _iter_strings(value: Any, path: str) -> "Iterator[tuple[str, str]]":
    """Yield (path, string) for EVERY string in a nested structure - values AND mapping keys.

    Walks dicts, lists and tuples so a value nested inside the SmartHR request
    body (e.g. formatted_output["smarthr_payload"]["custom_fields"][0]["value"])
    is scanned exactly like a top-level field. A mapping key is yielded for
    scanning too, and when the key is credential-shaped the path label built
    from it is withheld (_UNNAMEABLE_KEY) so the violation that reports it
    cannot repeat it.
    """
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                label = _UNNAMEABLE_KEY if _CREDENTIAL_LIKE_RE.search(key) else key
                key_path = f"{path}.{label}" if path else label
                yield key_path, key
            else:
                key_path = f"{path}.<key>" if path else "<key>"
            yield from _iter_strings(item, key_path)
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            yield from _iter_strings(item, f"{path}[{idx}]")


def _security_gate_output(formatted_output: "dict[str, Any]", is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Returns the list of violations ([] = the output may ship). Violations name
    the field PATH, never the value. They are error_log entries (internal) and
    never reach the caller.
    """
    problems: "list[str]" = []
    if set(formatted_output) - _DECLARED_OUTPUT_KEYS:
        problems.append("PostProcess output gate: response carries a field outside the declared output contract")
    if is_success and not (formatted_output.get("record_id") or formatted_output.get("record_ref")):
        problems.append("PostProcess output gate: SUCCESS output missing record_id/record_ref evidence")
    for path, value in _iter_strings(formatted_output, ""):
        if _CREDENTIAL_LIKE_RE.search(value):
            problems.append(f"PostProcess output gate: credential-like value in formatted_output['{path}']")
    return problems


class PostProcessNode(FunctionNode):
    """Format the final agent output."""

    # Read-only formatting of the already-produced result - default permissive;
    # the external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        # If the inner workflow errored, preserve the error status (do not mask
        # it) and publish NOTHING of it. Under the real pipeline
        # BaseNode.__call__ short-circuits on an errored state before execute()
        # runs, so this branch is defence in depth for direct invocation - and
        # it must still be a closed set: error_log already carries the inner
        # entries (the state reducer appends, so re-emitting them here would
        # duplicate every line) and the caller receives the reason code only.
        # The delta clears every output-bearing field, so the record evidence
        # cannot be recovered from the checkpoint or by a downstream reader.
        if state.get("status") == AgentStatus.ERROR.value:
            # Outcome signals only - a closed-set reason code and a count. The
            # audit log is not a store for HR record content or error text.
            emit_trace_event(
                "post_process_error_contained",
                {"reason": _REASON_WORKFLOW_FAILED, "error_count": len(state.get("error_log") or [])},
                state,
            )
            return _contain(_REASON_WORKFLOW_FAILED)

        formatted_output = {
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "employee_name": state.get("employee_name", ""),
            "intent": state.get("intent", ""),
            "confirmation": state.get("confirmation", ""),
            "smarthr_payload": from_json(state.get("smarthr_payload"), {}),
        }

        # Domain output gate (module-level helper - see module docstring). A
        # refusal is contained the same way as an inner error: the violations
        # go to error_log only, the caller receives the reason code only.
        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            # A refusal is a decision, so it gets its own audit event - a block
            # that leaves no trace is indistinguishable from a request that was
            # never made. Counts and the violated rule only, never the values.
            emit_trace_event(
                "post_process_gate_blocked",
                {
                    "reason": _REASON_OUTPUT_WITHHELD,
                    "intent": state.get("intent", ""),
                    "violation_count": len(violations),
                    "missing_record_evidence": not (
                        formatted_output.get("record_id") or formatted_output.get("record_ref")
                    ),
                },
                state,
            )
            return _contain(_REASON_OUTPUT_WITHHELD, violations)

        # Audit the final response shaping - outcome signals only, no payload content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_record_id": bool(state.get("record_id")),
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
