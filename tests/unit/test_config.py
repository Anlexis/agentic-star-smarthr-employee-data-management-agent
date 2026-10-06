# CMN-C2-281 - Unit tests: manifest + runtime config sanity.
#
# Two files, two jobs:
#   config/agent.yaml  - the FLAT registry manifest (every key at root level;
#                        no `agent:` block). Identity, entry point, trust and
#                        the compile-time `requires` gates live here.
#   config/config.yaml - the runtime parameters the graph is constructed with
#                        (max_retry, timeout_s) plus the `smarthr` integration
#                        section forwarded to the inner graph.
#
# The declared-secret assertion is a NEGATIVE one on purpose: the template
# reads its integration token with ctx.secrets.get() (optional) and never
# ctx.secrets.require(), and the default transport runs without a credential.
# Declaring a secret the deployment does not provision fails the agent at
# compile time, so `requires.secrets` must stay empty until a live transport
# and a provisioned token arrive together.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_CONFIG_DIR = pathlib.Path(__file__).parents[2] / "config"
_MANIFEST_PATH = _CONFIG_DIR / "agent.yaml"
_RUNTIME_PATH = _CONFIG_DIR / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_manifest_identity():
    data = _load(_MANIFEST_PATH)
    assert data["id"] == "CMN-C2-281"
    assert data["category"] == "Cat 2"
    assert data["industry"] == "CMN"
    assert data["base_type"] == "ToolCallingAgent"
    assert data["namespace"] == "cmn"


def test_manifest_is_flat():
    """No nested `agent:` block: the registry reads every key at root level."""
    data = _load(_MANIFEST_PATH)
    assert "agent" not in data


def test_manifest_entry_point():
    data = _load(_MANIFEST_PATH)
    assert data["class"] == "src.graph.graph.SmartHREmployeeDataAgent"


def test_manifest_security():
    data = _load(_MANIFEST_PATH)
    # Agent-level entry trust, enforced by the outer backbone pre_process gate;
    # inner domain nodes stay ANONYMOUS.
    assert data["required_trust_level"] == "VERIFIED_EXTERNAL"


def test_manifest_declares_llm_requirements_only():
    """Compile-time gates declare exactly the classify_intent LLM requirement.

    The SmartHR API token is read with ctx.secrets.get() (optional by
    contract - declaring it would make the agent fail to compile wherever
    it is not provisioned) and stays undeclared. The three Azure OpenAI
    values ARE compile-time required (ctx.secrets.require() inside
    ClassifyIntentNode._classify_via_llm()), so they must be declared here -
    any runtime failure of that call still degrades to the deterministic
    keyword classifier, but the declaration itself is a real compile-time
    gate for a registry-served deployment (require_at_compile()).
    """
    data = _load(_MANIFEST_PATH)
    assert data["requires"]["secrets"] == [
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
    ]
    assert data["requires"]["extras"] == ["openai"]


def test_runtime_config_smarthr_integration_section():
    data = _load(_RUNTIME_PATH)
    # Forwarded to the inner graph by SmartHRWorkflowGraphNode._parent_config().
    assert data["smarthr"]["base_url"] == "https://app.smarthr.jp/api/v1"


def test_runtime_config_scalars():
    data = _load(_RUNTIME_PATH)
    assert isinstance(data["max_retry"], int)
    assert isinstance(data["timeout_s"], int)


def test_runtime_config_reaches_the_inner_graph():
    """A declaration nothing reads is the failure this asserts against.

    The runtime file is the graph's config source after the manifest went flat;
    the integration section and the call deadline must arrive at the inner
    workflow through the graph node's config forwarding.
    """
    from src.graph.graph import SmartHRWorkflowGraphNode

    forwarded = SmartHRWorkflowGraphNode()._parent_config()["configurable"]
    assert forwarded["smarthr"]["base_url"] == _load(_RUNTIME_PATH)["smarthr"]["base_url"]
    assert forwarded["timeout_s"] == _load(_RUNTIME_PATH)["timeout_s"]
