"""
Unit tests for the R18-C1 T4 terminology engine (backend/app/terminology.py).

Pure-Python: no DB. Covers the case-preserving, plural-aware, allowlist-scoped
rewrite and the empty-map no-op, plus resolve_run_terminology with the run-store
lookup monkeypatched.
"""
import app.terminology as terminology
from app.terminology import apply_terminology, rewrite_text

LENDING = {
    "customer": "borrower",
    "account": "facility",
    "obligation": "covenant",
    "rationale": "credit memo",
    "approval": "approval gate",
}


# ── rewrite_text: whole-word, case-preserving, plural-aware ───────────────────

def test_rewrite_singular_lowercase():
    assert rewrite_text("the customer signed", LENDING) == "the borrower signed"


def test_rewrite_preserves_title_case():
    assert rewrite_text("Customer review", LENDING) == "Borrower review"


def test_rewrite_preserves_upper_case():
    assert rewrite_text("CUSTOMER", LENDING) == "BORROWER"


def test_rewrite_pluralises_regularly():
    # customer -> borrower (regular +s); approval -> approval gate (+s on last token)
    assert rewrite_text("customers and approvals", LENDING) == "borrowers and approval gates"


def test_rewrite_pluralises_y_to_ies():
    # account -> facility, so accounts -> facilities (irregular y->ies)
    assert rewrite_text("open accounts", LENDING) == "open facilities"


def test_rewrite_whole_word_only():
    # "accounting" must NOT be rewritten — it is not the whole word "account".
    assert rewrite_text("accounting policy", LENDING) == "accounting policy"


def test_rewrite_multiword_domain():
    assert rewrite_text("needs approval today", LENDING) == "needs approval gate today"


def test_empty_map_is_noop():
    text = "the customer approval"
    assert rewrite_text(text, {}) == text
    assert rewrite_text(text, None) == text


# ── apply_terminology: allowlist-scoped deep rewrite ──────────────────────────

def test_only_allowlisted_fields_rewritten():
    opp = {
        "id": "opp_1",
        "title": "Customer approval backlog",
        "description": "Accounts awaiting approval.",
        "detector_id": "APPROVAL_BOTTLENECK",   # NOT allowlisted → verbatim
        "decision": "UNREVIEWED",               # NOT allowlisted → verbatim
        "tier": "Quick Win",
        "impact": 5,
        "confirmedBy": "the customer",          # unknown key → verbatim
    }
    out = apply_terminology(opp, LENDING)

    assert out["title"] == "Borrower approval gate backlog"
    assert out["description"] == "Facilities awaiting approval gate."
    # Technical / enum / identifier fields untouched.
    assert out["detector_id"] == "APPROVAL_BOTTLENECK"
    assert out["decision"] == "UNREVIEWED"
    assert out["tier"] == "Quick Win"
    assert out["impact"] == 5
    assert out["confirmedBy"] == "the customer"


def test_keys_are_never_rewritten():
    # A dict whose KEY happens to be a generic term keeps the key verbatim.
    out = apply_terminology({"account": {"title": "the customer"}}, LENDING)
    assert "account" in out                       # key preserved
    assert out["account"]["title"] == "the borrower"


def test_list_of_strings_under_allowlisted_key():
    out = apply_terminology(
        {"aiWhyBullets": ["Customer waits", "Account is stalled"]},
        LENDING,
    )
    assert out["aiWhyBullets"] == ["Borrower waits", "Facility is stalled"]


def test_nested_opportunities_rewritten():
    # Mirrors roadmap/exec-report shape: opps nested under a non-allowlisted key.
    report = {
        "confidence": "Moderate",
        "topQuickWins": [
            {"id": "o1", "title": "Customer approval", "object": "Account__c"},
        ],
    }
    out = apply_terminology(report, LENDING)
    assert out["topQuickWins"][0]["title"] == "Borrower approval gate"
    # Salesforce object API name is NOT allowlisted → verbatim.
    assert out["topQuickWins"][0]["object"] == "Account__c"
    assert out["confidence"] == "Moderate"


def test_apply_empty_map_returns_input_unchanged():
    opp = {"title": "Customer approval"}
    assert apply_terminology(opp, {}) == opp
    assert apply_terminology(opp, None) == opp


def test_non_string_leaves_untouched():
    obj = {"title": 5, "description": None, "aiRisks": [1, 2, 3]}
    out = apply_terminology(obj, LENDING)
    assert out == obj


# ── resolve_run_terminology: run → template → terminology ─────────────────────

def test_resolve_reads_template_from_run_record(monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "get_run", lambda rid: {"templateId": "commercial_lending"})
    result = terminology.resolve_run_terminology("run_x")
    assert result.get("customer") == "borrower"
    assert result.get("approval") == "approval gate"


def test_resolve_falls_back_to_setup_context(monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "get_run", lambda rid: {})  # no templateId on record
    monkeypatch.setattr(
        db, "run_kv_get",
        lambda key, rid, default=None: {"template_id": "commercial_lending"}
        if key == "setup_context" else default,
    )
    result = terminology.resolve_run_terminology("run_x")
    assert result.get("obligation") == "covenant"


def test_resolve_no_template_is_empty(monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "get_run", lambda rid: {})
    monkeypatch.setattr(db, "run_kv_get", lambda key, rid, default=None: default)
    assert terminology.resolve_run_terminology("run_x") == {}


def test_resolve_template_without_terminology_is_empty(monkeypatch):
    import app.db as db

    # service_operations declares terminology={} → no-op map.
    monkeypatch.setattr(db, "get_run", lambda rid: {"templateId": "service_operations"})
    assert terminology.resolve_run_terminology("run_x") == {}
