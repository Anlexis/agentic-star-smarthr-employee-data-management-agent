"""AgentCore Platform v1.0 - SmartHR REST API v1 client.

Service layer: a thin wrapper around the SmartHR (employee-data SaaS) REST API
v1 crew endpoints. Contains NO business logic, NO routing, and NO credentials -
the integration token is passed in per call by the node (which reads it via
ctx.secrets). This module imports no framework/SDK internals - pure stdlib
(import-isolation, PB-4).

v1 LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented SmartHR response shapes (a crew-object list for lookups, wrapped
    under ``crews``; the created/updated crew object with an ``id`` +
    ``emp_code`` echo for create/update, derived from the request) so the
    pipeline is runnable and testable without a live SmartHR tenant or the
    ``requests`` package - it does NOT perform a live SmartHR call. This is the
    deliberate: the template never fakes a live call, it documents the
    limitation and ships a transport seam instead.

    To perform real SmartHR calls, inject live transports (requests-based
    ``post`` / ``patch`` / ``get``) at construction time; the method contracts
    follow the SmartHR REST API v1 ``/crews`` endpoints. Documented live-adapter
    notes: a live ``get`` transport filters GET /crews by ``emp_code`` and wraps
    the returned JSON array under ``"crews"`` (the stub does the same); a live
    adapter resolves the ``emp_code`` path key of an update to the SmartHR crew
    UUID before PATCH /crews/{id}. A live transport also requires a real
    integration token (see CallSmartHrApiNode - the stub runs without one
    because no request ever leaves the process).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, Any]", "dict[str, Any]"], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://app.smarthr.jp/api/v1"


class SmartHrApiError(Exception):
    """Raised when the SmartHR REST API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"SmartHR API error {status_code}: {message}")


class SmartHrClient:
    """SmartHR REST API v1 crew (employee-record) client.

    Args:
        base_url: SmartHR API base URL (default https://app.smarthr.jp/api/v1;
            live tenants use https://{tenant}.smarthr.jp/api/v1).
        post/patch/get: optional injected transports (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE v1 stub is used
            (see the module docstring - it returns the documented shape without
            a live SmartHR call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        post: Transport | None = None,
        patch: Transport | None = None,
        get: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._post = post
        self._patch = patch
        self._get = get

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free v1 default)."""
        return self._post is None and self._patch is None and self._get is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> "dict[str, str]":
        """Build the SmartHR REST API v1 auth headers (OAuth2 Bearer).

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}",
        }

    # -- v1 deterministic stub transport (default; NO network) ----------------

    def _stub_transport(
        self, url: str, headers: "dict[str, Any]", json_body: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free v1 stub - returns the documented SmartHR shape.

        NOT a live call. Synthetic ids are derived from the request so the
        response is stable and inspectable. See the module docstring for the v1
        limitation and how to inject live transports.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        if json_body.get("_smarthr_op") == "lookup":
            code = str(json_body.get("emp_code", "")) or f"e-{digest[:8]}"
            # Documented GET /crews shape: a JSON array of crew objects - the
            # transport contract wraps it under "crews" (live adapters do the same).
            return 200, {
                "crews": [
                    {
                        "id": f"crew-{digest[:8]}",
                        "emp_code": code,
                        "last_name": f"Employee {code}",
                        "custom_fields": [],
                    }
                ],
                "_stub": True,  # marks the network-free v1 stub response
            }
        # POST /crews (create) / PATCH /crews/{id} (update) - documented crew
        # object echo (id + emp_code) so the caller can reference the affected
        # record without a follow-up lookup.
        code = str(json_body.get("emp_code", "")) or f"e-{digest[:8]}"
        return 200, {
            "id": f"crew-{digest[:8]}",
            "emp_code": code,
            "_stub": True,  # marks the network-free v1 stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API ---------------------------------------------------------

    def find_crew(self, emp_code: str, api_token: str) -> "dict[str, Any]":
        """GET /crews - look up an employee record (crew) by employee code.

        The live SmartHR endpoint returns a JSON array of crew objects; a live
        ``get`` transport adapter is expected to filter by ``emp_code`` and wrap
        the array under ``"crews"`` (the stub returns the matching record
        directly in that shape). Returns the parsed response dict (containing
        ``crews``). Raises SmartHrApiError on non-2xx.
        """
        url = f"{self._base_url}/crews"
        transport = self._resolve(self._get)
        status, body = transport(url, self._headers(api_token), {"_smarthr_op": "lookup", "emp_code": emp_code})
        if not (200 <= status < 300):
            raise SmartHrApiError(status, _err_message(body))
        return body

    def create_crew(self, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """POST /crews - register a new employee record (crew).

        ``payload`` is the crew attributes body (``emp_code``, ``last_name``,
        name-keyed ``custom_fields`` - see docs/02 v1 adapter notes). Returns
        the parsed response dict (created crew object). Raises SmartHrApiError
        on a non-2xx status.
        """
        url = f"{self._base_url}/crews"
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise SmartHrApiError(status, _err_message(body))
        return body

    def update_crew(self, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """PATCH /crews/{id} - partially update an existing employee record.

        ``payload`` is the crew attributes body keyed by ``emp_code``. v1 uses
        the ``emp_code`` as the path identifier; a live adapter resolves it to
        the SmartHR crew UUID first (documented v1 note, docs/02). Returns the
        parsed response dict (updated crew object). Raises SmartHrApiError on a
        non-2xx status.
        """
        emp_code = str(payload.get("emp_code", "") or "")
        url = f"{self._base_url}/crews/{emp_code}"
        transport = self._resolve(self._patch)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise SmartHrApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a SmartHR error body."""
    if isinstance(body, dict):
        msg = body.get("message")
        if msg:
            return str(msg)
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
    return str(body)
