"""Lambda entry point of the API: API Gateway HTTP API (payload format 2.0) -> Mangum -> FastAPI.

Mangum turns the Lambda event into an ASGI request and puts the event itself in the scope as
"aws.event", where get_claims() finds the claims API Gateway's JWT authorizer verified
(api/auth.py).

The app's resources (settings, DB pool, S3 client) are opened by the container's first
invocation and reused by warm ones (DECISIONS 2026-11-21, "Step 13"). So Mangum's lifespan
support is off: it would open and close them around every invocation. Opening them on the
first call rather than at import keeps this module importable without AWS (tests).

A pooled connection can die while Lambda freezes the container between invocations. After
an idle gap longer than IDLE_RESET_SECONDS, the pool's connections are replaced before the
request runs. A connection that still turns out dead fails within the pool's command_timeout,
and the request gets a 503 with Retry-After (api/app.py).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI
from mangum import Mangum

from api.app import create_app, open_resources

logging.getLogger().setLevel(logging.INFO)  # Lambda's root logger defaults to WARNING

# Below the idle timeouts of Neon's pooler and of the network path to it.
IDLE_RESET_SECONDS = 60


class ApiHandler:
    """Callable as handler(event, context). One instance per container."""

    def __init__(
        self, app: FastAPI, open_app: Callable[[FastAPI], Awaitable[None]] = open_resources
    ) -> None:
        self.app = app
        self.open_app = open_app
        self.opened = False
        # One event loop for the container's life. The pool belongs to the loop it was
        # created on, and Mangum runs each request on the current loop, so this loop is made
        # current before anything runs.
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.mangum = Mangum(app, lifespan="off")
        self.last_request_at = 0.0

    def __call__(self, event: dict[str, Any], context: Any) -> dict[str, Any]:
        asyncio.set_event_loop(self.loop)
        now = time.monotonic()
        if not self.opened:
            self.loop.run_until_complete(self.open_app(self.app))
            self.opened = True
        elif now - self.last_request_at > IDLE_RESET_SECONDS:
            # Connections opened before the gap are closed and reopened on next acquire.
            self.loop.run_until_complete(self.app.state.pool.expire_connections())
        self.last_request_at = now
        return self.mangum(event, context)


handler = ApiHandler(create_app())
