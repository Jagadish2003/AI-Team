import os

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)
DEV_JWT = os.getenv("DEV_JWT", "dev-token-change-me")
VIEWER_JWT = os.getenv("VIEWER_JWT", "viewer-token")

ROLE_LEVELS = {
    "viewer": 0,
    "analyst": 1,
    "owner": 2,
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
        roles[admin_jwt] = "owner"

    return roles


def _jwt_role(token: str) -> str | None:
    """Role claim carried by an AUTH-1 JWT, mapped onto the security levels.

    Fail-closed: the token is fully verified (signature + expiry + logout
    blocklist) via verify_jwt before the role claim is trusted. This does NOT
    rely on require_auth having run upstream — a forged or tampered token, or one
    with an inflated role claim, yields None here, so a caller that uses this
    role for an authorization decision cannot be tricked into trusting an
    unverified claim (privilege-escalation guard). AUTH-1's 'owner' maps to the
    highest security level ('owner'); 'analyst' and 'viewer' pass through.
    Both the legacy 'admin' claim and the new 'owner' claim normalise to 'owner'
    so the returned role always matches the unified ROLE_LEVELS / rbac vocabulary.
    Returns None for non-JWT static tokens and for any token that fails
    verification.
    """
    try:
        from app.auth.user_auth import verify_jwt

        payload = verify_jwt(token)
    except Exception:
        return None
    role = (payload.get("role") or "").strip().lower()
    if role in ("admin", "owner"):
        return "owner"
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
