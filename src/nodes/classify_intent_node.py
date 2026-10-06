"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of lookup_record / create_record
/ update_record. A deterministic keyword heuristic is always computed first
(so the template is testable and runnable without a model call, and the
pipeline never blocks on an LLM outage); an Azure OpenAI call then attempts
to override it with a genuine NL-reasoning classification. Any LLM failure
(missing secret, API error, malformed response) silently keeps the keyword
result - never raises, never sets status=error. Low-confidence / unknown
keyword signal falls back to the read-only "lookup_record" default with a
note - never a write.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.services.llm.azure_openai_client import AzureOpenAIClient
from shared.utils.audit_logger import emit_trace_event
from shared.utils.llm_json import extract_json_object

_VALID_INTENTS = ("lookup_record", "create_record", "update_record")

_LLM_SYSTEM_PROMPT = (
    "You are an intent classifier for a SmartHR employee-data assistant. Given a "
    "user's natural-language request, classify it into exactly one of: "
    "lookup_record, create_record, update_record. Respond with a single JSON "
    'object only, no prose, no markdown fences: {"intent": '
    '"lookup_record"|"create_record"|"update_record"}. If the request is '
    "genuinely ambiguous or unclear, prefer lookup_record - the read-only, "
    "safest default. Never classify a request as create_record or "
    "update_record without a clear signal that a write was requested."
)

# Deterministic keyword signals (checked in priority order, writes first so a
# "update the record then show it" style request classifies as the write).
_KEYWORDS = (
    (
        "update_record",
        (
            "update",
            "change",
            "edit",
            "correct",
            "amend",
            "revise",
            "transfer",
            "promote",
            "promotion",
            "set the",
            "set her",
            "set his",
            "更新",
            "変更",
            "修正",
            "異動",
            "昇進",
        ),
    ),
    (
        "create_record",
        (
            "create",
            "register",
            "onboard",
            "new employee",
            "new hire",
            "add a record",
            "add an employee",
            "add a new",
            "登録",
            "追加",
            "新規",
            "入社",
            "作成",
        ),
    ),
    (
        "lookup_record",
        (
            "look up",
            "lookup",
            "find",
            "show",
            "get",
            "fetch",
            "retrieve",
            "search",
            "what is",
            "who is",
            "summarize",
            "record for",
            "record of",
            "照会",
            "検索",
            "参照",
            "確認",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a SmartHR employee-data operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(self, llm: "Any | None" = None) -> None:
        super().__init__()
        # `llm` is a test-double seam only - register_nodes() never passes one
        # in production. The real client is built fresh per invocation in
        # _classify_via_llm() from ctx.secrets, not cached on self: node
        # instances are constructed once in register_nodes() (registry LRU
        # cache, shared across every invocation) before any request's secrets
        # are provisioned, and caching one caller's client would leave it
        # visible to the next caller.
        self._llm = llm

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        # Heuristic baseline first - always available, fully deterministic.
        # An LLM classification then overrides it when the call succeeds and
        # validates; any failure (secret not provisioned, API error, malformed
        # response) silently keeps the keyword result.
        intent = self._classify_via_keywords(text)

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, " "defaulted to lookup_record (read-only)"]
            intent = "lookup_record"

        llm_intent = self._classify_via_llm(text, state)
        llm_used = False
        if llm_intent is not None:
            intent = llm_intent
            note = []  # LLM classification supersedes the low-confidence note
            llm_used = True

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note), "llm_used": llm_used},
            state,
        )

        result: "dict[str, Any]" = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_llm(self, text: str, state: "dict[str, Any]") -> "str | None":
        """LLM-based intent classification.

        Returns a valid intent string, or None on any failure (missing
        secret, API error, malformed response) - caller keeps the
        deterministic keyword classification. Never raises.
        """
        try:
            llm = self._llm
            if llm is None:
                ctx = InvocationContext.from_state(state)
                llm = AzureOpenAIClient(
                    {
                        "api_key": ctx.secrets.require("AZURE_OPENAI_API_KEY"),
                        "azure_endpoint": ctx.secrets.require("AZURE_OPENAI_ENDPOINT"),
                        "azure_deployment": ctx.secrets.require("AZURE_OPENAI_DEPLOYMENT"),
                    }
                )
            response = llm.complete(
                [
                    {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ]
            )
            parsed = extract_json_object(response.get("content", ""))
            candidate = parsed.get("intent") if isinstance(parsed, dict) else None
            return candidate if candidate in _VALID_INTENTS else None
        except Exception:
            return None

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"
