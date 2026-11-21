"""Claims for the current request (DECISIONS 2026-11-21, "Step 12").

Every endpoint gets tenant and role level only from get_claims(). Where they come from
depends on what is in front of the API:

- On Lambda, behind API Gateway: the HTTP API's JWT authorizer verifies the token's
  signature, iss, aud, exp, nbf and iat before the function runs, and passes the claims in
  the event at requestContext.authorizer.jwt.claims. Mangum puts the event in the ASGI scope
  as "aws.event". The API doesn't verify the token again: only API Gateway may invoke the
  function (its resource policy).
- Anywhere else (api.local, tests): nothing has verified the token, so the API does, with
  the TokenVerifier that api/app.py creates at startup from JWT_ISSUER.

The choice is made per request from the ASGI scope, which the server builds; a client can't
add "aws.event" to it. Either way, tenant_id and role_level are then checked here: a validly
signed token can still lack them.

This module must not import PyJWT: it runs on Lambda, where PyJWT isn't installed.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Claims:
    tenant_id: UUID
    role_level: int  # 1 to 3


class TokenRejected(Exception):
    """Raised by api.token_verifier when a token fails a check. Defined here, not there, so
    this module needn't import PyJWT. The message says which check, for the server log."""


# auto_error=False: we raise our own 401, the same for every failure.
_bearer = HTTPBearer(auto_error=False)


def _unauthorized(reason: str) -> HTTPException:
    # The reason goes to the log, never to the caller: the response doesn't say which check
    # failed.
    log.info("401: %s", reason)
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_claims(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Claims:
    event = request.scope.get("aws.event")
    if event is not None:
        raw = _authorizer_claims(event)
    else:
        raw = _verify_here(request, credentials)
    return parse_claims(raw)


def _authorizer_claims(event: Mapping[str, Any]) -> Mapping[str, Any]:
    """The claims API Gateway's JWT authorizer verified (payload format 2.0)."""
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = (authorizer.get("jwt") or {}).get("claims")
    if not isinstance(claims, Mapping):
        # A route without the JWT authorizer, or the function invoked some other way.
        raise _unauthorized("Lambda event has no JWT authorizer claims")
    return claims


def _verify_here(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> Mapping[str, Any]:
    verifier = getattr(request.app.state, "verifier", None)
    if verifier is None:
        raise _unauthorized("no API Gateway in front and no JWT_ISSUER to verify against")
    if credentials is None:
        raise _unauthorized("no bearer token")
    try:
        return verifier.verify(credentials.credentials)
    except TokenRejected as error:
        raise _unauthorized(str(error)) from None


def parse_claims(raw: Mapping[str, Any]) -> Claims:
    """tenant_id must be a UUID and role_level 1, 2 or 3.

    role_level is a JSON number when PyJWT decodes the token. API Gateway may pass every
    claim to the Lambda as a string, so "1" to "3" are accepted too. Nothing else is:
    no 2.0, no "02", and no true (bool is a subclass of int in Python).
    """
    tenant = raw.get("tenant_id")
    if not isinstance(tenant, str):
        raise _unauthorized("token has no tenant_id")
    try:
        tenant_id = UUID(tenant)
    except ValueError:
        raise _unauthorized("tenant_id is not a UUID") from None

    role = raw.get("role_level")
    if type(role) is int and 1 <= role <= 3:
        role_level = role
    elif isinstance(role, str) and role in ("1", "2", "3"):
        role_level = int(role)
    else:
        raise _unauthorized("role_level is missing or not 1, 2 or 3")
    return Claims(tenant_id=tenant_id, role_level=role_level)
