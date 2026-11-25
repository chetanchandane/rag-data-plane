"""api/llm.py: the Converse request per model, parsing the answer, prices. A fake client;
no Bedrock."""

import pytest

from api.llm import (
    ANSWER_TOOL_NAME,
    LLM_TIMEOUTS,
    PRIMARY_READ_TIMEOUT_S,
    SECONDARY_READ_TIMEOUT_S,
    LlmModel,
    converse_request,
    cost_usd,
    generate,
    llm_client,
    llm_generate,
    parse_answer,
)
from api.query import answer_config
from tests.api.apphelpers import make_settings

HAIKU = LlmModel("us.anthropic.claude-haiku-5-5", "low", read_timeout_s=6)
NOVA = LlmModel("us.amazon.nova-2-lite-v1:0", read_timeout_s=6)
VALID = {"answerable": True, "answer": "Yes.", "citations": [{"chunk_id": "c1", "quote": "q"}]}


def response(*blocks: dict, stop: str = "tool_use", usage=(900, 80)) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": list(blocks)}},
        "stopReason": stop,
        "usage": {"inputTokens": usage[0], "outputTokens": usage[1]},
    }


def tool_use(payload, name: str = ANSWER_TOOL_NAME) -> dict:
    return {"toolUse": {"toolUseId": "t1", "name": name, "input": payload}}


class FakeBedrock:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.requests: list[dict] = []

    def converse(self, **request):
        self.requests.append(request)
        return self.reply


# --- the request --------------------------------------------------------------------------


def test_claude_gets_auto_tool_choice_and_the_effort_level():
    request = converse_request(HAIKU, "system text", "prompt text")
    assert request["modelId"] == "us.anthropic.claude-haiku-5-5"
    # A forced tool choice isn't accepted with thinking on, so Claude is told in the prompt.
    assert request["toolConfig"]["toolChoice"] == {"auto": {}}
    assert request["additionalModelRequestFields"] == {"output_config": {"effort": "low"}}
    assert request["system"] == [{"text": "system text"}]
    assert request["messages"] == [{"role": "user", "content": [{"text": "prompt text"}]}]
    assert request["inferenceConfig"]["maxTokens"] > 0  # always set explicitly


@pytest.mark.parametrize(
    "model",
    [
        NOVA,
        # As production builds it: the effort setting reaches Nova but must be ignored.
        LlmModel("us.amazon.nova-2-lite-v1:0", "low", read_timeout_s=6),
        # Called by its in-Region id, with no us. prefix.
        LlmModel("qwen.qwen3-235b-a22b-2507-v1:0", read_timeout_s=14),
    ],
    ids=["nova", "nova-with-effort", "qwen"],
)
def test_other_models_get_the_forced_tool_choice_and_no_claude_fields(model):
    request = converse_request(model, "s", "p")
    assert request["modelId"] == model.model_id
    assert request["toolConfig"]["toolChoice"] == {"tool": {"name": ANSWER_TOOL_NAME}}
    assert "additionalModelRequestFields" not in request


def test_both_models_get_the_same_answer_tool():
    (claude_tool,) = converse_request(HAIKU, "s", "p")["toolConfig"]["tools"]
    (nova_tool,) = converse_request(NOVA, "s", "p")["toolConfig"]["tools"]
    assert claude_tool == nova_tool
    assert claude_tool["toolSpec"]["name"] == ANSWER_TOOL_NAME
    schema = claude_tool["toolSpec"]["inputSchema"]["json"]
    assert schema["required"] == ["answerable", "answer", "citations"]


def test_one_attempt_and_20_seconds_split_6_for_the_primary_and_14_for_the_secondary():
    # DECISIONS 2026-11-24, "Step 19". The total must stay 20 s to fit the Lambda's 29 s.
    assert (PRIMARY_READ_TIMEOUT_S, SECONDARY_READ_TIMEOUT_S) == (6, 14)
    assert LLM_TIMEOUTS.retries == {"mode": "standard", "total_max_attempts": 1}
    config = answer_config(make_settings())
    assert (config.primary.read_timeout_s, config.secondary.read_timeout_s) == (6, 14)


def test_each_model_is_called_with_the_client_for_its_read_timeout():
    made: list[int] = []

    def make_client(region: str, seconds: int) -> FakeBedrock:
        made.append(seconds)
        client = FakeBedrock(response(tool_use(VALID)))
        client.seconds = seconds  # type: ignore[attr-defined]
        return client

    run = llm_generate("us-east-2", make_client)
    assert made == [6, 14]  # production's two clients exist before the first request

    run(LlmModel(NOVA.model_id, read_timeout_s=6), "s", "p")
    run(LlmModel(NOVA.model_id, read_timeout_s=14), "s", "p")
    run(LlmModel(NOVA.model_id, read_timeout_s=30), "s", "p")  # any other: made once, reused
    run(LlmModel(NOVA.model_id, read_timeout_s=30), "s", "p")
    assert made == [6, 14, 30]


def test_llm_client_sets_its_read_timeout():
    client = llm_client("us-east-2", 6)
    assert (client.meta.config.read_timeout, client.meta.config.connect_timeout) == (6, 2)
    assert client.meta.config.retries == {"mode": "standard", "total_max_attempts": 1}


# --- parsing ------------------------------------------------------------------------------


def test_generate_returns_the_parsed_answer_and_usage():
    client = FakeBedrock(response({"reasoningContent": {}}, tool_use(VALID)))
    generation = generate(client, HAIKU, "s", "p")
    assert generation.answer is not None and generation.answer.answer == "Yes."
    assert (generation.input_tokens, generation.output_tokens) == (900, 80)
    assert generation.model_id == HAIKU.model_id and generation.stop_reason == "tool_use"
    assert len(client.requests) == 1


@pytest.mark.parametrize(
    "reply",
    [
        response({"text": "The PEP enables connections."}, stop="end_turn"),  # text, no tool
        response(tool_use(VALID, name="other_tool")),
        response(tool_use({"answer": "Yes."})),  # answerable missing
        response(tool_use({"answerable": "yes", "answer": "", "citations": []})),  # not a bool
        response(tool_use({"answerable": True, "answer": "x", "citations": [{"quote": "q"}]})),
        response(tool_use("not an object")),
        response(tool_use(VALID), stop="max_tokens"),  # cut off
        {"output": {}},
    ],
    ids=[
        "text_only",
        "wrong_tool",
        "missing_field",
        "wrong_type",
        "citation_without_chunk_id",
        "not_an_object",
        "max_tokens",
        "empty_output",
    ],
)
def test_malformed_output_is_no_answer(reply):
    assert parse_answer(reply) is None


def test_extra_fields_in_the_answer_are_ignored():
    answer = parse_answer(response(tool_use(VALID | {"confidence": 0.9})))
    assert answer is not None and answer.answerable


def test_usage_is_still_reported_for_unusable_output():
    generation = generate(
        FakeBedrock(response({"text": "no tool"}, stop="end_turn")), NOVA, "s", "p"
    )
    assert generation.answer is None
    assert generation.input_tokens == 900  # billed anyway


# --- prices -------------------------------------------------------------------------------


def test_cost_uses_each_models_price_per_million_tokens():
    assert cost_usd(HAIKU.model_id, 1_000_000, 0) == pytest.approx(0.10)
    assert cost_usd(NOVA.model_id, 0, 1_000_000) == pytest.approx(2.75)
    assert cost_usd("us.example.unknown", 10, 10) is None
