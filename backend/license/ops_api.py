#!/usr/bin/env python3
"""R-1.9.1-L3 (Option 1 / 3b) — CloudFulcrum-internal license-ops record API.

A minimal, machine-to-machine API the external AWS-Lambda signer calls AFTER it
signs a payload-v2 key, so every Lambda-minted key is gated and lands in the L3
`license_registry` + append-only `issuance_audit` — closing the "issuance with no
registry / no gate / no audit" findings for the production (Lambda) issuance path,
without moving signing off AWS.

**This is NOT part of the customer app.** It is a SEPARATE ASGI app that lives
under `backend/license/` (excluded from the customer image by `.dockerignore`) and
runs only in CloudFulcrum's ops environment, where it can reach the ops Postgres
registry (standard `DATABASE_URL`). It is deliberately NOT registered in
`app/main.py` — the customer FastAPI app has no vendor role and must never expose
issuance.

Auth is a shared machine token (`LICENSE_OPS_API_TOKEN`), NOT the customer RBAC:
the caller is a Lambda, not a workspace member. Fail-closed — if the token is not
configured, every request is refused.

Run it in the ops environment (from backend/, venv active):

    LICENSE_OPS_API_TOKEN=<secret> DATABASE_URL=postgresql://... \
        uvicorn ops_api:app --app-dir license --host 0.0.0.0 --port 8900

The Lambda then POSTs the signed key here (see backend/license/README.md for the
request contract).
"""

from __future__ import annotations

import hmac
import os
import sys
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..")))
sys.path.insert(0, _HERE)

from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import issuance  # noqa: E402
import registry  # noqa: E402

OPS_TOKEN_ENV = "LICENSE_OPS_API_TOKEN"

# Internal ops API: no interactive docs / OpenAPI surface exposed.
app = FastAPI(
    title="AgentIQ License Ops API (internal)",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


class RecordIssuanceRequest(BaseModel):
    key: str                         # the signed payload-v2 license key string
    contract_ref: str                # the contract this issuance is tied to (gate)
    issued_by: str                   # the actor recorded in the audit ledger (gate)
    action: str = registry.ACTION_ISSUE   # issue | renew | regenerate
    supersedes: Optional[str] = None       # prior license_id, for a renewal
    notes: Optional[str] = None


def _require_ops_token(authorization: Optional[str]) -> None:
    """Fail-closed shared-token check for the machine caller (the Lambda)."""
    expected = os.getenv(OPS_TOKEN_ENV)
    if not expected:
        raise HTTPException(status_code=503, detail="license ops API token not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not hmac.compare_digest(authorization[len("Bearer "):], expected):
        raise HTTPException(status_code=401, detail="invalid ops token")


@app.get("/internal/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/internal/license/record-issuance")
def record_issuance_route(
    req: RecordIssuanceRequest,
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """Gate + record an already-signed key. Returns {license_id, audit_id, action}."""
    _require_ops_token(authorization)
    try:
        return issuance.record_issuance(
            key_string=req.key,
            contract_ref=req.contract_ref,
            issued_by=req.issued_by,
            action=req.action,
            supersedes=req.supersedes,
            notes=req.notes,
        )
    except (issuance.IssuanceError, registry.RegistryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
