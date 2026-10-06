# Boundary test: a blocked response must not ship the answer it blocked - nor
# the words of the block.
#
# The mechanism under test is easy to get wrong and invisible in unit tests.
# The base graph shapes its response as
#
#     formatted_output OR result
#
# so an output gate that merely raises - or returns an error status without
# clearing state - still hands the caller the un-gated inner answer inside the
# error envelope. Containment therefore means the answer is GONE, not relabelled.
#
# The envelope that replaces it is a closed set: a constant reason code and
# nothing else. Node-authored text - the gate's own findings, and whatever the
# inner workflow appended to error_log - is internal and never reaches the
# invoke body.
#
# The whole compiled backbone runs here (initialize -> pre_process -> main ->
# post_process -> finalize) with a stand-in for the `main` slot that produces a
# result the gate must refuse. The stand-in exists because the real inner
# workflow cannot reach post_process with an ungated answer - its own steps
# refuse first - and the property being asserted belongs to the OUTER backbone.

import json

import pytest

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import SmartHREmployeeDataAgent
from src.nodes.post_process_node import _OUTPUT_BEARING_FIELDS, _REASON_OUTPUT_WITHHELD, PostProcessNode, _contain
from src.nodes.pre_process_node import PreProcessNode

_RELEASED_TEXT = "Retrieved employee record 'restricted executive personnel file' - internal roster extract"
_REQUEST = "Look up the employee record for employee code 1001 and summarize the record on file."

# An error_log line of the kind an upstream failure produces. Assembled at
# runtime so no credential-shaped literal is committed; deliberately not a
# shape any credential detector fires on, so only the envelope construction
# itself can keep it away from the caller.
_SEEDED_ERROR = "boom: upstream said {'customer':'A. Tanaka','token':'" + "sk-" + "live-" + "xxx" + "'}"
_SEEDED_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")


def _leaves(value):
    """Every key and scalar inside `value`, rendered as text, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _leaves(item)
    else:
        yield str(value)


class _StandInMainNode(FunctionNode):
    """Stands in for the inner workflow; emits an answer with chosen evidence."""

    required_trust_level = TrustLevel.ANONYMOUS

    record_id = ""
    record_ref = ""
    error_log_seed: "tuple[str, ...]" = ()

    def execute(self, state):
        return {
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_record",
            "record_id": self.record_id,
            "record_ref": self.record_ref,
            "employee_name": "restricted executive personnel file",
            "confirmation": _RELEASED_TEXT,
            "result": {"record_id": self.record_id, "confirmation": _RELEASED_TEXT},
            "error_log": list(self.error_log_seed),
        }


class _StandInAgent(SmartHREmployeeDataAgent):
    main_node_class = _StandInMainNode

    def register_nodes(self):
        AgentBaseGraph.register_nodes(self)  # initialize + finalize
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = self.main_node_class()
        self._nodes["post_process"] = PostProcessNode()


def _build(record_id="", record_ref="", error_log_seed=()):
    class _Configured(_StandInMainNode):
        required_trust_level = TrustLevel.ANONYMOUS

    _Configured.record_id = record_id
    _Configured.record_ref = record_ref
    _Configured.error_log_seed = tuple(error_log_seed)

    class _Agent(_StandInAgent):
        main_node_class = _Configured

    agent = _Agent()
    agent.compile()
    return agent


def _invoke(agent, caller_id):
    return agent.invoke(
        user_input=_REQUEST,
        session_id="pb-containment",
        ctx=InvocationContext(caller_id=caller_id, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
    )


class TestOutputContainment:
    def test_the_base_graph_falls_back_to_result(self):
        """Why clearing is required, asserted against the framework itself.

        If this stops being true the containment below is belt-and-braces
        rather than load-bearing - but while it holds, an uncleared `result`
        ships whatever the gate refused.
        """
        shaped = _build().get_output({"result": _RELEASED_TEXT})
        assert shaped["output"] == _RELEASED_TEXT

    def test_gate_violation_ships_no_released_text(self):
        result = _invoke(_build(), "contained")
        assert result["status"] == AgentStatus.ERROR.value
        # The caller gets the reason code and nothing else: no record
        # evidence, no confirmation, no employee label, no request body - and
        # not the gate's finding either, which is an internal error_log line.
        assert result["output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        rendered = json.dumps(result, default=str)
        assert _RELEASED_TEXT not in rendered
        assert "restricted executive personnel file" not in rendered
        assert "output gate" not in rendered
        assert "error_log" not in result

    def test_error_text_seeded_in_state_never_reaches_the_invoke_body(self):
        """The `main` slot appended an upstream-style line to error_log before
        the gate refused its answer. The reducer keeps it in state for the
        audit trail; the invoke body carries none of it, keys or values."""
        result = _invoke(_build(error_log_seed=(_SEEDED_ERROR,)), "contained-seeded")
        assert result["status"] == AgentStatus.ERROR.value
        assert result["output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        leaves = list(_leaves(result))
        for fragment in _SEEDED_FRAGMENTS:
            assert not any(fragment in leaf for leaf in leaves), (fragment, result)

    def test_error_envelope_carries_no_traceback_or_source_path(self):
        envelope = repr(_invoke(_build(), "contained-no-trace"))
        assert "Traceback" not in envelope
        assert '.py", line' not in envelope
        assert "/src/nodes/" not in envelope
        assert "site-packages" not in envelope

    def test_negative_control_a_compliant_answer_still_ships(self):
        """Proves the gate - not a broken graph - is what suppressed the answer."""
        result = _invoke(_build(record_id="crew-1001", record_ref="smarthr://crews/crew-1001"), "allowed")
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["output"]["record_id"] == "crew-1001"
        assert result["output"]["confirmation"] == _RELEASED_TEXT
        assert "reason" not in result["output"]


@pytest.mark.parametrize("field", _OUTPUT_BEARING_FIELDS)
def test_every_output_bearing_field_is_cleared_by_the_gate(field):
    """A field added to the response shape later must be added to the clear list."""
    contained = _contain(_REASON_OUTPUT_WITHHELD, ["gate: refused"])
    assert field in contained
    assert not contained[field]
