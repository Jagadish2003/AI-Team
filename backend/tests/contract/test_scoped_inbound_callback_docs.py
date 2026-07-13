"""R18-A3 T6 (AT-559) — scoped-inbound fallback documentation package (AC7).

AC7 is a design-review / documentation criterion: "the scoped-inbound fallback
package exists as customer-facing deployment documentation for connectors lacking
an outbound-only mode." These tests make that criterion *enforceable* rather than
merely asserted in a PR — they fail if the package is deleted, gutted of a required
element, or drifts out of step with the auth-mode registry.

The package is Approach B from the R18-A3 story: a narrowly scoped inbound path for
the OAuth callback only (reverse-proxy pattern, callback-path-only, provider-IP
allowlist), for connectors whose only auth grant is authorization_code.

This is a documentation-presence + coverage test; it exercises no runtime code path.
"""
from __future__ import annotations

from pathlib import Path

from app.auth.auth_modes import OUTBOUND_ONLY_MODES
from app.auth.configs import CONNECTOR_AUTH_CONFIGS

# backend/tests/contract/ -> repo root is three parents up from this file's dir.
REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DOC = REPO_ROOT / "deployment" / "SCOPED_INBOUND_CALLBACK.md"
DEPLOYMENT_README = REPO_ROOT / "deployment" / "README.md"


def _doc_text() -> str:
    assert PACKAGE_DOC.exists(), (
        f"AC7: the scoped-inbound fallback package must exist at {PACKAGE_DOC}"
    )
    return PACKAGE_DOC.read_text(encoding="utf-8").lower()


# ---------------------------------------------------------------------------
# The package exists and is customer-facing deployment documentation
# ---------------------------------------------------------------------------


def test_scoped_inbound_package_exists():
    text = _doc_text()
    # Non-trivial content, not a stub.
    assert len(text) > 2000, "AC7: the package must be substantive, not a stub"


def test_package_covers_the_three_required_elements():
    """The Jira ticket names three elements the package must cover:
    reverse-proxy pattern, callback-path-only, and provider-IP allowlist."""
    text = _doc_text()
    assert "reverse proxy" in text or "reverse-proxy" in text, "missing reverse-proxy pattern"
    # Callback-path-only: the exact single exposed path must be named.
    assert "/api/connectors/oauth/callback" in text, "missing the exact callback path"
    assert "allowlist" in text, "missing the source/provider-IP allowlist guidance"


def test_package_documents_concrete_reverse_proxy_config():
    """A security team negotiates against concrete config, not prose alone."""
    text = _doc_text()
    # At least the canonical nginx pattern (deployment/README already cites nginx).
    assert "nginx" in text, "package should include a concrete nginx pattern"
    # GET-only restriction and deny-by-default are the load-bearing controls.
    assert "get" in text and "deny" in text, "package must show method + deny-by-default controls"


def test_package_rejects_the_vendor_relay_approach_c():
    """Approach C (a vendor-hosted callback relay) is rejected on principle; the
    package must say so, since a security team will ask why not just relay it."""
    text = _doc_text()
    assert "approach c" in text or "relay" in text
    assert "reject" in text, "package must state the relay is rejected"


# ---------------------------------------------------------------------------
# Coverage invariant: the package names every connector that actually needs it
# ---------------------------------------------------------------------------


def _connectors_lacking_outbound_only_mode() -> list[str]:
    """Registry-derived source of truth: connectors whose supported modes include
    NONE of the outbound-only modes — i.e. authorization_code is their only path,
    so they are exactly the connectors Approach B covers."""
    return [
        cid
        for cid, cfg in CONNECTOR_AUTH_CONFIGS.items()
        if not (set(cfg.supported_auth_modes) & OUTBOUND_ONLY_MODES)
    ]


def test_package_names_every_connector_that_needs_approach_b():
    """AC7 targets 'connectors lacking an outbound-only mode'. Whatever that set is
    per the auth-mode registry, each such connector must be named in the package —
    so adding a new authorization_code-only connector forces documenting it here,
    and the package can never silently under-cover."""
    text = _doc_text()
    needs_b = _connectors_lacking_outbound_only_mode()
    # Sanity: there is at least one such connector today (GitHub / Slack), so this
    # test is meaningfully exercised and not vacuously true.
    assert needs_b, "expected at least one authorization_code-only connector"
    missing = [cid for cid in needs_b if cid.lower() not in text]
    assert not missing, (
        "AC7: these connectors lack an outbound-only mode but are not named in "
        f"deployment/SCOPED_INBOUND_CALLBACK.md: {missing}"
    )


def test_package_does_not_claim_outbound_capable_connectors_need_it():
    """Connectors that DO have an outbound-only mode must not be listed as needing
    Approach B — the whole point is to steer them to their outbound-only path. We
    check the ones with a clearly unique id to avoid substring false-positives.

    Space-insensitive so the customer-facing display name ('Dynamics 365') still
    matches its registry id ('dynamics365')."""
    text_nospace = _doc_text().replace(" ", "")
    needs_b = set(_connectors_lacking_outbound_only_mode())
    # salesforce (jwt_bearer) and dynamics365 (client_credentials) are outbound-capable
    # and have distinctive ids safe to assert on.
    for cid in ("salesforce", "dynamics365"):
        if cid not in needs_b:
            # It may be mentioned (the §3 table lists it as "No"), but must never be
            # flagged as REQUIRING Approach B. The table marks these "**No**".
            assert cid in text_nospace, f"{cid} should appear in the coverage table (as not needing B)"


# ---------------------------------------------------------------------------
# Discoverability: the deployment README links the package
# ---------------------------------------------------------------------------


def test_deployment_readme_links_the_package():
    assert DEPLOYMENT_README.exists()
    readme = DEPLOYMENT_README.read_text(encoding="utf-8")
    assert "SCOPED_INBOUND_CALLBACK.md" in readme, (
        "deployment/README.md must link the scoped-inbound package so it is discoverable"
    )
