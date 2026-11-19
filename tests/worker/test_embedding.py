"""Unit tests for embed_batch (a fake Bedrock client; never calls AWS)."""

import io
import json
import threading

import pytest

from worker.embedding import EMBEDDING_DIMENSIONS, embed_batch, make_bedrock_client

MODEL = "amazon.titan-embed-text-v2:0"


class FakeBedrock:
    """Returns a vector whose first value is the input's position, to check ordering."""

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self.dimensions = dimensions
        self.requests: list[dict] = []
        self._lock = threading.Lock()  # embed_batch calls from several threads

    def invoke_model(self, **kwargs) -> dict:
        body = json.loads(kwargs["body"])
        with self._lock:
            self.requests.append({**kwargs, "body": body})
        position = float(body["inputText"].split()[-1])
        vector = [position] + [0.0] * (self.dimensions - 1)
        return {"body": io.BytesIO(json.dumps({"embedding": vector}).encode())}


def test_results_are_in_input_order() -> None:
    texts = [f"chunk {i}" for i in range(32)]
    vectors = embed_batch(FakeBedrock(), texts, model_id=MODEL)
    assert [v[0] for v in vectors] == [float(i) for i in range(32)]


def test_request_asks_for_1024_normalized_dimensions() -> None:
    fake = FakeBedrock()
    embed_batch(fake, ["chunk 0"], model_id=MODEL)
    (request,) = fake.requests
    assert request["modelId"] == MODEL
    assert request["body"] == {"inputText": "chunk 0", "dimensions": 1024, "normalize": True}


def test_wrong_dimension_count_is_an_error() -> None:
    with pytest.raises(ValueError, match="expected 1024 dimensions, got 256"):
        embed_batch(FakeBedrock(dimensions=256), ["chunk 0"], model_id=MODEL)


def test_empty_batch_makes_no_calls() -> None:
    fake = FakeBedrock()
    assert embed_batch(fake, [], model_id=MODEL) == []
    assert fake.requests == []


def test_client_retries_throttling_with_backoff_five_times() -> None:
    # SPEC: backoff with full jitter on 429/5xx, 5 tries max. botocore's standard mode.
    client = make_bedrock_client("us-east-2")  # no credentials needed until a call is made
    assert client.meta.config.retries == {"mode": "standard", "total_max_attempts": 5}
    assert client.meta.region_name == "us-east-2"
