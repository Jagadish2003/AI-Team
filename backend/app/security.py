import os
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)
DEV_JWT = os.getenv("DEV_JWT", "dev-token-change-me")

def require_auth(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    if creds is None or creds.scheme.lower() != "bearer" or creds.credentials != DEV_JWT:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return creds.credentials
