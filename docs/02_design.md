# Template Design Specification — CMN-C2-281 SmartHR Employee Data Agent

## Position in the framework architecture

| Item | Value |
|------|-------|
| Agent class | `SmartHREmployeeDataAgent` (`src/graph/graph.py`) |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Category | Cat 2 (multi-step domain workflow, tool-calling) |
| Base type | ToolCallingAgent |
| Composition | outer 5-node backbone; the domain pipeline is encapsulated in a `GraphNode` (`main` slot) wrapping an inner `BaseGraph` (`src/graph/domain_workflow_graph.py`) |
| Pipeline shape | classify intent → resolve the employee and the employee-data fields → build a SmartHR REST API v1 request → call the tool → format the confirmation. No retrieval, no autonomous loop. |

**Three-layer separation**

- State: flat TypedDict `State(AgentState)` — no Pydantic (checkpoint serialization is msgpack-based).
- Node: framework inheritance; only `execute(self, state) -> dict` is overridden.
- Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()` is not
  overridden on the outer graph — backbone wiring belongs to the framework).

## Architecture overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input state | Output state | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | own the caller-data contract: empty guard, injection screen, HTML/length sanitize, field-by-field validation of `input_context`; serialize the request into `validated_input` (JSON) | user_input, input_context | validated_input, employee_hint, caller_employee | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner SmartHR workflow subgraph; stash the validated caller data on the context bridge | validated_input, employee_hint, caller_employee | result, intent, employee_id, record_id, record_ref, employee_name, confirmation, smarthr_payload | GraphNode (caller ctx forwarded unchanged) | SmartHRWorkflowGraphNode (GraphNode) |
| post_process | shape the caller-facing `formatted_output`; enforce the output contract; contain a violation | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

The inner graph inherits `BaseGraph` (fully custom linear topology). The five
pipeline steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's
`InvocationContext` is forwarded into the subgraph unchanged, so the single
external trust gate stays on the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------|
| validate_input | 1 ValidateInput | empty/non-request guard; deterministic (regex) flag-and-redact of email/token-like strings before logging | validated_input, employee_hint, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | keyword classification (deterministic baseline) -> lookup_record / create_record / update_record; low-confidence -> lookup_record (read-only default — never a write); an Azure OpenAI call then attempts to override the keyword result, degrading silently to it on any failure | intent | ANONYMOUS |
| infer_smarthr_fields | 3 InferSmartHrFields | resolve the employee code, display name and employee-data fields from the validated caller contract first and the request text second; assemble the SmartHR REST API v1 request body per intent; an unresolved employee code is left empty (never invented) | employee_name, employee_id, smarthr_payload | ANONYMOUS |
| call_smarthr_api | 4 CallSmartHrApi | GET /crews (lookup) / POST /crews (create) / PATCH /crews/{id} (update) via `SmartHrClient`; token via ctx.secrets; call deadline enforced; 4xx/5xx -> status=error | record_id, record_ref, employee_id, employee_name | ANONYMOUS |
| confirm | 5 Confirm | format intent + record id + reference into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max 3) ^
Inner (inside main / SmartHRWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_smarthr_fields
              -> call_smarthr_api -> confirm -> END
```

The request text travels as a JSON string: `pre_process` serializes
`{"text", "employee_hint"}` into `validated_input`,
`SmartHRWorkflowGraphNode.extract_input()` hands that JSON to the subgraph, and
the first inner node (`validate_input`) parses it back.

The caller's **structured** data does not travel that way — see the next
section.

## Caller-data contract

`POST /invoke` accepts an optional `input_context` object alongside `input`:

| Field | Shape | Bound |
|---|---|---|
| `employee_id` / `employee_hint` / `employee_code` | string, or a JSON number | first present key wins; `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`; a number goes through the finite+bounded parser and is rendered as a string |
| `employee.name` | string | ≤100 chars of the inert render charset |
| `employee.fields` | object of field name -> value | ≤20 entries; name ≤40 chars, value ≤200 chars, both inert; a value supplied as a number goes through the finite+bounded parser |

Rules, all enforced in `PreProcessNode` (`src/nodes/pre_process_node.py`) with
the helpers in `src/services/security.py`:

- **Fail closed, name the field, never echo the value.** A wrong type, an
  unsupported field, an over-length value or a value outside the inert charset
  is a hard error naming the field; the rejected value never appears in the
  error, and an unsupported field's NAME is not echoed either (a field name is
  caller-controlled text too).
- **The inert charset** is letters in any script (so Japanese names and
  department titles pass unchanged), digits, underscore, space, hyphen, dot,
  comma, parentheses and the Japanese middle dot. Every one of these values
  renders into the confirmation the caller reads back and into the SmartHR
  request body, so free text here would be caller-controlled output injection.
- **Every caller-controlled number goes through `finite_int_in_range`**, which
  refuses bools, non-numerics, NaN, ±Infinity and out-of-range magnitudes. NaN
  is the sharp edge: it parses cleanly through `float()`, it arrives intact in
  a raw JSON body, and every comparison against it is False. The inventory is
  exactly two channels — the employee code when supplied as a JSON number, and
  an employee-data field value when supplied as a JSON number. A field value
  supplied as a *string* is a label and no numeric decision is ever made on it;
  the one string form that is still refused is a value that IS a non-finite
  float spelling ("NaN", "-Infinity"), because the value is written into an
  external system of record where later readers will parse it.
- **Structural caps**: ≤20 employee-data fields per request, and an adapter
  cap of 256 KB on the serialized `input_context`, refused with HTTP 413.
- **Absent caller data is not an error.** The pipeline degrades to the
  text-inference baseline.

### Why a separate channel (`src/graph/context_bridge.py`)

Two framework behaviours make the structured channel necessary and make it
non-trivial to deliver:

1. The framework masks personal data in `user_input` / `validated_input` at
   every node boundary. Real employee data trips those heuristics — a display
   name written as two Title Case words is rewritten to `[MASKED]` — so data
   smuggled inside the request text arrives at the SmartHR write corrupted.
   `input_context` is not masked.
2. `GraphNode.execute()` invokes the subgraph as
   `subgraph.invoke(user_input, session_id=..., ctx=...)` and does **not**
   forward `input_context` or outer state fields, so the validated data would
   never reach the inner workflow on its own.

The bridge closes (2) through the sanctioned subclass hooks:
`SmartHRWorkflowGraphNode.extract_input()` stashes the validated contract in a
`ContextVar` immediately before the subgraph invoke, and
`SmartHRWorkflowGraph._extra_initial_state()` seeds it into the inner state.
A `ContextVar` keeps the hand-off correct per thread/task, so concurrent
invocations in one process cannot see each other's employee data. What crosses
is only the validated contract — never the raw request body.

## Output contract

The caller-facing response is exactly these keys: `record_id`, `record_ref`,
`employee_name`, `intent`, `confirmation`, `smarthr_payload`.

This template renders **no monetary aggregates**, so there is no rounding grid
to enforce. The invariant it does state, and enforces for every
representation, is:

1. **The response carries only the declared keys.** A SmartHR response object
   is projected into them, never spread wholesale, so bulk record data cannot
   ride out on a field nobody declared.
2. **A SUCCESS response carries record evidence** (`record_id` or
   `record_ref`) — otherwise it would misrepresent the outcome of a write
   against a real HR system.
3. **No credential-shaped string appears anywhere in the response**, including
   strings nested inside the assembled SmartHR request body (a mapping, with a
   list of employee-data fields inside it). The scan walks the whole nested
   structure; a top-level-only scan reports zero findings on exactly the case
   that matters.

Enforcement lives in the module-level `_security_gate_output()` in
`src/nodes/post_process_node.py`, called inline from `PostProcessNode.execute()`.
It is deliberately not an instance method and not the framework
`_extra_security_gate_output` hook: the framework gate methods are `@final` on
`FunctionNode` and the real SDK auto-wraps `_extra_` hooks, which breaks the
`.invoke()` chain.

**Every error return clears the answer, it does not merely relabel it.**
`AgentBaseGraph.get_output()` shapes its response as
`formatted_output or result`, so a path that raised — or that returned an error
status without clearing state — would still ship the un-gated inner answer
inside the error envelope. On a gate violation **and** on a pre-existing
inner-workflow error the node returns ERROR and clears every output-bearing
state field (`result`, `confirmation`, `record_id`, `record_ref`,
`employee_id`, `employee_name`, `smarthr_payload`, `intent`) through the one
module-level `_contain()` helper. Each path emits its own audit event
(`post_process_error_contained` / `post_process_gate_blocked` — a reason code
and a count, never the text) and returns the same envelope shape.

**The error envelope is a closed set.** On any non-success path
`formatted_output` is `{"reason": <code>}` with the code drawn from
`ERROR_REASONS` (`smarthr_workflow_failed` when the inner workflow reported an
error, `output_withheld_by_gate` when the output gate refused the response) —
and nothing else. Never `error_log`, never the gate's violation entries, never
any node-authored text: those lines can embed an upstream SmartHR response
body, identifiers, names or caller-derived fragments, and truncating or
redacting them is not a closed set. `error_log` stays the internal channel —
the state reducer appends to it and the audit trail needs it — and is never
projected to the caller. The inner entries are not re-emitted by
`post_process` either (the reducer would duplicate every line).

**The error envelope is record-free.** The inner-workflow error branch does not
rebuild `record_id`/`record_ref` out of state. Those are this agent's WRITE
EVIDENCE — the gate REFUSES a SUCCESS that lacks them — so returning them under
an ERROR status would tell a caller being informed of failure that an employee
record was nonetheless written, and which one; SmartHR is an HR system, so the
crew id, the employee code and the employee name are personal data. Credential
redaction on that path is a DIFFERENT property and does not cover it: a record
identifier is not credential-shaped, so it passes a redaction pass untouched.
Omitting a field from one envelope is not clearing it either — the clearing
above is what stops a checkpoint or a downstream reader recovering it. The
envelope stays a NON-EMPTY mapping so the `formatted_output or result`
projection stops there rather than falling back onto whatever survived.

The error REASONS in `error_log` keep to closed-set labels too, even though the
caller never sees them: the audit trail reads them, and a closed set there makes
any future projection safe by construction. `CallSmartHrApiNode` emits what was
not found, the HTTP status, the failure layer and the exception class — never
the employee code, the employee name, an upstream SmartHR response body or an
exception message. A gate violation names the offending PATH — fixed keys and
indices, never the value; mapping keys are scanned like values, and a
credential-shaped key is withheld from the label (`<withheld>`), because the
label rides `error_log`, where the framework's own credential scan would raise
on the node result and re-open the `result` fallback the clearing closed.

## Configuration

Two files, two jobs:

| File | Role |
|---|---|
| `config/agent.yaml` | the **flat** registry manifest — every key at root level, no `agent:` block. Identity, entry point (`class: src.graph.graph.SmartHREmployeeDataAgent`), `required_trust_level`, and the compile-time `requires` gates. |
| `config/config.yaml` | the runtime parameters the graph is constructed with: `max_retry`, `timeout_s`, and the `smarthr` integration section. |

Nodes take **no constructor arguments** (nodes are no-arg; configuration never
rides on node instances). The standalone entry point loads `config/config.yaml`
and passes it to the graph constructor, so `max_retry` reaches the framework run
loop. `SmartHRWorkflowGraphNode._parent_config()` loads the same file and
forwards `{smarthr, llm (if declared), timeout_s}` to the inner graph under
`config["configurable"]` — never `{}`. The inner graph's
`_extra_initial_state()` injects the merged settings into State as a JSON string
(`smarthr_config`), where `CallSmartHrApiNode` reads them; an explicit
`config["configurable"]["smarthr"]` override is also honoured for direct
invocation.

`requires.secrets` and `requires.extras` are both empty, deliberately. The
integration token is read with `ctx.secrets.get()` — optional by contract — and
never `ctx.secrets.require()`, and the default transport runs without a
credential. Declaring a secret the deployment does not provision makes the agent
fail at compile time, so the declaration stays empty until a live transport and
a provisioned token arrive together.

## Security design

- **Trust gate** — the single external trust gate is on the outer backbone
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every
  inner domain node — **including the write-capable `CallSmartHrApiNode`** —
  declares `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL`
  would deny a legitimate external caller before the call runs — the boundary is
  enforced exactly once, at `pre_process`. Agent-level default trust
  `VERIFIED_EXTERNAL` is declared in `config/agent.yaml`. `src/api/server.py`
  enforces the standalone entry-point Bearer-token auth boundary
  (`INVOKE_AUTH_TOKEN` -> VERIFIED_EXTERNAL elevation).
- **Template-owned injection screen** — `src/services/security.py`
  `find_injection()` / `screen_text()`, applied by `PreProcessNode` to both
  channels. The template owns this guarantee itself rather than relying on an
  upstream gate being active; the unit suite proves it by calling `execute()`
  directly, with no framework wrapper in front. It covers chat-template control
  tokens as a class (`<|…|>`, `[INST]`, `<<SYS>>`, forged role tags) as well as
  instruction- and role-override phrasing, normalizes URL-encoding, NFKC and
  zero-width characters first, and screens the caller text **both raw and after
  the markup strip** — the strip removes a control token silently and would
  forward its directive residue as ordinary prose, and it can splice
  `ig<b>nore all rules` back into a matchable phrase. `input_context` is
  screened post-parse over every key and string value at any depth, so a
  payload in a mapping key, nested a level down, or `\u`-escaped on the wire is
  screened like a top-level value. Nothing the request text gets is skipped for
  the context channel: the markup strip applied to the text is *subsumed* there
  by the inert charset, which excludes `<`, `>` and quoting characters outright
  rather than removing them afterwards, and the injection screen runs on both
  representations of every context string.
  The role-override patterns are anchored on an assistant/model target rather
  than on the verb, because personnel language is full of the words an
  injection screen reaches for: "act as a new team lead" is an ordinary
  promotion request in this domain.
- **Input flag-and-redact** — `ValidateInputNode.execute()` runs a
  deterministic (regex, not model) scan for email addresses and
  access-token-like strings (`eyJ...`, `secret_...`, `sk-...`) and redacts them
  before any logging. An employee-data request legitimately names an employee,
  so this is flag-and-redact for safe logging, not a hard reject.
- **Secrets** — the integration token is read via
  `ctx.secrets.get("SMARTHR_TOKEN")` (`InvocationContext.from_state(state)`),
  never `os.environ`, never stored in State. A missing token is tolerated
  **only** while the network-free stub transport is active (no live call is
  made); with a live transport injected, a missing token is a hard
  `status=error`.
- **Call deadline** — `timeout_s` from `config/config.yaml` goes through the
  finite+bounded parser and bounds the SmartHR call; a result that arrives after
  the deadline is discarded rather than surfaced. An unusable declaration falls
  back to the documented default rather than running unbounded. A live transport
  should also set its own socket timeout so a hung connection is cut rather than
  only observed.
- **Error text** — an upstream API error surfaces as its status code, not its
  body: an HR API error page can quote request content back. Transport failures
  surface as a fixed message, never the exception text.
- **Audit** — every node's `execute()` emits a domain
  `emit_trace_event("<event>", {small non-PII payload}, state)` on its path
  (intent / presence signals only — never request text, employee data, or
  credentials). `__call__()` is never overridden. Events:

  | Node | event |
  |------|-----------|
  | pre_process | `pre_process_complete` |
  | validate_input | `validate_input_complete` |
  | classify_intent | `classify_intent_complete` |
  | infer_smarthr_fields | `infer_smarthr_fields_complete` |
  | call_smarthr_api | `call_smarthr_api_complete` |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete`, `post_process_gate_blocked`, `post_process_error_contained` |

  A refusal gets its own event: a block that leaves no trace is
  indistinguishable from a request that was never made.

## State definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` (fields are absent until their
producer node writes them). Dict/list payloads are stored as JSON strings
(`Optional[str]`) via the module helpers `to_json` / `from_json`, used by every
producer and consumer — LangGraph checkpoints use msgpack, which nested
containers are not safe for.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| employee_hint | NotRequired[str] | caller-supplied employee code, validated; never inferred | pre_process / validate_input |
| caller_employee | NotRequired[Optional[str]] | JSON — the caller's validated structured employee data, carried across the graph boundary by the context bridge | pre_process (seeded into the inner graph by `_extra_initial_state()`) |
| employee_id | NotRequired[str] | resolved SmartHR employee code (`emp_code`) | infer_smarthr_fields |
| redaction_flags | NotRequired[Optional[str]] | JSON list of patterns redacted before logging | validate_input |
| employee_name | NotRequired[str] | employee display name / record label | infer_smarthr_fields / call_smarthr_api |
| smarthr_payload | NotRequired[Optional[str]] | JSON — assembled SmartHR REST API v1 request body | infer_smarthr_fields |
| smarthr_config | NotRequired[Optional[str]] | JSON — the runtime `smarthr:` section plus `timeout_s`, injected by the inner graph | inner graph |
| record_id | NotRequired[str] | crew id / employee code returned by SmartHR | call_smarthr_api |
| record_ref | NotRequired[str] | human-readable record reference (`smarthr://crews/<id>`) | call_smarthr_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `formatted_output` are inherited from
`AgentState` and are **not** re-declared.

**State constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No JWT / API keys / credentials in State — the SmartHR token is accessed via `ctx.secrets`.
- `InvocationContext` read via `InvocationContext.from_state(state)`, never stored in State.

## Routing

The inner topology is linear, so `route()` is required by the `BaseGraph` ABC
but never referenced by an `add_conditional_edges()` call. It is nonetheless
annotated with **this graph's own `State`**, not the framework base state:
LangGraph reads a path callable's annotation as its input schema and projects
away every field the annotation does not declare, so a base-state annotation
would hand the callable a state with the domain fields missing and any branch
decision would be made on absent data. A unit test pins the annotation so a
future conditional edge inherits a correct one.

## Implementation note — model-backed synthesis

Field resolution (`InferSmartHrFieldsNode`) uses the validated caller contract
plus regex / line-structure extraction and remains fully deterministic — an
LLM would only make it non-reproducible for no benefit, since the caller
contract is already structured input.

Intent classification (`ClassifyIntentNode`) is LLM-enhanced: a deterministic
keyword heuristic is always computed first (so the template still runs and
tests without a model backend and the pipeline never blocks on an LLM
outage), then an Azure OpenAI call (`AzureOpenAIClient`, built fresh per
invocation inside `execute()` from `ctx.secrets` — never cached, never
`__init__`/`register_nodes()`-time) attempts to override it with genuine NL
reasoning over the request text. Any failure (secret not provisioned, API
error, malformed/wrong-shape JSON response) silently keeps the keyword
result — never raises, never sets `status=error`; the node's `llm=None`
constructor parameter is a test-double seam only, never used by
`register_nodes()` in production. `config/agent.yaml` declares
`AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT`
under `requires.secrets` and `openai` under `requires.extras` accordingly
(`generation_mode: "llm"`).

Free-text-to-field mapping and natural-language record summaries beyond
intent classification remain a documented follow-up: the `llm:` section —
when declared in `config/config.yaml` — is already forwarded to the inner
graph by `_parent_config()`, so wiring one in for `InferSmartHrFieldsNode` is
additive and requires no graph-shape change.

## Known limitation — the framework's own injection screen and HR phrasing

The framework applies its own high-confidence injection screen to the request
text before any node runs. One of its patterns matches `act as a
{different|new|…}`, which is ordinary personnel language: "she will act as a new
team lead" is refused before the pipeline sees it. This template does not
weaken or work around that gate. The practical guidance is to put role and
department changes in the structured `employee.fields` channel — which is what
the write path uses anyway — rather than in the request prose.

## Limitation — SmartHR client (documented)

`src/services/smarthr_client.py` ships a **deterministic, network-free stub** as
its default transport: it returns the documented SmartHR REST API v1 response
shapes (a crew-object list for lookups; the created/updated crew object with an
`id` + `emp_code` echo for create/update, derived from the request) so the
pipeline is runnable and testable without a live SmartHR tenant or an HTTP
client library. It does **not** perform a live SmartHR call. To go live, inject
real `get`/`post`/`patch` transports at construction; the method contracts and
payload shapes follow the SmartHR REST API v1 `/crews` endpoints, with three
adapter notes: a live `get` transport filters GET /crews by `emp_code` and wraps
the returned array under `"crews"`; a live adapter resolves the `emp_code` path
key of an update to the SmartHR crew UUID; and name-keyed `custom_fields`
entries are mapped to SmartHR custom-field template ids. The stub also runs
without a live credential — see Security design; a live transport requires
`SMARTHR_TOKEN`.

## Framework utilization

### Shared components used
- [x] `InvocationContext` — read in `CallSmartHrApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallSmartHrApiNode`) declare `TrustLevel.ANONYMOUS`
- [x] Secrets — `ctx.secrets.get("SMARTHR_TOKEN")`; entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`
- [x] `emit_trace_event()` — one domain call per node; framework lifecycle events (node_start/node_complete/node_error) are not re-emitted

### Composition pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `SmartHRWorkflowGraph` (`BaseGraph`) via `SmartHRWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `SmartHRWorkflowGraphNode._parent_config()` loads
  `config/config.yaml` and forwards `{smarthr, llm (if declared), timeout_s}`
  under `config["configurable"]` to the subgraph.
- **Caller-data forwarding**: the `ContextVar` bridge in
  `src/graph/context_bridge.py` (`GraphNode` does not forward `input_context`).
- **Error propagation strategy**: `propagate` (default) — inner errors re-raised
  as `SubgraphError`; per-step `status=error` + `error_log` for API/validation
  failures (no silent pass).

## Import isolation confirmation
- [x] The template imports `framework/` and `shared/` only; no platform-SDK import anywhere
- [x] `src/services/smarthr_client.py` and `src/services/security.py` have no
      framework imports (pure service layer, stdlib only)

## Design decision record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base class | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat Cat 1 (MainNode) | GraphNode + inner subgraph | GraphNode + inner subgraph | Cat 2 must not be flat; 5 domain steps live in the inner graph |
| Caller data channel | inside the request text | `input_context` + ContextVar bridge | `input_context` + bridge | the text channel is masked at every node boundary, so employee data sent that way reaches the write corrupted |
| Employee resolution precedence | text mention first | validated caller contract first | caller contract first | the structured value is the caller's unambiguous instruction and arrives unmasked; the text mention is a heuristic read of a rewritten string |
| Model dependency | model client in the pipeline for every step | deterministic baseline everywhere, model synthesis as a documented follow-up | split: `classify_intent` LLM-enhanced (Azure OpenAI, graceful degrade to keyword heuristic), `infer_smarthr_fields` stays fully deterministic | intent classification benefits from genuine NL reasoning over ambiguous phrasing; field resolution is already structured input (caller contract + regex), where an LLM would only add non-reproducibility |
| SmartHR client | live HTTP call | injectable transport + documented stub default | injectable + stub default | no live network in the shipped template; going live is a transport injection, no logic change |
| Node configuration | ctor-arg dependency injection | no-arg nodes + config forwarding via `_parent_config()` -> `configurable` -> state | no-arg nodes | nodes are no-arg (ctor args raise TypeError at graph build) |
| Write target | infer the employee code from prose freely | caller-supplied/explicit code only; unresolved left empty | explicit only | never write to the wrong employee record; unresolved code -> status=error, not invented |
| Default intent | create_record | lookup_record | lookup_record | low-confidence classification must never default to a write |
| Gate violation handling | raise / return ERROR | return ERROR **and clear every output-bearing field** | clear | `get_output()` falls back to `result`, so a relabelled error still ships the blocked answer |
| Inner-workflow error response | echo `record_id`/`record_ref` back so the caller can correlate the failure | record-free envelope + the same clearing | record-free + clear | the identifiers are the WRITE evidence; naming them tells a caller told the run failed that a record was written anyway, and whose. Redaction does not cover it — an identifier is not credential-shaped |
| Not-found reason text | name the employee code that missed | name what was not found | closed-set label | `error_log` is internal, but the audit trail reads it and a closed set there keeps any future projection safe by construction |
| Caller-visible error content | the `error_log` lines (credential-redacted) / the gate's findings | a constant reason code only | reason code only | node-authored error text can embed an upstream response body, identifiers or names; redaction or truncation of it is not a closed set. `error_log` stays the internal channel (state reducer + audit trail) and the envelope stays truthy on the constant key |
