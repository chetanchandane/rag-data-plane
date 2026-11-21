"""api/handler.py: the Lambda entry point, driven with API Gateway HTTP API (payload format
2.0) events through the real Mangum adapter. Fake resources; no AWS, no network.

Plain (sync) tests: the handler runs its own event loop, as on Lambda.
"""

import json
import time
import uuid

import asyncpg

from api.app import create_app
from api.handler import IDLE_RESET_SECONDS, ApiHandler
from tests.api.apphelpers import BUCKET, REGION, fake_s3

TENANT = uuid.UUID("7d1e0000-0000-4000-8000-000000000001")


class FakePool:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self.expired = 0
        self.fail_with = fail_with

    async def expire_connections(self) -> None:
        self.expired += 1

    def acquire(self):
        pool = self

        class _Acquire:
            async def __aenter__(self):
                raise pool.fail_with or AssertionError("unexpected database use")

            async def __aexit__(self, *exc):
                return False

        return _Acquire()


def make_handler(pool: FakePool | None = None) -> tuple[ApiHandler, list[int]]:
    opened: list[int] = []
    pool = pool or FakePool()

    async def open_fake(app) -> None:
        from api.config import Settings

        opened.append(1)
        app.state.settings = Settings(
            database_url="unused", raw_bucket=BUCKET, aws_region=REGION, jwt_issuer=None
        )
        app.state.pool = pool
        app.state.s3 = fake_s3()
        app.state.verifier = None  # on Lambda only API Gateway's claims count

    return ApiHandler(create_app(), open_app=open_fake), opened


def event(path: str, *, claims: dict | None, method: str = "GET") -> dict:
    """A payload format 2.0 event as API Gateway sends it (claim values as strings)."""
    request_context = {
        "accountId": "905418224080",
        "apiId": "abc123",
        "domainName": "abc123.execute-api.us-east-2.amazonaws.com",
        "http": {
            "method": method,
            "path": path,
            "protocol": "HTTP/1.1",
            "sourceIp": "203.0.113.7",
            "userAgent": "curl/8",
        },
        "requestId": "req-1",
        "routeKey": f"{method} {path}",
        "stage": "$default",
        "time": "21/Nov/2026:20:00:00 +0000",
        "timeEpoch": 1795286000000,
    }
    if claims is not None:
        request_context["authorizer"] = {"jwt": {"claims": claims, "scopes": None}}
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "abc123.execute-api.us-east-2.amazonaws.com"},
        "requestContext": request_context,
        "isBase64Encoded": False,
    }


def claims(role_level: str = "2") -> dict:
    return {
        "iss": "https://d111111abcdef8.cloudfront.net",
        "aud": "rag-data-plane",
        "sub": "t",
        "tenant_id": str(TENANT),
        "role_level": role_level,
        "exp": "1795286894",
    }


def test_whoami_returns_the_claims_api_gateway_passed_on():
    handler, _ = make_handler()
    response = handler(event("/v1/whoami", claims=claims("3")), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"tenant_id": str(TENANT), "role_level": 3}


def test_an_event_without_authorizer_claims_gets_401():
    handler, _ = make_handler()
    response = handler(event("/v1/whoami", claims=None), None)
    assert response["statusCode"] == 401


def test_resources_are_opened_once_per_container():
    handler, opened = make_handler()
    for _ in range(3):
        assert handler(event("/v1/whoami", claims=claims()), None)["statusCode"] == 200
    assert opened == [1]


def test_connections_are_replaced_after_an_idle_gap_only():
    pool = FakePool()
    handler, _ = make_handler(pool)
    handler(event("/v1/whoami", claims=claims()), None)
    handler(event("/v1/whoami", claims=claims()), None)
    assert pool.expired == 0  # back to back: connections are reused

    handler.last_request_at = time.monotonic() - IDLE_RESET_SECONDS - 1  # as after a freeze
    handler(event("/v1/whoami", claims=claims()), None)
    assert pool.expired == 1


def test_a_dead_database_connection_gets_503_with_retry_after():
    pool = FakePool(fail_with=asyncpg.ConnectionDoesNotExistError("connection was closed"))
    handler, _ = make_handler(pool)
    response = handler(event(f"/v1/documents/{uuid.uuid4()}", claims=claims()), None)
    assert response["statusCode"] == 503
    assert response["headers"]["retry-after"] == "1"


def test_a_query_timeout_gets_503():
    handler, _ = make_handler(FakePool(fail_with=TimeoutError()))
    response = handler(event(f"/v1/documents/{uuid.uuid4()}", claims=claims()), None)
    assert response["statusCode"] == 503
