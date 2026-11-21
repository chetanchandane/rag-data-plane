"""GET /v1/whoami: the claims the API received (DECISIONS 2026-11-21, "Step 13").

Proves the token path end to end: through API Gateway's JWT authorizer, the claims it passes
on, and get_claims(). Also lets a client check a token. Needs no database and no scope.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.auth import Claims, get_claims

router = APIRouter()


class WhoAmI(BaseModel):
    tenant_id: str
    role_level: int


@router.get("/v1/whoami")
def whoami(claims: Annotated[Claims, Depends(get_claims)]) -> WhoAmI:
    return WhoAmI(tenant_id=str(claims.tenant_id), role_level=claims.role_level)
