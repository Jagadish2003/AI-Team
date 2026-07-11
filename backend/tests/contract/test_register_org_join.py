"""Contract tests — org-name deduplication + letters-only validation.

Registration now DEDUPLICATES by normalised org name: two registrations whose
org names are equal once trimmed + lowercased resolve to the SAME org_id (a
second registrant JOINS the existing workspace rather than minting a duplicate).
Distinct names still produce distinct orgs. The org name itself must be
letters-only; a non-letter name is rejected with 400 and a did-you-mean
suggestion.

These tests were previously the "review #5" isolation tests that asserted
same-name → different orgs. That behaviour is intentionally reversed here for the
name-dedup feature on this branch. No org names are hard-coded as special cases.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.auth import user_auth
from auth_helpers import member_for_email, rand_org_name


def _email() -> str:
    return f"join_{uuid.uuid4().hex[:10]}@example.com"


# ---------------------------------------------------------------------------
# AC1 — same normalised name (any case / whitespace) → same org_id
# ---------------------------------------------------------------------------


def test_same_name_case_insensitive_dedupes_to_one_org(client: TestClient):
    """'ReleaseOwl', 'releaseowl', 'RELEASEOWL' all resolve to ONE org_id."""
    base = rand_org_name("Release")  # letters-only, unique per run

    e1, e2, e3 = _email(), _email(), _email()
    user_auth.register_org_and_owner(base, e1, "Supersecret1!")
    user_auth.register_org_and_owner(base.lower(), e2, "Supersecret1!")
    user_auth.register_org_and_owner(base.upper(), e3, "Supersecret1!")

    org1, _ = member_for_email(e1)
    org2, _ = member_for_email(e2)
    org3, _ = member_for_email(e3)

    assert org1 == org2 == org3, "case variants must dedupe to a single org_id"


def test_leading_trailing_whitespace_dedupes(client: TestClient):
    base = rand_org_name("Ws")

    e1, e2 = _email(), _email()
    user_auth.register_org_and_owner(base, e1, "Supersecret1!")
    user_auth.register_org_and_owner(f"  {base}  ", e2, "Supersecret1!")

    assert member_for_email(e1)[0] == member_for_email(e2)[0]


def test_only_one_org_row_created_for_repeated_name(client: TestClient):
    from app import db

    base = rand_org_name("Once")
    con = db.connect()
    try:
        before = con.execute("SELECT COUNT(*) FROM orgs").fetchone()[0]
    finally:
        con.close()

    for _ in range(3):
        user_auth.register_org_and_owner(base, _email(), "Supersecret1!")

    con = db.connect()
    try:
        after = con.execute("SELECT COUNT(*) FROM orgs").fetchone()[0]
    finally:
        con.close()
    assert after - before == 1, "three same-name registrations create ONE org"


# ---------------------------------------------------------------------------
# AC2 — different users, same org name → same org_id, different user_ids
# ---------------------------------------------------------------------------


def test_two_users_same_name_share_org_but_distinct_users(client: TestClient):
    name = rand_org_name("Shared")

    ea, eb = _email(), _email()
    user_auth.register_org_and_owner(name, ea, "Supersecret1!")
    user_auth.register_org_and_owner(name, eb, "Supersecret1!")

    org_a, _ = member_for_email(ea)
    org_b, _ = member_for_email(eb)
    assert org_a == org_b, "same name → same org_id"

    from app import db

    con = db.connect()
    try:
        rows = con.execute(
            "SELECT u.id FROM users u WHERE u.email IN (%s, %s)", (ea, eb)
        ).fetchall()
    finally:
        con.close()
    user_ids = {r[0] for r in rows}
    assert len(user_ids) == 2, "distinct users despite shared org"


# ---------------------------------------------------------------------------
# AC3 — non-letter characters → 400 with a did-you-mean suggestion
# ---------------------------------------------------------------------------


def _register(client: TestClient, org_name: str):
    from auth_helpers import email_for_org

    # BUG 1: a valid (letters-only) org name needs a matching-domain email. For an
    # INVALID name the letters-only check fires first (400), so the email domain is
    # irrelevant there — deriving it from the name is safe for both cases.
    email = email_for_org(org_name) if org_name.strip().isalpha() else _email()
    return client.post(
        "/api/auth/register",
        json={"org_name": org_name, "email": email, "password": "Supersecret1!"},
    )


def test_underscore_name_rejected_with_suggestion(client: TestClient):
    # Build the input from a letters-only base so the expected suggestion is not
    # a hard-coded literal.
    base = rand_org_name("Release")  # letters only
    typed = f"{base}_Owl"
    expected = base + "Owl"  # non-letters removed

    resp = _register(client, typed)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "Only letters are allowed" in detail
    assert f"Did you mean '{expected}'?" in detail


def test_space_and_digit_name_rejected_with_suggestion(client: TestClient):
    a, b = rand_org_name("Agent"), rand_org_name("IQ")
    typed = f"{a} {b}2"
    expected = a + b  # space + digit removed

    resp = _register(client, typed)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert f"Did you mean '{expected}'?" in detail


def test_hyphen_and_digits_name_rejected_with_suggestion(client: TestClient):
    base = rand_org_name("IBM")
    typed = f"{base}-2024"
    expected = base  # hyphen + digits removed

    resp = _register(client, typed)
    assert resp.status_code == 400, resp.text
    assert f"Did you mean '{expected}'?" in resp.json()["detail"]


def test_validation_helper_units():
    """Direct unit coverage of the validation/normalisation contract."""
    import pytest

    # Suggestion strips every non-letter, preserving letter case/order.
    with pytest.raises(user_auth.RegistrationError) as exc:
        user_auth.validate_org_name("Release_Owl")
    assert "Did you mean 'ReleaseOwl'?" in str(exc.value)

    with pytest.raises(user_auth.RegistrationError) as exc:
        user_auth.validate_org_name("Agent IQ2")
    assert "Did you mean 'AgentIQ'?" in str(exc.value)

    # Letters-only names pass and are returned trimmed.
    assert user_auth.validate_org_name("  Acme  ") == "Acme"
    # Normalisation lowercases + trims.
    assert user_auth.normalise_org_name("  ReleaseOwl ") == "releaseowl"
    assert user_auth.normalise_org_name("IBM") == "ibm"


# ---------------------------------------------------------------------------
# AC4 — different org names → different org_ids
# ---------------------------------------------------------------------------


def test_different_names_create_distinct_orgs(client: TestClient):
    ea, eb = _email(), _email()
    user_auth.register_org_and_owner(rand_org_name("Alpha"), ea, "Supersecret1!")
    user_auth.register_org_and_owner(rand_org_name("Beta"), eb, "Supersecret1!")

    assert member_for_email(ea)[0] != member_for_email(eb)[0]


def test_valid_letter_only_names_any_case_accepted(client: TestClient):
    for name in ("Acme", "ACME" + rand_org_name(""), rand_org_name("Zeta")):
        resp = _register(client, name)
        # Either 201 (created) or, if the normalised name already exists from a
        # sibling case, still a successful 201 join — never a validation 400.
        assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# users.org_id FK (migration 0021) — denormalized pointer set at registration
# ---------------------------------------------------------------------------


def _users_org_ids(*emails: str) -> dict:
    """Return {email: users.org_id} straight from the users table."""
    from app import db

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT email, org_id FROM users WHERE email = ANY(%s)", (list(emails),)
        )
        rows = cur.fetchall()
    finally:
        con.close()
    return {r[0]: r[1] for r in rows}


def test_users_org_id_shared_across_case_variants(client: TestClient):
    """Users registering IBM / ibm / IbM all point at the SAME orgs.id."""
    from app import db

    base = rand_org_name("Ibm")  # letters-only, unique per run
    e1, e2, e3 = _email(), _email(), _email()
    user_auth.register_org_and_owner(base, e1, "Supersecret1!")            # e.g. IBM
    user_auth.register_org_and_owner(base.lower(), e2, "Supersecret1!")    # ibm
    mixed = "".join(c.upper() if i % 2 else c.lower() for i, c in enumerate(base))
    user_auth.register_org_and_owner(mixed, e3, "Supersecret1!")           # IbM

    con = db.connect()
    try:
        org_id = con.execute(
            "SELECT id FROM orgs WHERE name_normalised = %s",
            (user_auth.normalise_org_name(base),),
        ).fetchone()[0]
    finally:
        con.close()

    org_ids = _users_org_ids(e1, e2, e3)
    # Every user's users.org_id equals the single orgs.id (requirement 7a/7b).
    assert set(org_ids.values()) == {org_id}
    # …and it agrees with workspace_members, the source of truth.
    assert all(member_for_email(e)[0] == org_id for e in (e1, e2, e3))


def test_users_org_id_differs_across_distinct_orgs(client: TestClient):
    """Different organisations get different UUIDs in users.org_id (requirement 7c)."""
    ea, eb = _email(), _email()
    user_auth.register_org_and_owner(rand_org_name("Alpha"), ea, "Supersecret1!")
    user_auth.register_org_and_owner(rand_org_name("Beta"), eb, "Supersecret1!")

    ids = _users_org_ids(ea, eb)
    assert ids[ea] != ids[eb]
    # Each user's org_id matches its own org (via workspace_members).
    assert ids[ea] == member_for_email(ea)[0]
    assert ids[eb] == member_for_email(eb)[0]


def test_join_path_sets_users_org_id_to_existing_org(client: TestClient):
    """The dedup JOIN path (2nd registrant) also stamps users.org_id."""
    name = rand_org_name("JoinFk")
    first, second = _email(), _email()
    user_auth.register_org_and_owner(name, first, "Supersecret1!")   # creates org
    user_auth.register_org_and_owner(name, second, "Supersecret1!")  # joins it

    ids = _users_org_ids(first, second)
    assert ids[first] == ids[second]
    assert ids[second] == member_for_email(second)[0]


def test_backfill_links_legacy_user_without_org_id(client: TestClient):
    """The migration's backfill statement links a pre-existing user (org_id NULL)
    to its workspace membership's org."""
    import uuid as _uuid

    from app import db
    from database.models.users import BACKFILL_USERS_ORG_ID

    # A real org to link to.
    owner = _email()
    user_auth.register_org_and_owner(rand_org_name("Backfill"), owner, "Supersecret1!")
    org_id, _ = member_for_email(owner)

    # A legacy user: has a membership but org_id was never populated.
    legacy_id = str(_uuid.uuid4())
    legacy_email = _email()
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO users (id, email, password_hash, is_active, created_at) "
            "VALUES (%s, %s, %s, TRUE, %s)",
            (legacy_id, legacy_email, "x", db.now_iso()),
        )
        cur.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, 'analyst', %s)",
            (org_id, legacy_id, db.now_iso()),
        )
        con.commit()
        # Precondition: org_id is NULL for the legacy row.
        cur.execute("SELECT org_id FROM users WHERE id = %s", (legacy_id,))
        assert cur.fetchone()[0] is None

        # Run the exact backfill the migration runs.
        cur.execute(BACKFILL_USERS_ORG_ID)
        con.commit()

        cur.execute("SELECT org_id FROM users WHERE id = %s", (legacy_id,))
        assert cur.fetchone()[0] == org_id
    finally:
        con.close()
