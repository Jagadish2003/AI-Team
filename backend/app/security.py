import os
from typing import Callable

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)
DEV_JWT = os.getenv("DEV_JWT", "dev-token-change-me")

# In dev mode the single bearer token is granted all roles.
# Production: decode the JWT and inspect role claims.
_DEV_TOKEN_ROLES: frozenset[str] = frozenset({"viewer", "analyst", "admin"})


def require_auth(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    if creds is None or creds.scheme.lower() != "bearer" or creds.credentials != DEV_JWT:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return creds.credentials


def require_role(role: str) -> Callable[[str], str]:
    """Return a FastAPI dependency that enforces a minimum role.

    Depends on require_auth — a 401 from require_auth propagates before
    this check runs.  Returns 403 Forbidden when the authenticated user
    does not carry the required role.

    Usage::

        @app.get("/protected")
        def endpoint(
            token: str = Depends(require_auth),
            _role: str = Depends(require_role("analyst")),
        ):
            ...
    """

    def _check_role(token: str = Depends(require_auth)) -> str:
        # Dev mode: the dev token has all roles.
        # Replace with JWT role-claim inspection in production.
        if role not in _DEV_TOKEN_ROLES:
            raise HTTPException(status_code=403, detail="Forbidden: insufficient role")
        return token

    return _check_role
