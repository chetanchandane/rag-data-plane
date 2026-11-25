"""The query cache's pure parts (api/cache.py) and its best-effort store. No network."""

import dataclasses
import uuid

import asyncpg
import pytest

from api import cache
from api.answering import Citation, QueryAnswer
from api.query import _from_cache
from tests.api.apphelpers import make_settings

TENANT = uuid.UUID("00000000-0000-4000-8000-000000000001")
VERSION = cache.answer_version(make_settings())


def key(**overrides) -> str:
    parts = {"tenant_id": TENANT, "role_level": 2, "query": "q", "epoch": 1, "version": VERSION}
    return cache.cache_key(**(parts | overrides))


def answer(**overrides) -> QueryAnswer:
    values = {
        "answer": "From the documents.",
        "abstain_reason": None,
        "citations": [Citation(uuid.uuid4(), uuid.uuid4(), 1, "a quote")],
        "degraded": [],
        "top_rerank_score": 0.9,
        "input_tokens": 1200,
        "output_tokens": 90,
        "cost_usd": 0.001,
    }
    return QueryAnswer(**(values | overrides))


def test_the_key_is_a_64_char_sha256_hex_and_deterministic():
    assert key() == key()
    assert len(key()) == 64 and int(key(), 16) >= 0


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": uuid.uuid4()},
        {"role_level": 3},
        {"query": "another question"},
        {"epoch": 2},  # an activation or a delete
        {"version": cache.answer_version(make_settings(llm_primary_model_id="other"))},
    ],
)
def test_every_part_changes_the_key(change):
    assert key(**change) != key()


def test_whitespace_doesnt_change_the_key_but_case_does():
    assert key(query="  What is   ICH\tE6?\n") == key(query="What is ICH E6?")
    # Case is kept: the lexical weight is case-sensitive, so this can recall differently.
    assert key(query="what is ich e6?") != key(query="What is ICH E6?")


def test_parts_cant_run_into_each_other():
    # With a plain "|" join these two would be the same string.
    a = cache.cache_key(TENANT, 2, "q|1", 1, VERSION)
    b = cache.cache_key(TENANT, 2, "q", 1, "1|" + VERSION)
    assert a != b


@pytest.mark.parametrize(
    "field, value",
    [
        ("embedding_model_id", "other-embedding"),
        ("rerank_model_id", "other-rerank"),
        ("llm_primary_model_id", "other-primary"),
        ("llm_primary_effort", "high"),
        ("llm_secondary_model_id", "other-secondary"),
        ("abstain_threshold", 0.5),
        ("llm_context_chunks", 8),
    ],
)
def test_the_answer_version_follows_every_setting_that_shapes_an_answer(field, value):
    settings = make_settings()
    assert cache.answer_version(dataclasses.replace(settings, **{field: value})) != (
        cache.answer_version(settings)
    )


def test_only_validated_non_degraded_answers_are_cacheable():
    assert cache.is_cacheable(answer())
    assert not cache.is_cacheable(answer(answer=None, abstain_reason="low_relevance"))
    assert not cache.is_cacheable(answer(answer=None, abstain_reason="not_in_documents"))
    assert not cache.is_cacheable(answer(degraded=["reranker"]))
    assert not cache.is_cacheable(answer(degraded=["citation_retry"]))


def test_a_stored_response_that_no_longer_fits_the_schema_is_a_miss():
    assert _from_cache({"answer": "only this field"}, "trace") is None


class FailingPool:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def acquire(self):
        return self

    async def __aenter__(self):
        raise self.error

    async def __aexit__(self, *exc):
        return False


async def test_a_store_that_cant_reach_postgres_is_skipped():
    stored = await cache.store(FailingPool(TimeoutError()), TENANT, 2, key(), {"answer": "a"})
    assert stored is False


async def test_any_postgres_rejection_is_skipped_not_a_500():
    # The answer is already paid for. A bug on the database side is logged, not raised.
    error = asyncpg.UntranslatableCharacterError("unsupported Unicode escape sequence")
    assert await cache.store(FailingPool(error), TENANT, 2, key(), {"answer": "a"}) is False


async def test_a_store_that_fails_outside_the_database_raises():
    with pytest.raises(ValueError):
        await cache.store(FailingPool(ValueError("a bug")), TENANT, 2, key(), {"answer": "a"})
