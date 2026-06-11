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


def _role_set_for(role: str | None) -> frozenset[str]:
    level = ROLE_LEVELS.get((role or "").strip().lower(), -1)
    return frozenset(name for name, value in ROLE_LEVELS.items() if value <= level)


_DEV_TOKEN_ROLES: frozenset[str] = _role_set_for(os.getenv("DEV_JWT_ROLE", "analyst"))


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


def _jwt_role(token: str) -> str | None:
    """Role claim carried by an AUTH-1 JWT, mapped onto the security levels.

    Decoded without signature verification — require_auth already proved the
    signature. AUTH-1's 'owner' maps to the highest security level ('admin');
    'analyst' and 'viewer' pass through. Returns None for non-JWT static tokens.
    """
    try:
        import jwt as _pyjwt

        payload = _pyjwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False},
            algorithms=["HS256"],
        )
    except Exception:
        return None
    role = (payload.get("role") or "").strip().lower()
    if role == "owner":
        return "admin"
    return role or None


def require_auth(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = creds.credentials
    # Static dev/test tokens — unchanged behaviour.
    if token in _token_roles():
        return token
    # AUTH-1 dynamic JWT (register / login / accept-invite): accept when the
    # signature, expiry, and logout blocklist all check out (AC15/AC9). Role and
    # org_id are enforced downstream by require_role and the tenancy middleware.
    try:
        from app.auth.user_auth import verify_jwt

        verify_jwt(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return token


def require_role(required_role: str) -> Callable[[str], str]:
    """Return a FastAPI dependency that enforces a minimum role."""
    required = required_role.strip().lower()
    required_level = ROLE_LEVELS.get(required)
    if required_level is None:
        raise ValueError(f"Unknown role: {required_role}")

    def dependency(token: str = Depends(require_auth)) -> str:
        if token in _token_roles():
            role = _token_roles().get(token)
            token_roles = _DEV_TOKEN_ROLES if token == DEV_JWT else _role_set_for(role)
        else:
            # AUTH-1 JWT — role comes from the verified token's claim.
            role = _jwt_role(token)
            token_roles = _role_set_for(role)
        if role is None or required not in token_roles:
            raise HTTPException(status_code=403, detail="Forbidden")
        return role

    return dependency
