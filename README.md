# SmartHR Employee Data Management Agent

AI agent for managing employee data in SmartHR, built with Agentic Star.

> **Category**: Cat 2 (domain-specific multi-step pipeline)
> **Industry**: Common
> **Template ID**: CMN-C2-281

## Overview

Turns a plain-language HR request into an employee-record action against the
[SmartHR](https://smarthr.jp/) REST API: it looks up an existing employee record, registers a new
one, or amends an existing one, and returns a confirmation naming the record it touched. Requests
arrive as free text ("Look up the employee record for employee code 1001", "社員番号 1001 の
従業員情報を照会してください"), optionally with structured employee data supplied alongside them,
and the pipeline classifies the intent, resolves the employee, assembles the API request body,
calls SmartHR, and formats the confirmation.

Two safety properties are built in rather than bolted on. **Nothing is invented**: the employee
code comes only from the caller's structured data or an explicit mention in the request, and an
unresolved code is left empty and reported rather than guessed — so the agent cannot amend the
wrong person's record. **A vague request never writes**: low-confidence classification falls back
to the read-only lookup.

Structured employee data travels in its own request channel (`input_context`) rather than inside
the request text. That is not a convenience: the framework masks personal data in the text channel
at every step, so a display name written into the prose arrives at the API call already rewritten.
The structured channel is validated field by field — bounded lengths, an inert character set, and
a finite+bounded parser for every number — and only then carried through to the write.

The bundled SmartHR transport is a deterministic, network-free stub, so the pipeline runs and
tests end-to-end out of the box without a live SmartHR tenant. A real deployment injects
`get` / `post` / `patch` transports at client construction; the request and response shapes
already follow the documented SmartHR `/crews` endpoints, so no pipeline change is needed.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Provided by the platform environment, not resolved from the default package index. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent manifest (agent.yaml) and runtime parameters (config.yaml)
docs/         design specification and test specification
```

See [`docs/02_design.md`](docs/02_design.md) for the architecture, the caller-data contract and
the output contract, and [`docs/03_test_spec.md`](docs/03_test_spec.md) for what the test suite
covers and why.

## Customising

1. Adjust `config/config.yaml` for your own tenant URL and call deadline.
2. Inject live SmartHR transports in `src/services/smarthr_client.py` and provision the
   integration token as an agent secret.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
