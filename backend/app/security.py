import os
from collections.abc import Callable

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)
DEV_JWT = os.getenv("DEV_JWT", "dev-token-change-me")
VIEWER_JWT = os.getenv("VIEWER_JWT", "viewer-token")

ROLE_LEVELS = {
    "viewer": 0,
    "analyst": 1,
    "admin": 2,
}


def _token_roles() -> dict[str, str]:
    roles = {
        DEV_JWT: os.getenv("DEV_JWT_ROLE", "analyst").strip().lower(),
        VIEWER_JWT: "viewer",
    }

    analyst_jwt = os.getenv("ANALYST_JWT")
    if analyst_jwt:
        roles[analyst_jwt] = "analyst"

    admin_jwt = os.getenv("ADMIN_JWT")
    if admin_jwt:
        roles[admin_jwt] = "admin"

    return roles


def require_auth(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    if (
        creds is None
        or creds.scheme.lower() != "bearer"
        or creds.credentials not in _token_roles()
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return creds.credentials


def require_role(required_role: str) -> Callable[[str], str]:
    required = required_role.strip().lower()
    required_level = ROLE_LEVELS.get(required)
    if required_level is None:
        raise ValueError(f"Unknown role: {required_role}")

    def dependency(token: str = Depends(require_auth)) -> str:
        role = _token_roles().get(token)
        if role is None or ROLE_LEVELS.get(role, -1) < required_level:
            raise HTTPException(status_code=403, detail="Forbidden")
        return role

    return dependency
