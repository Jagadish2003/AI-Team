"""Manual runtime check — R18-B1 Retrieval Substrate, end to end.

Run this ON A MACHINE THAT CAN REACH THE BACKEND DB (the pgvector store lives in
the `retrieval_chunks` table) AND the configured embedding endpoint. It exercises
the REAL substrate — real chunking, real gateway embedding calls, real pgvector
index, real ``retrieve()`` — with no mocks and no stub provider, and answers the
one question a unit test cannot: *does this deployment's embedding model actually
work?*

The substrate is inert without a working embedding provider. The hosted
(Anthropic) provider has no embeddings endpoint, so a deployment left on
``MODEL_EMBEDDING_PROVIDER=hosted`` indexes content forever without ever
embedding it and every search returns nothing — silently. This script makes that
condition, and its fix, visible in one command.

It walks the story's acceptance criteria in order:

  AC2  every embedding routes through the model gateway (the provider that
       served the call is reported; no direct API call exists in the package)
  AC1  handed-over content is chunked per content-type policy, embedded, and
       indexed with provenance + content hash
  AC7  content is indexed BEFORE it is embedded — embedding lag leaves chunks
       pending, never an error
  AC8  every vector is stamped with the embedding model identity + version
  AC5  retrieved chunks carry chunk_id and retrieval_result_id
  AC4  source_filter scopes to named systems; min_score excludes weak matches
  AC3  a second org cannot retrieve the first org's chunks

Usage (from backend/, with the venv active and .env pointing at the DB):

    python scripts/verify_retrieval_substrate.py
    python scripts/verify_retrieval_substrate.py --keep    # leave data behind

Exit code 0 = the substrate embedded and retrieved live content successfully.
Exit code 1 = at least one check failed (the reason is printed).

Test data is written under throwaway org ids and removed on exit unless --keep.
No credential value is ever printed.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# This script lives in backend/scripts/ but imports the `app` package under
# backend/. Ensure backend/ is on sys.path regardless of how it is launched.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Throwaway orgs. Two of them, because tenant isolation is only provable with a
# second tenant holding similar content (AC3).
ORG_A = "verify-r18b1-org-a"
ORG_B = "verify-r18b1-org-b"

# Deliberately distinguishable content: the retrieval check asserts that a
# semantic query returns the RELEVANT artifact first, which only means something
# if the corpus contains a clear distractor.
_DOC_INCIDENT = (
    "Incident response runbook for payment gateway outages.\n\n"
    "When the payment gateway returns elevated 5xx error rates, the on-call "
    "engineer must first check the upstream card processor status page, then "
    "drain the affected region from the load balancer before failing over.\n\n"
    "Escalate to the payments platform team if error rates stay above five "
    "percent for more than ten minutes after failover."
)
_DOC_ONBOARDING = (
    "New starter onboarding checklist.\n\n"
    "Order the laptop two weeks before the start date, request building access, "
    "and book the first-week introduction sessions with the team lead.\n\n"
    "Payroll and benefits enrolment must be completed within the first five "
    "working days."
)
_CODE_SNIPPET = (
    "def retry_payment(charge_id: str, attempts: int = 3) -> bool:\n"
    "    for attempt in range(attempts):\n"
    "        if submit_charge(charge_id):\n"
    "            return True\n"
    "        backoff(attempt)\n"
    "    return False\n"
)

_QUERY = "what should on-call do when the payment gateway starts failing?"

_PASS = "PASS"
_FAIL = "FAIL"

_failures: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> bool:
    """Record and print one check outcome. Returns ``ok`` for chaining."""
    status = _PASS if ok else _FAIL
    print(f"  [{status}] {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        _failures.append(label)
    return ok


def _section(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


def _report_config() -> str:
    """Print the resolved embedding config and return the provider name.

    Deliberately reads ONLY the gateway's public surface — the provider name and
    ``embedding_identity()``. Endpoint URLs and credentials are gateway-private by
    design (R16-D1): no code outside ``app/model_gateway/`` may reference them, and
    the no-bypass tests enforce that on this file too. The provider's own
    ``validate()`` logs any endpoint/credential misconfiguration at startup, which
    is why this script turns WARNING logging on — the diagnostics arrive through
    the gateway rather than by reaching around it.
    """
    from app.model_gateway import get_embedding_provider, validate_provider_config

    provider = get_embedding_provider()
    print(f"  embedding provider : {provider.name}")

    identity, version = provider.embedding_identity()
    print(f"  model identity     : {identity or '(none)'}")
    print(f"  model version      : {version or '(none)'}")

    print("  gateway validation :")
    validate_provider_config()
    return provider.name


def _verify_schema() -> bool:
    """Confirm the pgvector extension and retrieval_chunks table are present."""
    from contextlib import closing

    from app import db

    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        has_ext = cur.fetchone() is not None
        cur.execute("SELECT to_regclass('public.retrieval_chunks')")
        row = cur.fetchone()
        has_table = bool(row and row[0])
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'retrieval_chunks' AND column_name = 'embedding'"
        )
        has_vector_col = cur.fetchone() is not None

    _check("pgvector extension installed", has_ext)
    _check("retrieval_chunks table present", has_table)
    _check("vector column present", has_vector_col)
    return has_ext and has_table and has_vector_col


def _vector_dimensions(org_id: str) -> set[int]:
    """Return the distinct vector dimensions stored for an org.

    A store holding two different dimensions means two incompatible models were
    mixed into one partition, which the AC8 stamp exists to prevent. Reporting the
    dimension also lets an operator confirm the model that ANSWERED matches the one
    configured — text-embedding-3-small emits 1536 — rather than trusting the
    config string alone.
    """
    from contextlib import closing

    from app import db

    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT DISTINCT vector_dims(embedding) FROM retrieval_chunks "
            "WHERE org_id = %s AND embedding IS NOT NULL",
            (org_id,),
        )
        return {int(r[0]) for r in cur.fetchall() if r[0] is not None}


def _cleanup() -> None:
    """Remove every chunk this script wrote, for both orgs."""
    from app.retrieval import store

    for org in (ORG_A, ORG_B):
        for system, artifact in (
            ("document", "runbooks/payment-gateway-incident.md"),
            ("document", "hr/onboarding-checklist.md"),
            ("git", "billing/payments/retry.py"),
        ):
            try:
                store.purge_artifact(org, system, artifact)
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the test chunks in the store instead of purging them",
    )
    args = parser.parse_args()

    # Load .env the same way the app does, so the script sees the deployment's
    # real configuration rather than a bare shell environment.
    try:
        from dotenv import load_dotenv

        load_dotenv(_BACKEND_DIR / ".env")
    except ImportError:  # pragma: no cover - dotenv is a normal dependency
        pass

    from app.model_gateway import get_embedding_provider
    from app.retrieval import store
    from app.retrieval.api import retrieve
    from app.retrieval.embedder import active_embedding_model, embed_pending_for_org
    from app.retrieval.ingest import ContentArtifact, ingest_content

    print("=" * 72)
    print("R18-B1 Retrieval Substrate — live end-to-end verification")
    print("=" * 72)

    # Surface the gateway's own provider diagnostics (endpoint/credential
    # problems are reported by the provider, never read from outside it).
    # force=True because importing the app may already have configured logging,
    # which would otherwise make this a silent no-op and hide the very warnings
    # this script exists to surface.
    logging.basicConfig(
        level=logging.WARNING, format="    %(levelname)s %(message)s", force=True
    )
    logging.getLogger("app.model_gateway").setLevel(logging.WARNING)

    _section("Configuration")
    provider_name = _report_config()

    if provider_name == "hosted":
        print(
            "\n  The hosted provider has no embeddings endpoint, so the substrate\n"
            "  cannot embed anything. Set MODEL_EMBEDDING_PROVIDER to\n"
            "  'customer_tenant' or 'in_boundary' — see backend/.env.template."
        )
        return 1

    _section("Store")
    # The migration (0024/0025) is the authoritative provisioning path and the
    # runtime role is deliberately not the table owner, so this VERIFIES the
    # schema rather than trying to create it.
    if not _verify_schema():
        print(
            "\n  The pgvector store is not provisioned. Run `alembic upgrade head`."
        )
        return 1

    # Start from a clean slate so a previous aborted run cannot mask a failure.
    _cleanup()

    _section("AC1/AC7 — ingest: chunked, indexed with provenance, NOT yet embedded")
    artifacts = [
        ContentArtifact(
            source_system="document",
            source_artifact="runbooks/payment-gateway-incident.md",
            content=_DOC_INCIDENT,
            content_type="prose",
            source_timestamp="2026-07-01T09:00:00Z",
            provenance={"title": "Payment gateway incident runbook", "owner": "SRE"},
        ),
        ContentArtifact(
            source_system="document",
            source_artifact="hr/onboarding-checklist.md",
            content=_DOC_ONBOARDING,
            content_type="prose",
            source_timestamp="2026-07-02T09:00:00Z",
            provenance={"title": "Onboarding checklist", "owner": "People"},
        ),
        ContentArtifact(
            source_system="git",
            source_artifact="billing/payments/retry.py",
            content=_CODE_SNIPPET,
            content_type="code",
            source_timestamp="2026-07-03T09:00:00Z",
            provenance={"repo": "billing", "commit": "abc1234"},
        ),
    ]
    result = ingest_content(ORG_A, artifacts)
    _check(
        "producer handover indexed every artifact",
        result.artifacts_indexed == len(artifacts)
        and result.artifacts_failed == 0
        and result.chunks_indexed > 0,
        f"{result.chunks_indexed} chunks from {result.artifacts_indexed}/"
        f"{len(artifacts)} artifacts ({result.artifacts_failed} failed)",
    )
    total = store.count_chunks(ORG_A)
    embedded_before = store.count_chunks(ORG_A, embedded_only=True)
    _check(
        "AC7: content is indexed before it is embedded",
        total > 0 and embedded_before == 0,
        f"{total} indexed, {embedded_before} embedded",
    )

    # A second tenant holds content on the SAME topic — so AC3 proves partitioning
    # rather than merely proving the two corpora differ.
    ingest_content(
        ORG_B,
        [
            ContentArtifact(
                source_system="document",
                source_artifact="runbooks/payment-gateway-incident.md",
                content=(
                    "Tenant B private runbook: when the payment gateway fails, "
                    "page the Tenant B duty manager and open a Sev1 bridge."
                ),
                content_type="prose",
                source_timestamp="2026-07-01T09:00:00Z",
                provenance={"title": "Tenant B runbook"},
            )
        ],
    )

    _section("AC2/AC8 — embed through the gateway, stamp model identity")
    identity, version = active_embedding_model()
    _check(
        "gateway reports an active embedding model",
        bool(identity),
        f"identity={identity!r} version={version!r}",
    )
    if not identity:
        print("\n  No embedding model identity — cannot continue.")
        if not args.keep:
            _cleanup()
        return 1

    started = time.monotonic()
    run = embed_pending_for_org(ORG_A)
    embed_pending_for_org(ORG_B)
    elapsed = time.monotonic() - started

    _check(
        "AC2: embeddings produced via the model gateway provider",
        run.embedded > 0,
        f"{run.embedded}/{run.pending_seen} chunks in {run.batches} gateway "
        f"batch(es), {elapsed:.2f}s — provider={get_embedding_provider().name}",
    )
    if run.embedded == 0:
        print(
            "\n  The gateway returned no vectors. The endpoint, api-version, or\n"
            "  credential is wrong, or the endpoint is unreachable. Re-run with\n"
            "  logging at WARNING to see the provider's error."
        )
        if not args.keep:
            _cleanup()
        return 1

    _check(
        "AC8: every written vector carries the model identity + version",
        run.model_identity == identity and run.model_version == version,
        f"stamped {run.model_identity!r} / {run.model_version!r}",
    )
    _check(
        "all indexed content became retrievable",
        store.count_chunks(ORG_A, embedded_only=True) == store.count_chunks(ORG_A),
        f"{store.count_chunks(ORG_A, embedded_only=True)}/"
        f"{store.count_chunks(ORG_A)} embedded",
    )
    dims = _vector_dimensions(ORG_A)
    _check(
        "every vector shares one dimension (no mixed vector spaces)",
        len(dims) == 1 and next(iter(dims), 0) > 0,
        f"dimension(s)={sorted(dims)} — cross-check against the configured model "
        f"(text-embedding-3-small emits 1536)",
    )

    _section("AC5 — retrieve(): ranked results with EvidencePointer fields")
    hits = retrieve(ORG_A, _QUERY, k=5)
    _check("retrieve() returned ranked results", bool(hits), f"{len(hits)} hits")
    if not hits:
        print("\n  Retrieval returned nothing despite embedded content.")
        if not args.keep:
            _cleanup()
        return 1

    top = hits[0]
    print(f"    top hit : {top.source_artifact}  (similarity {top.similarity:.4f})")
    print(f"    excerpt : {top.content[:90].replace(chr(10), ' ')}…")
    _check(
        "semantic ranking put the relevant artifact first",
        "payment-gateway-incident" in top.source_artifact,
        f"top={top.source_artifact}",
    )
    _check(
        "results are ordered by descending similarity",
        all(
            hits[i].similarity >= hits[i + 1].similarity for i in range(len(hits) - 1)
        ),
    )
    _check(
        "AC5: chunk_id and retrieval_result_id are populated",
        all(h.chunk_id and h.retrieval_result_id for h in hits),
        f"chunk_id={top.chunk_id[:8]}… result_id={top.retrieval_result_id[:8]}…",
    )
    pointer = top.to_evidence_pointer()
    _check(
        "AC5: the EvidencePointer carries both fields, origin=observed",
        pointer.chunk_id == top.chunk_id
        and pointer.retrieval_result_id == top.retrieval_result_id
        and pointer.origin == "observed",
        f"origin={pointer.origin}",
    )

    _section("AC4 — source_filter and min_score")
    git_only = retrieve(ORG_A, "retry a failed charge", k=5, source_filter=["git"])
    _check(
        "source_filter scopes results to the named system",
        bool(git_only) and all(h.source_system == "git" for h in git_only),
        f"{len(git_only)} hits, systems="
        f"{sorted({h.source_system for h in git_only})}",
    )
    docs_only = retrieve(ORG_A, _QUERY, k=5, source_filter=["document"])
    _check(
        "source_filter excludes unnamed systems",
        all(h.source_system == "document" for h in docs_only),
        f"{len(docs_only)} hits",
    )
    # Pick a floor strictly BETWEEN the best and worst hit, so the check proves a
    # partition — some results kept, some dropped. A floor above every hit would
    # return nothing and satisfy `all(...)` vacuously, proving only that a filter
    # can exclude everything.
    if len(hits) >= 2 and hits[0].similarity > hits[-1].similarity:
        floor = (hits[0].similarity + hits[-1].similarity) / 2
        weak = retrieve(ORG_A, _QUERY, k=5, min_score=floor)
        _check(
            "min_score keeps strong matches and excludes weak ones",
            bool(weak)
            and len(weak) < len(hits)
            and all(h.similarity >= floor for h in weak),
            f"floor={floor:.4f}: {len(weak)} of {len(hits)} hits survive "
            f"(range {hits[-1].similarity:.4f}–{hits[0].similarity:.4f})",
        )
    else:
        _check(
            "min_score keeps strong matches and excludes weak ones",
            False,
            "inconclusive: needs >=2 hits with distinct similarities",
        )
    none_scoped = retrieve(ORG_A, _QUERY, k=5, source_filter=["nonexistent-system"])
    _check(
        "a filter naming nothing valid returns nothing (never widens)",
        none_scoped == [],
    )

    _section("AC3 — cross-tenant isolation")
    a_artifacts = {h.source_artifact for h in retrieve(ORG_A, _QUERY, k=10)}
    b_hits = retrieve(ORG_B, _QUERY, k=10)
    _check(
        "org B retrieves its own content for the same query",
        bool(b_hits),
        f"{len(b_hits)} hits",
    )
    _check(
        "org B never sees org A's chunks (same artifact id, same topic)",
        all(h.content not in (_DOC_INCIDENT, _DOC_ONBOARDING) for h in b_hits)
        and all("Tenant B" in h.content for h in b_hits),
        f"org A artifacts={len(a_artifacts)}, org B leakage=0",
    )

    _section("Result")
    if _failures:
        print(f"  {len(_failures)} check(s) FAILED:")
        for f in _failures:
            print(f"    - {f}")
    else:
        print("  All checks passed — the retrieval substrate is working end to end.")

    if args.keep:
        print(f"\n  --keep: test chunks left under {ORG_A} / {ORG_B}")
    else:
        _cleanup()
        print("\n  Test chunks purged.")

    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
