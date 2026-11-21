"""The FastAPI app, built by create_app() (an ASGI app factory).

`api.local` serves it on this laptop; step 13 puts it on Lambda. Resources are created once
per process in the lifespan, so warm requests reuse the pool (SPEC: Query path, "asyncpg
details").
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api import documents
from api.config import Settings
from api.uploads import make_s3_client
from shared.db import create_pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()
    app.state.settings = settings
    app.state.pool = await create_pool(settings.database_url)
    app.state.s3 = make_s3_client(settings.aws_region)
    app.state.verifier = None
    if settings.jwt_issuer:
        # Imported only here: on Lambda JWT_ISSUER isn't set, API Gateway verifies tokens,
        # and PyJWT isn't installed (api/auth.py).
        from api.token_verifier import TokenVerifier

        app.state.verifier = TokenVerifier.from_issuer(settings.jwt_issuer)
    try:
        yield
    finally:
        await app.state.pool.close()


def create_app() -> FastAPI:
    app = FastAPI(title="rag-data-plane", lifespan=lifespan)
    app.include_router(documents.router)
    return app
