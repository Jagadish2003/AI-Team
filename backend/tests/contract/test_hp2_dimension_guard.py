"""HP-2.4 — embedding-dimension consistency check at startup.

The residual from the pack's item #1. The vector column accepts any dimension by
design and this does NOT change that; what was missing was any check that the
configured embedding model emits the dimension already stored under the active
model stamp. A mismatch previously surfaced as a runtime storage error on the
first write.

Covered here:
  * AC4 — a differing dimension is refused at STARTUP, not at first write;
  * the message names both dimensions, both model identities, and a remedy that
    actually works (the managed backfill does NOT fix the active stamp);
  * a first run passes silently — no stored vectors is not a mismatch;
  * an unknown model, an unreadable store and no active model all SKIP, and
    skipped is reported distinctly from ok ("could not look" is not "it is fine");
  * the check is read-only, and `database/models/retrieval.py` is untouched;
  * only the ACTIVE stamp is examined — other stamps may legitimately differ.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.retrieval import dimension_guard, embedding_dimensions
from app.retrieval.dimension_guard import (
    OUTCOME_MISMATCH,
    OUTCOME_OK,
    OUTCOME_SKIPPED,
    SKIP_NO_ACTIVE_MODEL,
    SKIP_NO_DECLARED_DIMENSION,
    SKIP_NO_STORED_VECTORS,
    SKIP_STORE_UNREADABLE,
    EmbeddingDimensionMismatch,
    check_embedding_dimensions,
    validate_embedding_dimensions,
)
from app.retrieval.embedding_dimensions import (
    MODEL_DIMENSIONS,
    declared_dimension,
    model_from_identity,
    normalise_model_name,
)

BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]
_GUARD_SOURCE: Path = BACKEND_ROOT / "app" / "retrieval" / "dimension_guard.py"
_RETRIEVAL_MODEL: Path = BACKEND_ROOT / "database" / "models" / "retrieval.py"


def _stored(*specs) -> List[Dict[str, Any]]:
    """Build a stored-dimension result set: (dims, chunks, orgs) tuples."""
    return [
        {"dimensions": d, "chunks": c, "orgs": o} for d, c, o in specs
    ]


def _check(*, identity="in_boundary:nomic-embed-text", version="1",
           declared=768, stored=None, raise_active=False, raise_store=False):
    def _active():
        if raise_active:
            raise RuntimeError("provider exploded")
        return (identity, version)

    def _declared(_ident):
        return declared

    def _stored_fn(_m, _v):
        if raise_store:
            raise RuntimeError("pgvector missing")
        return stored if stored is not None else []

    return check_embedding_dimensions(
        active_model=_active, declared=_declared, stored_dimensions=_stored_fn
    )


# ---------------------------------------------------------------------------
# The declared-dimension table
# ---------------------------------------------------------------------------


def test_the_four_documented_self_hosted_models_are_declared():
    """HP-2.7's on-prem templates name these four; the check needs their dims."""
    assert declared_dimension("nomic-embed-text") == 768
    assert declared_dimension("mxbai-embed-large") == 1024
    assert declared_dimension("all-MiniLM-L6-v2") == 384
    assert declared_dimension("bge-large-en-v1.5") == 1024


def test_the_shipped_configuration_model_is_declared():
    """.env.template pins text-embedding-3-small and records 1536-dim vectors."""
    assert declared_dimension("text-embedding-3-small") == 1536


def test_declared_dimension_accepts_a_stamped_identity():
    assert declared_dimension("in_boundary:nomic-embed-text") == 768
    assert declared_dimension("customer_tenant:text-embedding-3-small") == 1536


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("nomic-embed-text:latest", "nomic-embed-text"),
        ("NOMIC-EMBED-TEXT", "nomic-embed-text"),
        ("  nomic-embed-text  ", "nomic-embed-text"),
        ("library/nomic-embed-text", "nomic-embed-text"),
    ],
)
def test_model_names_are_normalised(raw, expected):
    """An Ollama tag is the same model and the same vector space.

    Treating `nomic-embed-text` and `nomic-embed-text:latest` as different would
    make the check silently skip a model the table knows.
    """
    assert normalise_model_name(raw) == expected
    assert declared_dimension(raw) == 768


def test_unknown_model_has_no_declared_dimension():
    """None means 'cannot check', never zero and never a mismatch."""
    assert declared_dimension("some-model-nobody-has-heard-of") is None
    assert declared_dimension("") is None


def test_bare_identity_has_no_model_component():
    """The hosted provider stamps 'hosted' with no model — nothing to look up."""
    assert model_from_identity("hosted") == ""
    assert declared_dimension("hosted") is None


def test_every_table_entry_declares_a_positive_dimension_and_a_basis():
    for key, entry in MODEL_DIMENSIONS.items():
        assert entry.dimensions > 0, key
        assert entry.basis in ("published", "measured"), key
        assert normalise_model_name(entry.model) == key, (
            f"{key} is not the normalised form of {entry.model}"
        )


# ---------------------------------------------------------------------------
# AC4 — a mismatch is refused at startup
# ---------------------------------------------------------------------------


def test_mismatch_is_detected():
    result = _check(declared=768, stored=_stored((1536, 42, 2)))
    assert result.outcome == OUTCOME_MISMATCH
    assert result.is_mismatch is True


def test_mismatch_raises_at_startup():
    with pytest.raises(EmbeddingDimensionMismatch):
        validate_embedding_dimensions(
            active_model=lambda: ("in_boundary:nomic-embed-text", "1"),
            declared=lambda _i: 768,
            stored_dimensions=lambda _m, _v: _stored((1536, 42, 2)),
        )


def test_refusal_names_both_dimensions_and_the_model_identity():
    with pytest.raises(EmbeddingDimensionMismatch) as exc:
        validate_embedding_dimensions(
            active_model=lambda: ("in_boundary:nomic-embed-text", "v2"),
            declared=lambda _i: 768,
            stored_dimensions=lambda _m, _v: _stored((1536, 42, 3)),
        )
    message = str(exc.value)
    assert "768" in message            # configured
    assert "1536" in message           # stored
    assert "in_boundary:nomic-embed-text" in message
    assert "v2" in message             # the version, which forms the stamp
    assert "42 chunks" in message
    assert "3 org(s)" in message


def test_refusal_names_a_remedy_that_actually_works():
    """The managed backfill does NOT fix the active stamp — it only re-embeds
    vectors stamped by a NON-active model, and these already carry the active one.

    Saying "run the backfill" would send an operator to a tool that runs and
    changes nothing, so the message must state both real remedies AND the caveat.
    """
    with pytest.raises(EmbeddingDimensionMismatch) as exc:
        validate_embedding_dimensions(
            active_model=lambda: ("in_boundary:nomic-embed-text", "1"),
            declared=lambda _i: 768,
            stored_dimensions=lambda _m, _v: _stored((1536, 1, 1)),
        )
    message = str(exc.value)
    assert "repin" in message.lower()
    assert "version" in message.lower()
    assert "will NOT fix this" in message


def test_several_conflicting_dimensions_are_all_reported():
    with pytest.raises(EmbeddingDimensionMismatch) as exc:
        validate_embedding_dimensions(
            active_model=lambda: ("in_boundary:nomic-embed-text", "1"),
            declared=lambda _i: 768,
            stored_dimensions=lambda _m, _v: _stored((1536, 10, 1), (384, 5, 1)),
        )
    message = str(exc.value)
    assert "1536" in message
    assert "384" in message


# ---------------------------------------------------------------------------
# What must NOT be a mismatch
# ---------------------------------------------------------------------------


def test_matching_dimension_passes_silently():
    result = _check(declared=768, stored=_stored((768, 900, 4)))
    assert result.outcome == OUTCOME_OK
    validate_embedding_dimensions(
        active_model=lambda: ("in_boundary:nomic-embed-text", "1"),
        declared=lambda _i: 768,
        stored_dimensions=lambda _m, _v: _stored((768, 900, 4)),
    )  # must not raise


def test_first_run_with_no_stored_vectors_passes_silently():
    """The normal state of a fresh deployment. Not a mismatch."""
    result = _check(declared=768, stored=[])
    assert result.outcome == OUTCOME_SKIPPED
    assert result.skip_reason == SKIP_NO_STORED_VECTORS
    validate_embedding_dimensions(
        active_model=lambda: ("in_boundary:nomic-embed-text", "1"),
        declared=lambda _i: 768,
        stored_dimensions=lambda _m, _v: [],
    )  # must not raise


def test_unknown_model_skips_rather_than_refusing():
    result = _check(declared=None, stored=_stored((1536, 10, 1)))
    assert result.outcome == OUTCOME_SKIPPED
    assert result.skip_reason == SKIP_NO_DECLARED_DIMENSION
    assert "embedding_dimensions.py" in result.detail  # says how to enable it


def test_unreadable_store_skips_and_says_it_is_not_evidence():
    """A check that cannot read its evidence has not found a fault — and must not
    imply it found health either."""
    result = _check(declared=768, raise_store=True)
    assert result.outcome == OUTCOME_SKIPPED
    assert result.skip_reason == SKIP_STORE_UNREADABLE
    assert "not evidence" in result.detail


def test_no_active_model_skips():
    for identity in ("", "   "):
        result = _check(identity=identity, declared=768)
        assert result.outcome == OUTCOME_SKIPPED
        assert result.skip_reason == SKIP_NO_ACTIVE_MODEL


def test_a_raising_provider_skips_rather_than_breaking_startup():
    result = _check(raise_active=True)
    assert result.outcome == OUTCOME_SKIPPED
    assert result.skip_reason == SKIP_NO_ACTIVE_MODEL


def test_skipped_is_never_reported_as_ok():
    """The distinction the whole degradation posture rests on."""
    for kwargs in (
        {"declared": None, "stored": _stored((1536, 1, 1))},
        {"declared": 768, "stored": []},
        {"declared": 768, "raise_store": True},
        {"identity": "", "declared": 768},
    ):
        assert _check(**kwargs).outcome != OUTCOME_OK


def test_check_never_raises_whatever_happens():
    """validate_* raises; check_* returns. The split is what lets a caller
    surface the state without a try/except."""
    for kwargs in (
        {"declared": 768, "stored": _stored((1536, 1, 1))},  # mismatch
        {"raise_active": True},
        {"declared": 768, "raise_store": True},
    ):
        result = _check(**kwargs)
        assert result.outcome in (OUTCOME_OK, OUTCOME_MISMATCH, OUTCOME_SKIPPED)


# ---------------------------------------------------------------------------
# Scope: only the active stamp
# ---------------------------------------------------------------------------


def test_only_the_active_stamp_is_queried():
    """Vectors under another stamp are never compared against these, so they may
    legitimately differ — they are the managed backfill's job, not a fault."""
    seen = []

    def _stored_fn(model, version):
        seen.append((model, version))
        return _stored((768, 10, 1))

    check_embedding_dimensions(
        active_model=lambda: ("in_boundary:nomic-embed-text", "v9"),
        declared=lambda _i: 768,
        stored_dimensions=_stored_fn,
    )
    assert seen == [("in_boundary:nomic-embed-text", "v9")]


def test_store_query_filters_on_both_stamp_columns():
    from app.retrieval.store import stored_embedding_dimensions

    sql = inspect.getsource(stored_embedding_dimensions)
    assert "embedding_model = %s" in sql
    assert "embedding_model_version = %s" in sql
    assert "embedding IS NOT NULL" in sql


# ---------------------------------------------------------------------------
# Structural — read-only, and the schema is untouched
# ---------------------------------------------------------------------------


def test_the_guard_never_writes():
    code = _GUARD_SOURCE.read_text(encoding="utf-8")
    for forbidden in ("UPDATE ", "INSERT ", "DELETE ", "ALTER ", "DROP ", "set_embedding"):
        assert forbidden not in code, f"dimension_guard.py must not {forbidden.strip()}"


def test_the_store_read_is_read_only():
    from app.retrieval.store import stored_embedding_dimensions

    sql = inspect.getsource(stored_embedding_dimensions)
    for forbidden in ("UPDATE", "INSERT", "DELETE", "ALTER", "DROP"):
        assert forbidden not in sql.upper().replace("READ-ONLY", "")


def test_the_vector_column_stays_dimensionless():
    """The pack's verification note: this column is a good decision, not a defect.

    HP-2.4 adds a startup CHECK; it must not pin the column, which would cost the
    model portability the comment exists to protect.
    """
    ddl = _RETRIEVAL_MODEL.read_text(encoding="utf-8")
    assert "embedding               public.vector," in ddl, (
        "the embedding column must remain an unqualified, dimensionless vector"
    )
    assert "vector(" not in ddl.replace("public.vector,", "")


def test_the_guard_is_wired_into_startup():
    from app import main as main_module

    source = inspect.getsource(main_module.lifespan)
    assert "validate_embedding_dimensions" in source
    # After the provider config is validated: the active model must be resolvable
    # before its dimension can mean anything.
    assert source.index("validate_provider_config") < source.index(
        "validate_embedding_dimensions"
    )


def test_outcome_serialises_without_vector_content():
    result = _check(declared=768, stored=_stored((768, 5, 1)))
    payload = result.to_dict()
    assert set(payload) == {
        "outcome", "modelIdentity", "modelVersion", "declaredDimensions",
        "stored", "skipReason", "detail",
    }
    # 'stored' carries counts only — never an embedding, never chunk content.
    for row in payload["stored"]:
        assert set(row) == {"dimensions", "chunks", "orgs"}


def test_mismatch_is_profile_independent(monkeypatch):
    """Unlike HP-2.3's reachability probe this is not environmental: the model
    provably cannot write into the index, whoever operates the deployment."""
    for profile in ("saas", "customer_hosted"):
        monkeypatch.setenv("DEPLOYMENT_PROFILE", profile)
        with pytest.raises(EmbeddingDimensionMismatch):
            validate_embedding_dimensions(
                active_model=lambda: ("in_boundary:nomic-embed-text", "1"),
                declared=lambda _i: 768,
                stored_dimensions=lambda _m, _v: _stored((1536, 1, 1)),
            )


def test_guard_does_not_read_the_deployment_profile():
    """Structural companion to the test above."""
    code = _GUARD_SOURCE.read_text(encoding="utf-8")
    assert "DEPLOYMENT_PROFILE" not in code
    assert "is_customer_hosted" not in code
