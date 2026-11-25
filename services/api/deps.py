"""Shared resources for endpoints, created once in the app's lifespan (api/app.py).

They live on app.state, so tests can put their own pool, S3 client and settings there.
"""

import asyncpg
from fastapi import Request

from api.breaker import Breakers
from api.config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def get_s3(request: Request):
    return request.app.state.s3


def get_sqs(request: Request):
    return request.app.state.sqs


def get_breakers(request: Request) -> Breakers:
    return request.app.state.breakers
