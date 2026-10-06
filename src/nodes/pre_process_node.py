"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: serialize the caller's request (raw NL text + validated
employee data) into a single JSON string in `validated_input`, which the
GraphNode (`main` slot) hands to the inner SmartHR workflow graph. Business
validation happens inside the inner graph's ValidateInputNode - this node owns
the CALLER-DATA CONTRACT: the empty-guard, the HTML/length sanitize, the
template-owned injection screen, and the field-by-field validation of
`input_context` (every accepted value is bounded; a malformed value is refused
with an error that names the field and never echoes the value).

Why input_context carries the employee data: the pipeline's other input channel
(the request text serialized into validated_input) is rewritten by the
framework's PII masking heuristics at every node boundary - a display name
written as two Title Case words ("Taro Yamada") arrives at the SmartHR call as
"[MASKED]". Structured caller data therefore travels through input_context,
which is not masked, and this node screens that channel itself: prompt-injection
content (post-parse, keys included, both raw and after the markup strip) is
refused, and every value is held to a bounded, inert shape before it can render
into the confirmation the caller reads back or into the SmartHR request body.
"""

import json
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import (
    CODE_NUMBER_MAX,
    CODE_NUMBER_MIN,
    EMPLOYEE_CODE_RE,
    EMPLOYEE_LABEL_RE,
    FIELD_KEY_RE,
    FIELD_NUMBER_MAX,
    FIELD_NUMBER_MIN,
    FIELD_VALUE_RE,
    MAX_EMPLOYEE_FIELDS,
    finite_int_in_range,
    is_non_finite_spelling,
    sanitize_query,
    screen_text,
)

# input_context keys that may carry an explicit target employee (caller-supplied
# only - a target is never inferred here). First present key wins.
_HINT_KEYS = ("employee_id", "employee_hint", "employee_code")

# The structured employee contract: input_context["employee"] may carry these
# fields, each bounded below. An unknown field inside `employee` is refused
# (silently dropping a misspelled field would write a record missing data the
# caller supplied); the refusal names the container, never the unknown key
# itself (a field NAME is caller-controlled text too).
_EMPLOYEE_KEYS = ("name", "fields")


def _iter_context_strings(value: Any) -> "list[str]":
    """Every string in a parsed input_context - keys AND values, at any depth.

    The injection screen runs post-parse over this list, so a payload hidden in
    a mapping KEY, nested one level down, or JSON-escaped on the wire (parsed
    back to its real characters by then) is screened exactly like a top-level
    value.
    """
    found: "list[str]" = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found.append(key)
            found.extend(_iter_context_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_iter_context_strings(item))
    return found


class PreProcessNode(FunctionNode):
    """Validate + serialize caller input for the inner workflow graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner SmartHR call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted (ANONYMOUS) caller is denied at this gate
    # before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not user_input or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # Caller context must be a mapping; anything else is refused without
        # being echoed (fail closed - never guess at a malformed contract).
        if input_context is None:
            input_context = {}
        if not isinstance(input_context, dict):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: input_context must be an object"],
            }

        # Template-owned injection screen. Both channels are screened RAW and
        # again after the markup strip, and input_context is screened
        # post-parse over every key and string value at any depth. Refusals
        # name the pattern type only - hostile content is never echoed.
        findings = screen_text(user_input)
        if findings:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: user_input contains disallowed content ({findings[0]})"],
            }
        for text in _iter_context_strings(input_context):
            findings = screen_text(text)
            if findings:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"PreProcessNode: input_context contains disallowed content ({findings[0]})"],
                }

        # Strip HTML markup + cap length before JSON serialization.
        sanitized_input = sanitize_query(user_input.strip())

        # Target employee is caller-supplied and never inferred here: an
        # explicit code from input_context, validated against the bounded inert
        # shape. A present-but-malformed value (wrong type, wrong charset,
        # over-length) is a hard error naming the FIELD, never the value -
        # silently dropping it could read or amend a different employee's
        # record than the caller intended.
        employee_hint, problem = self._validate_hint(input_context)
        if problem:
            return {"status": AgentStatus.ERROR.value, "error_log": [problem]}

        # Structured employee data: validated field-by-field against explicit
        # bounds; the display name and every employee-data field must match the
        # inert render charset, and a numeric field value goes through the
        # finite+bounded number parser. Absent data degrades to the
        # text-inference baseline in the inner graph.
        caller_employee, problem = self._validate_employee(input_context.get("employee"))
        if problem:
            return {"status": AgentStatus.ERROR.value, "error_log": [problem]}

        validated_input = json.dumps({"text": sanitized_input, "employee_hint": employee_hint})

        # Audit the shaped request - presence signals only, not the raw text.
        emit_trace_event(
            "pre_process_complete",
            {"has_employee_hint": bool(employee_hint), "has_caller_employee": bool(caller_employee)},
            state,
        )

        return {
            "validated_input": validated_input,
            "employee_hint": employee_hint,
            "caller_employee": to_json(caller_employee) if caller_employee else None,
            "status": AgentStatus.SUCCESS.value,
        }

    # -- caller contract validation -------------------------------------------

    def _validate_hint(self, input_context: "dict[str, Any]") -> "tuple[str, str]":
        """Resolve the caller-supplied employee code -> (code, "" | problem).

        A code delivered as a JSON NUMBER goes through the finite+bounded
        parser before it is rendered as a string: NaN and Infinity parse
        cleanly through float() and arrive intact in a raw JSON body, so an
        unchecked numeric code would reach the SmartHR request as "nan".
        """
        for key in _HINT_KEYS:
            value = input_context.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or isinstance(value, (int, float)):
                bounded = finite_int_in_range(value, CODE_NUMBER_MIN, CODE_NUMBER_MAX)
                if bounded is None:
                    return "", (
                        f"PreProcessNode: input_context.{key} must be a finite whole number "
                        f"between {CODE_NUMBER_MIN} and {CODE_NUMBER_MAX}"
                    )
                value = str(bounded)
            if not isinstance(value, str):
                return "", f"PreProcessNode: input_context.{key} must be a string"
            value = value.strip()
            if not value:
                continue  # blank = absent
            if not EMPLOYEE_CODE_RE.match(value):
                return "", f"PreProcessNode: input_context.{key} is not a valid employee code"
            return value, ""
        return "", ""

    def _validate_employee(self, employee: Any) -> "tuple[dict[str, Any], str]":
        """Validate input_context.employee -> (accepted fields, "" | problem).

        Fail closed: wrong container type, an unsupported field, a wrong-typed
        or over-bound value, a label outside the inert render charset, too many
        employee-data fields, or a non-finite / out-of-range numeric field
        value each refuse with a field-naming error. Values are never echoed;
        the unsupported-field refusal does not echo the key either (a field
        NAME is caller-controlled text too).
        """
        if employee is None:
            return {}, ""
        if not isinstance(employee, dict):
            return {}, "PreProcessNode: input_context.employee must be an object"
        if [key for key in employee if key not in _EMPLOYEE_KEYS]:
            return {}, "PreProcessNode: input_context.employee contains an unsupported field"

        accepted: "dict[str, Any]" = {}

        name = employee.get("name")
        if name is not None:
            if not isinstance(name, str):
                return {}, "PreProcessNode: input_context.employee.name must be a string"
            name = name.strip()
            if name:
                if not EMPLOYEE_LABEL_RE.match(name):
                    return {}, (
                        "PreProcessNode: input_context.employee.name must be at most 100 letters, "
                        "digits, spaces, hyphens, dots, commas or parentheses"
                    )
                accepted["name"] = name

        fields, problem = self._validate_fields(employee.get("fields"))
        if problem:
            return {}, problem
        if fields:
            accepted["fields"] = fields

        return accepted, ""

    def _validate_fields(self, fields: Any) -> "tuple[list[dict[str, str]], str]":
        """Validate input_context.employee.fields -> ([{name, value}, ...], "" | problem)."""
        if fields is None:
            return [], ""
        if not isinstance(fields, dict):
            return [], "PreProcessNode: input_context.employee.fields must be an object"
        if len(fields) > MAX_EMPLOYEE_FIELDS:
            return [], (
                f"PreProcessNode: input_context.employee.fields carries more than " f"{MAX_EMPLOYEE_FIELDS} entries"
            )

        accepted: "list[dict[str, str]]" = []
        for key, value in fields.items():
            if not isinstance(key, str) or not FIELD_KEY_RE.match(key.strip()):
                return [], "PreProcessNode: input_context.employee.fields carries an invalid field name"
            if isinstance(value, bool) or isinstance(value, (int, float)):
                bounded = finite_int_in_range(value, FIELD_NUMBER_MIN, FIELD_NUMBER_MAX)
                if bounded is None:
                    return [], (
                        "PreProcessNode: a numeric input_context.employee.fields value must be a "
                        f"finite whole number between {FIELD_NUMBER_MIN} and {FIELD_NUMBER_MAX}"
                    )
                value = str(bounded)
            if not isinstance(value, str):
                return [], "PreProcessNode: an input_context.employee.fields value must be a string or a number"
            value = value.strip()
            if not value:
                continue  # blank = absent
            if is_non_finite_spelling(value):
                # A label is fine; a stored NaN/Infinity is not. See
                # is_non_finite_spelling() for why this one string form is refused.
                return [], ("PreProcessNode: an input_context.employee.fields value must not be a " "non-finite number")
            if not FIELD_VALUE_RE.match(value):
                return [], (
                    "PreProcessNode: an input_context.employee.fields value must be at most 200 "
                    "letters, digits, spaces, hyphens, dots, commas or parentheses"
                )
            accepted.append({"name": key.strip(), "value": value})
        return accepted, ""
