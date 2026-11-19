"""Claims for the current request (CLAUDE.md "Auth in Slice 1").

Every endpoint gets tenant and role level only from get_claims(). Slice 1's stub accepts one
fixed bearer token and returns the configured dev tenant and role. Slice 2 replaces the body
of get_claims() with real JWT verification; no endpoint changes.
"""

import hmac
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.config import Settings
from api.deps import get_settings


@dataclass(frozen=True)
class Claims:
    tenant_id: UUID
    role_level: int  # 1 to 3


# auto_error=False: we raise our own 401, the same for a missing and a wrong token.
_bearer = HTTPBearer(auto_error=False)


def get_claims(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Claims:
    # compare_digest takes the same time wherever the strings differ, so response timing
    # doesn't reveal how much of a guessed token was right.
    if credentials is None or not hmac.compare_digest(
        credentials.credentials.encode(), settings.dev_auth_token.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Claims(tenant_id=settings.dev_tenant_id, role_level=settings.dev_role_level)
