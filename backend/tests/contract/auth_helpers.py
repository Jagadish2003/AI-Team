"""AUTH-2 test helpers — simulate the CloudFulcrum admin approval step.

Under AUTH-2 a freshly registered org starts in ``pending_approval``:
``POST /api/auth/register`` returns a pending-approval body with NO JWT, and
login is blocked (403) until an admin approves via the emailed link. These
helpers let the AUTH-1-era contract tests reach a logged-in state by approving
the org directly in the DB (the moral equivalent of the admin clicking Approve),
then logging in to obtain the JWT.

This is test-only and faithful to production: the real product still requires the
email-link approval; the dedicated approval-flow tests
(test_auth2_org_approval.py) exercise that path end-to-end. The DB UPDATE here
needs only UPDATE privilege (the app role has it; no DELETE/DDL).
"""
from __future__ import annotations

import uuid

from app import db

# Org-name validation now rejects anything but ASCII letters, so test org names
# can no longer embed a uuid hex suffix (digits) or spaces/underscores. Map the
# hex digits 0-9 to letters so the name stays unique per call AND letters-only;
# dedup then treats each generated name as its own org.
_HEX_TO_ALPHA = str.maketrans("0123456789", "ghijklmnop")


def rand_org_name(prefix: str = "Org") -> str:
    """Return a unique, letters-only org name (safe under the letters-only rule)."""
    return prefix + uuid.uuid4().hex[:12].translate(_HEX_TO_ALPHA)


def member_for_email(email: str) -> tuple[str, str]:
    """Return (org_id, role) for the workspace member registered under ``email``.

    AUTH-2 register no longer returns the created user/org, so tests read the
    membership directly when they need the org_id / role.
    """
    con = db.connect()
    try:
        row = con.cursor()
        row.execute(
            "SELECT wm.org_id, wm.role FROM workspace_members wm "
            "JOIN users u ON u.id = wm.user_id WHERE u.email = %s",
            (email.strip().lower(),),
        )
        r = row.fetchone()
    finally:
        con.close()
    return (r[0], r[1])


def activate_org_by_email(email: str) -> None:
    """Approve (activate) the org owned by ``email`` so its members can log in."""
    con = db.connect()
    con.autocommit = True
    try:
        con.cursor().execute(
            "UPDATE orgs SET approval_status = 'active' WHERE id IN ("
            "  SELECT wm.org_id FROM workspace_members wm "
            "  JOIN users u ON u.id = wm.user_id WHERE u.email = %s)",
            (email.strip().lower(),),
        )
    finally:
        con.close()


def register_approve_login(
    client,
    *,
    email: str,
    password: str,
    org_name: str | None = None,
) -> dict:
    """Register → approve the org → login. Return the login JSON ``{token, user}``.

    Mirrors the shape the AUTH-1 register response used to return, so a caller can
    swap ``register(...).json()`` for this and keep using ``["token"]`` / ``["user"]``.
    """
    org_name = org_name or rand_org_name()
    reg = client.post(
        "/api/auth/register",
        json={"org_name": org_name, "email": email, "password": password},
    )
    assert reg.status_code == 201, reg.text
    activate_org_by_email(email)
    login = client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200, login.text
    return login.json()
