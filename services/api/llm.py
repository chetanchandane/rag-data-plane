"""Answer generation with Bedrock's Converse API (SPEC: Query path; DECISIONS 2026-11-22,
"Step 15").

Converse is one request shape for every Bedrock model, so the failover from the primary
(Amazon Nova 2 Lite) to the secondary (Qwen3 235B) crosses providers with no second SDK or
API key. Nova is a US cross-region inference profile (us.*); Qwen is called directly in
us-east-2. Either way inference stays in US Regions. Claude Haiku 5.5 was the first primary
and its request shape is kept below, but this account is refused Anthropic models
(DECISIONS 2026-11-24, "LLMs without Anthropic").

The model answers by calling one tool, submit_answer, whose input is the structured answer.
How the tool is requested differs by model:
  - Claude runs with thinking on (Haiku 5.5's default) at the configured effort. Anthropic
    models don't accept a forced tool choice while thinking, so Claude gets toolChoice
    "auto" and the system prompt tells it to call the tool.
  - Other models (Nova, Qwen) get the forced choice, which both honor (checked against the
    real models, 2026-11-22 and 2026-11-24).
Either way the output is validated here; anything else is reported as no answer
(answer=None), never guessed at.

Blocking (boto3): the caller runs generate() on a worker thread.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import boto3
from botocore.config import Config
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

ANSWER_TOOL_NAME = "submit_answer"
# Room for Claude's (low-effort) thinking plus an answer with a few quotes.
MAX_OUTPUT_TOKENS = 4096

# No retry, and 20 s for the two models together: embedding (up to about 4.5 s), SQL (2 s),
# the reranker (1.5 s) and both models then fit in the Lambda's 29 s, under API Gateway's
# 30 s. botocore's read timeout is the wait for the response, which for a non-streaming
# Converse call is the generation time.
#
# The 20 s is split 6 / 14 (DECISIONS 2026-11-24, "Step 19"), not 10 / 10. Nova's slowest
# of 136 eval answers took 5.0 s; Qwen, which answers the prompts Nova misquoted, took up
# to 11.9 s and 5 of its 30 probe calls passed 10 s. A slower primary (Claude with
# thinking) would need this split looked at again.
PRIMARY_READ_TIMEOUT_S = 6
SECONDARY_READ_TIMEOUT_S = 14
# What both clients share; each sets its own read timeout (llm_client).
LLM_TIMEOUTS = Config(retries={"mode": "standard", "total_max_attempts": 1}, connect_timeout=2)

# USD per million tokens (input, output), by the model ID sent to Converse.
# Nova 2 Lite: AWS Price List API, us-east-2, 2026-11-22.
# Qwen3 235B A22B 2507: AWS Price List API, us-east-2, 2026-11-24.
# Haiku 5.5: Anthropic's list price. Bedrock bills Anthropic models through AWS Marketplace
# and the Price List API doesn't list them, so confirm it against the first bill.
PRICES_PER_MILLION: dict[str, tuple[float, float]] = {
    "us.anthropic.claude-haiku-5-5": (0.10, 0.50),
    "us.amazon.nova-2-lite-v1:0": (0.33, 2.75),
    "qwen.qwen3-235b-a22b-2507-v1:0": (0.22, 0.88),
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answerable": {
            "type": "boolean",
            "description": "false if the documents don't contain the answer",
        },
        "answer": {
            "type": "string",
            "description": "The answer, using only the documents. Empty if not answerable.",
        },
        "citations": {
            "type": "array",
            "description": "The passages that support the answer.",
            "items": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "string", "description": "The document's id attribute"},
                    "quote": {
                        "type": "string",
                        "description": "Text copied exactly from that document",
                    },
                },
                "required": ["chunk_id", "quote"],
            },
        },
    },
    "required": ["answerable", "answer", "citations"],
}


class ModelCitation(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    chunk_id: str
    quote: str


class ModelAnswer(BaseModel):
    """What submit_answer's input must look like. Citations are checked later against the
    chunks this request retrieved (api/answering.py)."""

    # strict: the model's JSON must have the right types ("yes" is not true).
    model_config = ConfigDict(extra="ignore", strict=True)

    answerable: bool
    answer: str = ""
    citations: list[ModelCitation] = Field(default_factory=list)


@dataclass(frozen=True)
class LlmModel:
    model_id: str  # a us.* inference profile or an in-Region model ID
    effort: str | None = None  # Claude only; None for other models
    # How long to wait for the answer, set by role (api/query.py). Required, so no model
    # silently gets the other role's budget: 14 s for both wouldn't fit the Lambda.
    read_timeout_s: int = field(kw_only=True)

    @property
    def is_claude(self) -> bool:
        return ".anthropic." in self.model_id or self.model_id.startswith("anthropic.")


@dataclass(frozen=True)
class Generation:
    model_id: str
    answer: ModelAnswer | None  # None: the output wasn't a valid submit_answer call
    input_tokens: int
    output_tokens: int
    stop_reason: str


# (model, system prompt, user prompt) -> Generation; raises botocore errors on failure.
Generate = Callable[[LlmModel, str, str], Generation]


def llm_client(region: str, read_timeout_s: int):
    """A bedrock-runtime client for generation: LLM_TIMEOUTS with this read timeout."""
    config = LLM_TIMEOUTS.merge(Config(read_timeout=read_timeout_s))
    return boto3.client("bedrock-runtime", region_name=region, config=config)


def llm_generate(region: str, make_client: Callable[[str, int], object] = llm_client) -> Generate:
    """generate() with the client whose read timeout is the model's own. botocore sets
    timeouts per client, not per call, so there is one client per timeout. The two that
    production uses are made up front; any other is made on first use."""
    clients = {
        seconds: make_client(region, seconds)
        for seconds in (PRIMARY_READ_TIMEOUT_S, SECONDARY_READ_TIMEOUT_S)
    }
    lock = threading.Lock()  # generate() runs on worker threads

    def run(model: LlmModel, system: str, prompt: str) -> Generation:
        with lock:
            client = clients.get(model.read_timeout_s)
            if client is None:
                client = clients[model.read_timeout_s] = make_client(region, model.read_timeout_s)
        return generate(client, model, system, prompt)

    return run


def converse_request(model: LlmModel, system: str, prompt: str) -> dict:
    """The Converse arguments for one model: everything model-specific is here."""
    tool = {
        "toolSpec": {
            "name": ANSWER_TOOL_NAME,
            "description": "Submit the answer to the question, with its citations.",
            "inputSchema": {"json": ANSWER_SCHEMA},
        }
    }
    request = {
        "modelId": model.model_id,
        "system": [{"text": system}],
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": MAX_OUTPUT_TOKENS},
    }
    if model.is_claude:
        request["toolConfig"] = {"tools": [tool], "toolChoice": {"auto": {}}}
        if model.effort:
            # Passed through to Anthropic's request body as output_config.effort.
            request["additionalModelRequestFields"] = {"output_config": {"effort": model.effort}}
    else:
        request["toolConfig"] = {
            "tools": [tool],
            "toolChoice": {"tool": {"name": ANSWER_TOOL_NAME}},
        }
    return request


def generate(client, model: LlmModel, system: str, prompt: str) -> Generation:
    """One Converse call. Bedrock errors (BotoCoreError, ClientError) are raised for the
    caller's failover; output that isn't a valid answer comes back as answer=None."""
    response = client.converse(**converse_request(model, system, prompt))
    usage = response.get("usage", {})
    return Generation(
        model_id=model.model_id,
        answer=parse_answer(response),
        input_tokens=int(usage.get("inputTokens", 0)),
        output_tokens=int(usage.get("outputTokens", 0)),
        stop_reason=str(response.get("stopReason", "")),
    )


def parse_answer(response: dict) -> ModelAnswer | None:
    """The first submit_answer call's input, validated, or None. Text and thinking blocks
    around it are ignored. A cut-off response (max_tokens) never counts as an answer."""
    if response.get("stopReason") == "max_tokens":
        return None
    content = response.get("output", {}).get("message", {}).get("content", [])
    for block in content:
        tool_use = block.get("toolUse")
        if tool_use and tool_use.get("name") == ANSWER_TOOL_NAME:
            try:
                return ModelAnswer.model_validate(tool_use.get("input"))
            except ValidationError:
                return None
    return None


def cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float | None:
    """The tokens' price, or None for a model without a known price."""
    prices = PRICES_PER_MILLION.get(model_id)
    if prices is None:
        return None
    return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000
