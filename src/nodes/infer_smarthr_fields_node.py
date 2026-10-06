"""AgentCore Platform v1.0 - inner workflow Step 3: InferSmartHrFields.

Assembles the SmartHR REST API v1 request body for the classified intent from
two channels:

  * the caller-data contract - the employee code, display name and
    "name/value" employee-data fields the caller supplied through
    input_context, already validated field-by-field by PreProcessNode and
    carried into this graph unmasked by the context bridge;
  * the request text - the same entities recovered from the (redacted, masked)
    natural-language request, used when the caller supplied nothing.

The structured channel wins where both carry a value: it is the caller's
unambiguous instruction, while the text channel is a heuristic read of a string
the framework has already rewritten to mask personal data. The employee code is
taken only from one of those two explicit sources - an unresolved code is left
empty rather than invented (risk mitigation: never touch the wrong employee
record; the executor surfaces the miss as status=error). Deterministic - no
model call (docs/02_design.md, "Implementation Note").
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json

# A SmartHR employee code (emp_code): short alphanumeric identifier (no spaces).
_CODE_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
# Explicit code mention in the request text, EN or JA
# ("employee code 1001" / "staff id: A123" / "社員番号 1001").
_CODE_IN_TEXT_RE = re.compile(
    r"(?:employee|member|staff|crew)\s+(?:code|id|number)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})"
    r"|(?:社員番号|社員コード|従業員番号)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})",
    re.IGNORECASE,
)
# Quoted display name: named "Foo" / called "Foo". Curly quotes as \u escapes so
# the source stays pure ASCII (push-safe).
_NAME_QUOTED_RE = re.compile(r'(?:named|called|for)\s+["“]([^"”\n]+)["”]', re.IGNORECASE)
# "Key: value" employee-data field lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs, as \u escapes (push-safe).
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
# Keys that are the code/name themselves, not custom employee-data fields.
_CODE_KEYS = ("code", "employee code", "member code", "id")
_NAME_KEYS = ("name", "employee name", "member name")


class InferSmartHrFieldsNode(FunctionNode):
    """Extract entities and assemble the SmartHR REST API v1 request body."""

    # Inner domain node - derives fields from already-validated text and the
    # already-validated caller contract; the external gate lives on the outer
    # backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "lookup_record") or "lookup_record"
        employee_hint = state.get("employee_hint", "") or ""
        caller = from_json(state.get("caller_employee"), {}) or {}

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferSmartHrFieldsNode: missing validated_input"],
            }

        text_fields = self._parse_fields(text)
        caller_fields = self._caller_fields(caller)
        fields = self._merge_fields(caller_fields, text_fields)
        employee_id = self._resolve_code(text, employee_hint, text_fields)
        employee_name = self._resolve_name(text, caller, text_fields)

        if intent in ("create_record", "update_record"):
            payload = self._build_crew_payload(employee_id, employee_name, fields)
        else:  # lookup_record (read-only default)
            payload = {"emp_code": employee_id}

        # Audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "infer_smarthr_fields_complete",
            {
                "intent": intent,
                "has_employee_id": bool(employee_id),
                "n_fields": len(fields),
                "n_caller_fields": len(caller_fields),
            },
            state,
        )

        return {
            "employee_id": employee_id,
            "employee_name": employee_name,
            "smarthr_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _resolve_code(self, text: str, employee_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit code only: caller-supplied code > text mention > 'Code:' field.

        The caller-supplied value comes first because it arrived through the
        validated, unmasked contract channel; the text mention is a heuristic
        read of a string the framework has already rewritten. Never invented.
        """
        hint = employee_hint.strip()
        if hint and _CODE_SHAPE_RE.match(hint):
            return hint
        match = _CODE_IN_TEXT_RE.search(text)
        if match:
            return match.group(1) or match.group(2) or ""
        for key, value in fields:
            if key.strip().lower() in _CODE_KEYS and _CODE_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_name(self, text: str, caller: "dict[str, Any]", fields: "list[tuple[str, str]]") -> str:
        caller_name = str(caller.get("name") or "").strip()
        if caller_name:
            return caller_name[:100]
        match = _NAME_QUOTED_RE.search(text)
        if match:
            return match.group(1).strip()[:100]
        for key, value in fields:
            if key.strip().lower() in _NAME_KEYS:
                return value.strip()[:100]
        return ""

    def _caller_fields(self, caller: "dict[str, Any]") -> "list[tuple[str, str]]":
        """The validated employee-data fields supplied through input_context."""
        entries = caller.get("fields") or []
        if not isinstance(entries, list):
            return []
        pairs: "list[tuple[str, str]]" = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "") or "").strip()
            value = str(entry.get("value", "") or "").strip()
            if name and value:
                pairs.append((name, value))
        return pairs

    @staticmethod
    def _merge_fields(
        caller_fields: "list[tuple[str, str]]", text_fields: "list[tuple[str, str]]"
    ) -> "list[tuple[str, str]]":
        """Caller-supplied fields first; a text field with the same name is dropped."""
        merged = list(caller_fields)
        seen = {name.strip().lower() for name, _ in caller_fields}
        for name, value in text_fields:
            if name.strip().lower() in seen:
                continue
            merged.append((name, value))
        return merged

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] employee-data fields parsed from the request lines."""
        fields: "list[tuple[str, str]]" = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            match = _KV_RE.match(stripped)
            if match:
                fields.append((match.group(1).strip(), match.group(2).strip()))
        return fields

    # -- payload assembly (SmartHR REST API v1 crew shape) ---------------------

    def _build_crew_payload(
        self, employee_id: str, employee_name: str, fields: "list[tuple[str, str]]"
    ) -> "dict[str, Any]":
        # SmartHR crew attributes: emp_code + name. The extracted display name
        # is carried under last_name (SmartHR splits family/given names; a live
        # adapter may split further). Extra employee-data fields ride as
        # name-keyed custom_fields entries - a live adapter maps them to SmartHR
        # custom-field template ids (documented note, docs/02).
        record: "dict[str, Any]" = {"emp_code": employee_id}
        if employee_name:
            record["last_name"] = employee_name
        custom_fields = []
        for key, value in fields:
            if key.strip().lower() in _CODE_KEYS + _NAME_KEYS:
                continue
            custom_fields.append({"name": key, "value": value})
        if custom_fields:
            record["custom_fields"] = custom_fields
        return record
