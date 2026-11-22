"""api/retrieval.py and POST /v1/retrieve without a database: the lexical-weight rule, the
request rules, and when the embedding failure falls back. DB behaviour: test_retrieve.py."""

import uuid

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from api import retrieval
from tests.api.apphelpers import build_app, client


class UntouchablePool:
    def acquire(self):
        raise AssertionError("the request reached the database")


@pytest.mark.parametrize(
    ("query", "weight"),
    [
        ("what is a policy enforcement point", 1.0),
        ("NIST SP800 zero trust", 2.0),  # code-like: letters then digits
        ("control AC-12 requirements", 2.0),  # with a hyphen
        ("ICH E10 guideline", 2.0),
        ("AC-2 one digit", 1.0),  # needs 2-6 digits
        ("ac-12 lower case", 1.0),  # capitals only
        ("ABCDE123 five letters", 1.0),  # 1-4 letters, whole token only
        ("version 1234", 1.0),  # digits alone aren't a code
        ('"exact phrase" search', 2.0),  # quoted phrase
        ('a lone " quote', 1.0),
    ],
)
def test_the_lexical_weight_rule(query, weight):
    assert retrieval.lexical_weight(query) == weight


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 2001},
        {"query": 42},
        {"query": "q", "top_k": 0},
        {"query": "q", "top_k": 31},
        {"query": "q", "top_k": "10"},  # strict: no string-to-int coercion
        {"query": "q", "top_k": True},
    ],
)
async def test_invalid_requests_get_422_before_the_database(body):
    app = build_app(UntouchablePool(), uuid.uuid4(), 3)
    app.state.embed_query = lambda q: pytest.fail("embedded an invalid request")
    async with client(app) as c:
        resp = await c.post("/v1/retrieve", json=body)
    assert resp.status_code == 422


async def test_a_request_without_a_token_gets_401():
    app = build_app(UntouchablePool(), uuid.uuid4(), 3)
    async with client(app, token=None) as c:
        resp = await c.post("/v1/retrieve", json={"query": "q"})
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "error",
    [
        ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-2.amazonaws.com"),
        ClientError({"Error": {"Code": "ThrottlingException"}}, "InvokeModel"),
    ],
    ids=["timeout", "throttled"],
)
async def test_bedrock_failures_mean_no_embedding(error):
    def embed(query):
        raise error

    assert await retrieval._embed(embed, "q") is None


async def test_other_embedding_errors_are_bugs_and_raised():
    def embed(query):
        raise ValueError("expected 1024 dimensions, got 256")

    with pytest.raises(ValueError):
        await retrieval._embed(embed, "q")


async def test_top_k_is_bounded_even_without_the_endpoint():
    with pytest.raises(ValueError):
        await retrieval.retrieve(
            UntouchablePool(),
            lambda q: [],
            query="q",
            top_k=31,
            tenant_id=uuid.uuid4(),
            role_level=1,
        )
