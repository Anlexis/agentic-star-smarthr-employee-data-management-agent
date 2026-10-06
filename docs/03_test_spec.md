# Test Specification - CMN-C2-281 SmartHR Employee Data Agent

## Test strategy

- Test types: Unit (per node + service + config + caller contract + inner graph)
  and Proof-of-Boundary (the real ASGI `/invoke` entry point, the compiled outer
  graph, output containment, import isolation, state safety, server boot).
- Location: `tests/unit/`, `tests/proof_of_boundary/` (`tests/integration/` is an
  empty package; end-to-end coverage lives in the boundary suite, which drives
  the real compiled graph through the HTTP adapter).
- The SmartHR call is exercised through the deterministic, network-free stub
  transport (default) and through monkeypatched fake clients; no live SmartHR
  call is ever made. The stub derives synthetic crew ids (`crew-<digest8>`) from
  a request hash, so record evidence is asserted by shape (prefix +
  `record_ref` derivation), never a retyped hash literal.
- **Trust-gate routing canon**: every per-node unit test invokes the node as
  `node(state)` — `BaseNode.__call__` routes the full security pipeline (trust
  gate -> input gate -> `execute()` -> output gate) — never bare
  `node.execute(state)`. State builders set `caller_trust_level =
  TrustLevel.VERIFIED_EXTERNAL.value` for `PreProcessNode` (the single external
  gate) and `TrustLevel.ANONYMOUS.value` for every other node. Two documented
  exceptions, both deliberate:
  - `CallSmartHrApiNode.execute(state, config=...)` takes a second argument that
    `__call__` cannot forward;
  - the caller-data contract tests call `execute()` directly **on purpose**. A
    refusal through `node(state)` does not say who refused; a refusal through
    `execute()`, with no framework wrapper in front, proves the TEMPLATE owns
    the guarantee and would still hold where an upstream gate is absent or
    configured off.
  The trust-denial test asserts on the RETURNED error dict (`status ==
  AgentStatus.ERROR.value`, "trust gate denied" in `error_log`, execute-only
  keys absent) — `__call__` never raises for a trust denial.
- Assertion contract: the invoke surface is `result["output"]` / `status` /
  `trace_id` / `correlation_id` / `node_history` (never `formatted_output` at
  the invoke surface); status is compared to `AgentStatus.SUCCESS`/`.value`
  (lowercase `success`/`error`); identifiers may be masked (`[MASKED]`) so
  record evidence is asserted by presence or by derived shape, not by raw repr;
  audit spies assert on `call.args[1]` (the event payload), never the
  whole-call repr.
- Framework pipeline behaviours the suite encodes: `__call__` short-circuits on
  an incoming errored state (`execute()` is skipped; error status/error_log pass
  through); the framework input gate rewrites Title-Case bigrams (across
  newlines), emails and digit groups in `user_input`/`validated_input` to
  `[MASKED]` before `execute()` sees the text — positive text-channel payloads
  are therefore PII-free, and the intentional-PII tests assert the `[MASKED]`
  path; the framework's own credential scan raises on a credential-shaped value
  in any node result.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations | denial RETURNS an error dict ("trust gate denied" in error_log, execute-only keys absent); VERIFIED_EXTERNAL passes; every inner node declares ANONYMOUS |
| U-02 | test_pre_process_node.py | serialize the request + employee hint into `validated_input` (JSON); HTML strip; hint priority employee_id > employee_hint > employee_code | hint resolved by priority; `<script>` stripped; empty/missing -> `status=error` |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; framework `[MASKED]` path for emails; node-level token flag-and-redact (`secret_*`); state hint beats the copy embedded in the masked JSON | email -> `[MASKED]` before execute; token -> `[REDACTED]` + `redaction_flags=["token"]` (JSON string); empty/short -> error; audit payload carries flags only |
| U-04 | test_classify_intent_node.py | intent = lookup_record / create_record / update_record (keyword baseline, writes-first priority, read-only default); Azure OpenAI override (test-double injection only, no real network call): valid response overrides, prose/markdown-wrapped JSON still parses, malformed/wrong-shape/invalid-value response and a raised exception both fall back to the keyword result, no secret configured falls back, empty input never calls the LLM | correct intent per keyword; no-signal defaults to lookup_record with a non-fatal note; empty -> error; audit emits the intent label + `llm_used` only; LLM override clears the low-confidence note; every LLM failure mode degrades silently to the keyword result, no crash, no `status=error` |
| U-05 | test_infer_smarthr_fields_node.py | employee-code resolution (validated caller code > text mention; never invented); quoted name; `Key: value` fields; crew payload per intent; caller-contract precedence and field merge | lookup `{emp_code}`; create/update crew body with emp_code/last_name/name-keyed custom_fields; caller code and name win over the text channel; a caller field wins a name collision; unresolved code left `""`; a malformed caller block degrades instead of raising |
| U-06 | test_call_smarthr_api_node.py | lookup/create/update via the stub; `smarthr_config` state field + `execute(state, config=...)` override; API error / empty `crews` miss / unresolved code / unknown intent / missing payload; secret posture; the `timeout_s` call deadline | record_id (`crew-*`) + derived record_ref on success; an API error surfaces its STATUS CODE and not the upstream body; empty crews -> "no employee record found for the requested code" (the reason names no employee; `error_log` is internal but kept closed-set); live+no-secret -> error "unauthenticated"; live+bound secret -> token passed to the client; a call past the deadline is discarded; an unusable deadline falls back to the default |
| U-07 | test_confirm_node.py | human-readable confirmation per intent verb; ref/id formatting; name fallback | "Retrieved/Created/Updated employee record ... ref=... id=..."; missing evidence -> error |
| U-08 | test_post_process_node.py | `formatted_output` shaping (JSON payload round-trip); errored state passes through `__call__` un-masked (short-circuit); the output contract (record evidence, declared keys, credential scan incl. the NESTED case with a clean control; mapping KEYS scanned and a credential-shaped key withheld from the label); containment clears every output-bearing field on EVERY non-success return (violation AND inner-workflow error); the ERROR envelope is a closed set — parametrised over every non-success path with a sentinel seeded in `error_log` | success shape with parsed `smarthr_payload`, no `reason` key; error status/error_log preserved, no success shape fabricated; a SUCCESS without record evidence blocked; a credential nested two levels down is found by path; every non-success return leaves every output-bearing field cleared and ships `{"reason": <one of ERROR_REASONS>}` — truthy, the sentinel nowhere in the returned mapping (keys and values walked), inner entries not re-emitted, gate violations in `error_log` only, both paths equal to what `_contain()` builds |
| U-09 | test_smarthr_client.py | SmartHR REST API v1 client: find/create/update crew; `Authorization: Bearer` header; `/crews` + `/crews/{emp_code}` URLs; `SmartHrApiError` on non-2xx; stub shapes; `uses_stub_transport` | correct URLs/headers/bodies; 400 raises with joined `errors`; stub shapes deterministic |
| U-10 | test_config.py | flat `config/agent.yaml` manifest + `config/config.yaml` runtime parameters; the forwarding path | id CMN-C2-281, Cat 2, CMN, namespace cmn, no nested `agent:` block, dotted `class` entry point, VERIFIED_EXTERNAL, `requires.secrets == ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT"]` and `requires.extras == ["openai"]` (classify_intent's LLM override); runtime `smarthr.base_url` / `max_retry` / `timeout_s`; both reach the inner graph through `_parent_config()` |
| U-11 | test_domain_workflow_graph.py | inner `SmartHRWorkflowGraph`: identity, `_extra_initial_state()` config + deadline injection, the caller-context bridge seeding, `route()` annotation and error short-circuit, `get_output` contract, compile, direct inner invoke on the stub | name/state_schema correct; config forwarded as a JSON string with `timeout_s` merged; bridge-stashed caller data seeded (and absent when nothing was stashed); `route()` annotated with the graph's own `State`; error -> END; inner invoke runs validate -> classify -> infer -> call -> confirm to SUCCESS with record evidence |
| U-12 | test_caller_data_contract.py | the whole caller-data contract through `execute()`: accepted shapes, every malformed shape, non-finite numbers per field, the injection screen both directions | documented shape accepted (incl. Japanese labels and a numeric code); 14 malformed shapes each fail closed; a rejected value and an unsupported field NAME are never echoed; NaN/±Infinity/bool/fractional/out-of-range refused on both numeric channels and as whole-string spellings, while "Nancy"/"Infinity Branch" pass; 10 attack forms refused with nothing published; context keys, nested values and escaped payloads screened post-parse; 7 ordinary HR sentences produce no finding |
| U-13 | test_framework_compliance_tc06_tc07.py | domain nodes may extend the framework input/output gates only through the sanctioned hooks | overriding a final gate method raises at class definition |

## Proof-of-Boundary tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: 0 platform-SDK imports |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + external trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json` "input" (asserted); a VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, SmartHRWorkflowGraphNode, PostProcessNode, FinalizeNode]` and record evidence + confirmation in `result["output"]`; an ANONYMOUS caller is denied at pre_process (error, no post_process, no output); blank input -> error, not crash |
| PB-6b | The real ASGI `/invoke` entry point | test_pb_invoke_endpoint.py | health shape; runtime config reaches the compiled graph; an authenticated lookup returns REAL evidence computed from the request; caller-supplied employee data reaches the write INTACT while the same words in the text channel are masked (the bridge regression, with its control); absent caller data degrades to the text baseline; a pure-numeric code crosses byte-identical; create path; missing/wrong Bearer -> 401 with a generic body; oversized `input_context` -> 413; 7 malformed-metadata shapes refused; a rejected value never echoed; non-finite field values refused as strings AND as raw JSON literals; 6 injection forms refused with nothing written; an escaped payload screened post-parse; 4 ordinary HR requests still succeed; no credential-shaped string anywhere in the nested response; the ERROR envelope through `/invoke`: a gate refusal publishes `{"reason": "output_withheld_by_gate"}` and error text seeded in `error_log` — or planted as the gate's own finding — appears nowhere in the response body (keys and values walked); a live inner-workflow error yields `output: null` with the seeded text and no traceback absent from the body; an unpatched control still answers |
| PB-6c | Output containment | test_pb_output_containment.py | the framework's `get_output` fallback to `result` is asserted directly (so the containment below is load-bearing, not decorative); a gate violation ships `{"reason": "output_withheld_by_gate"}` only — no released text, no employee label, no gate finding, no traceback and no source path; error text seeded in state by the `main` slot is absent from the invoke body; a compliant answer still ships (negative control); every output-bearing field appears in the clear list |
| PB-6d | Error-envelope containment | test_error_envelope_no_record_evidence.py | the existing-ERROR branch of `post_process`: the shipped `formatted_output` is PRESENT and TRUTHY (a falsy value re-opens the `formatted_output or result` projection) and carries no `record_id`, `record_ref`, `employee_id`, `employee_name`, confirmation text or `smarthr://` reference; the returned delta CLEARS `result`, `confirmation`, `record_id`, `record_ref`, `employee_id`, `employee_name`, `smarthr_payload`, `intent`; the shipped envelope is `{"reason": "smarthr_workflow_failed"}`; the error REASONS (internal `error_log`) name no record — the not-found reason omits the employee code, the API-failure reason carries the HTTP status and not the upstream body, the transport reason carries the failure layer and the exception class, not the exception text; plus a success-path control so the containment assertions cannot pass vacuously |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived — non-HITL** (`config/agent.yaml` declares no `hitl.enabled: true`): module-level skipif; the stub bodies are real AssertionErrors so enabling HITL without implementing PB-7 fails loudly |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); the agent constructs and compiles via the supported path; `/invoke` + `/health` routes exposed |

> PB-1 (audit emission) is covered inside the unit suite via the emit-spy tests
> (validate / classify / call nodes assert on the event payload,
> `call.args[1]`). PB-3 (live external service) is exercised at first invoke
> against a live tenant, not in this suite — the shipped transport is the
> documented network-free stub.

## Test execution summary
- Runner: `python -m pytest tests/ -q` against the published framework wheel.
- Total tests: 230
- Pass: 228 / Fail: 0 / Skip: 2 (PB-7 A/B — auto-waived, non-HITL)
