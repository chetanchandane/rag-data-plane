"""Answer generation with Bedrock's Converse API (SPEC: Query path; DECISIONS 2026-11-22,
"Step 15").

Converse is one request shape for every Bedrock model, so the failover from the primary
(Claude Haiku 5.5) to the secondary (Amazon Nova 2 Lite) crosses providers with no second
SDK or API key. Both are US cross-region inference profiles (us.*), so inference stays in
US Regions.

The model answers by calling one tool, submit_answer, whose input is the structured answer.
How the tool is requested differs by model:
  - Claude runs with thinking on (Haiku 5.5's default) at the configured effort. Anthropic
    models don't accept a forced tool choice while thinking, so Claude gets toolChoice
    "auto" and the system prompt tells it to call the tool.
  - Other models (Nova) get the forced choice, which Nova honors.
Either way the output is validated here; anything else is reported as no answer
(answer=None), never guessed at.

Blocking (boto3): the caller runs generate() on a worker thread.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

import boto3
from botocore.config import Config
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

ANSWER_TOOL_NAME = "submit_answer"
# Room for Claude's (low-effort) thinking plus an answer with a few quotes.
MAX_OUTPUT_TOKENS = 4096

# Each model gets 10 s and no retry: embedding (up to about 4.5 s), SQL (2 s), the
# reranker (1.5 s) and both models then fit in the Lambda's 29 s, under API Gateway's
# 30 s. botocore's read timeout is the wait for the response, which for a non-streaming
# Converse call is the generation time.
LLM_TIMEOUTS = Config(
    retries={"mode": "standard", "total_max_attempts": 1},
    connect_timeout=2,
    read_timeout=10,
)

# USD per million tokens (input, output), by inference profile ID.
# Nova 2 Lite: AWS Price List API, us-east-2, 2026-11-22.
# Haiku 5.5: Anthropic's list price. Bedrock bills Anthropic models through AWS Marketplace
# and the Price List API doesn't list them, so confirm it against the first bill.
PRICES_PER_MILLION: dict[str, tuple[float, float]] = {
    "us.anthropic.claude-haiku-5-5": (0.10, 0.50),
    "us.amazon.nova-2-lite-v1:0": (0.33, 2.75),
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
    model_id: str  # an inference profile ID, e.g. us.anthropic.claude-haiku-5-5
    effort: str | None = None  # Claude only; None for other models

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


def llm_client(region: str):
    """The API's bedrock-runtime client for generation (LLM_TIMEOUTS)."""
    return boto3.client("bedrock-runtime", region_name=region, config=LLM_TIMEOUTS)


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
