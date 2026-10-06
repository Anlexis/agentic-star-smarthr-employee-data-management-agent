"""AgentCore Platform v1.0 - inner workflow Step 4: CallSmartHrApi (tool side-effect).

Performs the lookup/create/update call against the SmartHR REST API v1 crew
endpoints via src/services/smarthr_client.py.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. GraphNode.execute() passes the caller's InvocationContext into the
       inner subgraph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring
       INTERNAL here would deny that already-gated external caller before the
       call ever runs. The node therefore stays ANONYMOUS.
  Secret: the integration token is read via ctx.secrets.get("SMARTHR_TOKEN")
       (InvocationContext.from_state(state)) - never os.environ, never stored in
       state. While the deterministic NETWORK-FREE stub transport is active a
       missing token is tolerated (a sentinel placeholder is used - it is never
       sent anywhere because no request leaves the process); with a LIVE
       transport injected, a missing token is a hard status=error - a real API
       is never called unauthenticated.
  Audit: emit_trace_event() is called on the success path - a side-effect
       against an external HR system; HTTP 4xx/5xx surfaces as status=error +
       error_log (no silent pass), naming the status code rather than echoing
       the upstream response body.

Error reasons are CLOSED-SET labels only - what was not found, the HTTP
status, the failure layer and the exception class - never the employee code,
the employee name, an upstream SmartHR response body (unbounded third-party
text that can quote the very record it refused) or an exception message.
`error_log` is the internal channel (post_process publishes a reason code, not
the log), and a closed set there keeps the audit trail record-free and makes
any future projection safe by construction.

Configuration: this node takes NO constructor arguments (nodes are no-arg).
SmartHR settings (base_url, timeout_s) arrive as the JSON `smarthr_config`
state field - injected by the inner graph's _extra_initial_state() from the
runtime section forwarded by SmartHRWorkflowGraphNode._parent_config() - or via
the optional `config["configurable"]["smarthr"]` argument for direct
invocation. The client is constructed locally per call (no module-global
mutation).
"""

import time
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.security import TIMEOUT_MAX, TIMEOUT_MIN, finite_int_in_range
from src.services.smarthr_client import SmartHrApiError, SmartHrClient

_SECRET_KEY = "SMARTHR_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"

# Deadline applied to the SmartHR call when config/config.yaml declares
# `timeout_s`. A result that arrives after the deadline is discarded and the
# request fails closed rather than surfacing data the deployment declared too
# late to trust. A live transport should also set its own socket timeout so a
# hung connection is cut rather than only observed.
_DEFAULT_TIMEOUT_S = 30


class CallSmartHrApiNode(FunctionNode):
    """Look up / create / update a SmartHR employee record via REST API v1."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]", config: "dict[str, Any] | None" = None) -> "dict[str, Any]":
        payload = from_json(state.get("smarthr_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallSmartHrApiNode: missing smarthr_payload"],
            }

        intent = state.get("intent", "lookup_record") or "lookup_record"

        # Settings: runtime section from state (graph-injected), overridable via
        # an explicit config["configurable"]["smarthr"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("smarthr_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("smarthr") or {}
        settings.update(override)

        # The declared deadline goes through the finite+bounded parser; an
        # absent or unusable declaration falls back to the documented default
        # rather than running unbounded.
        timeout_s = finite_int_in_range(settings.get("timeout_s"), TIMEOUT_MIN, TIMEOUT_MAX)
        if timeout_s is None:
            timeout_s = _DEFAULT_TIMEOUT_S

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE stub (documented limitation, docs/02).
        base_url = str(settings.get("base_url", "") or "").strip()
        client = SmartHrClient(base_url=base_url) if base_url else SmartHrClient()

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # Stub limitation: no request leaves the process, so run with a
                # non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallSmartHrApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        employee_id = state.get("employee_id", "") or str(payload.get("emp_code", "") or "")

        started = time.monotonic()
        try:
            if intent == "lookup_record":
                if not employee_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallSmartHrApiNode: unresolved employee code - cannot look up record"],
                    }
                resp = client.find_crew(employee_id, api_token) or {}
                crews = resp.get("crews") or []
                if not crews:
                    # The reason names WHAT was not found (a closed-set label),
                    # never for whom. error_log is the internal channel, but
                    # the audit trail reads it, so an interpolated employee
                    # code would disclose the record there - and a closed set
                    # keeps any future projection safe by construction.
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallSmartHrApiNode: no employee record found for the requested code"],
                    }
                record = crews[0]
                record_id = str(record.get("id", "")) or employee_id
                employee_name = state.get("employee_name", "") or str(record.get("last_name", ""))
            elif intent in ("create_record", "update_record"):
                if intent == "update_record" and not employee_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallSmartHrApiNode: unresolved employee code - cannot update record"],
                    }
                if intent == "create_record":
                    resp = client.create_crew(payload, api_token) or {}
                else:
                    resp = client.update_crew(payload, api_token) or {}
                record_id = str(resp.get("id", "")) or employee_id
                employee_name = state.get("employee_name", "")
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"CallSmartHrApiNode: unknown intent '{intent}'"],
                }
        except SmartHrApiError as exc:
            # The HTTP status is the closed-set part of the failure and all
            # that is logged. It is bound to a local first so nothing else on
            # the exception can ride the line: its message embeds the upstream
            # body, and an HR API error page can quote request content back.
            http_status = exc.status_code
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallSmartHrApiNode: SmartHR returned HTTP {http_status}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            # Exception CLASS only: a transport error's text can carry the
            # request URL and the employee code.
            failure_class = type(exc).__name__
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    f"CallSmartHrApiNode: the SmartHR call failed at the transport layer ({failure_class})",
                ],
            }

        elapsed = time.monotonic() - started
        if elapsed > timeout_s:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    f"CallSmartHrApiNode: the SmartHR call exceeded the configured "
                    f"{timeout_s}s deadline - result discarded"
                ],
            }

        record_ref = f"smarthr://crews/{record_id}" if record_id else ""

        # Audit the tool side-effect - intent + presence signals only, never
        # employee data or credentials.
        emit_trace_event(
            "call_smarthr_api_complete",
            {
                "intent": intent,
                "has_record_id": bool(record_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "employee_id": employee_id or record_id,
            "employee_name": employee_name,
            "status": AgentStatus.SUCCESS.value,
        }
