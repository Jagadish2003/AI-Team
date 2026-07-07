"""R18-B1 T7 — Confirm the R16-D1 no-bypass test covers the retrieval package.

R16-D1 established one hard rule: the model gateway package
(``backend/app/model_gateway/``) is the ONLY code permitted to reference a
model-provider endpoint, SDK, or API-key header. Every model call — generation
AND embedding — routes through it, so hosted, in-boundary, and customer-tenant
deployments embed inside the customer's control automatically. For a sovereignty
customer a single direct embedding call outside the gateway is a data breach, not
just a style violation (R18-B1 §Architecture, AC2).

R18-B1 adds a greenfield package — ``backend/app/retrieval/`` — whose embedding
pipeline (``embedder.py``) and query path (``api.py``) both produce embeddings.
Those embeddings MUST route through ``get_embedding_provider()`` / the gateway
``embed()`` wrapper and never through a direct embedding API. This suite confirms
the R16-D1 no-bypass guarantee reaches the new package (T7 / AC2):

  T7-A  The canonical R16-D1 scan COVERS the retrieval package (and the T3
        embedding worker): its files are among the scanned, non-gateway targets,
        so the enforcement is not silently skipping the release's critical-path
        code. Guards against a future change that excludes retrieval from the scan.
  T7-B  The retrieval package + embedding worker currently pass the canonical
        R16-D1 scan — adding the substrate introduced no direct provider call.
  T7-C  A stronger, embedding-specific guard: the canonical R16-D1 patterns are
        generation-centric (the hosted endpoint, the API-key header, and the
        generation SDK-method literals), so a direct embedding-ONLY SDK or
        endpoint (a hosted-style /embeddings HTTP path, an SDK embeddings
        create-method, a local ``SentenceTransformer``, ``voyageai``, ``cohere``)
        would slip past them. This asserts the retrieval package and the worker
        contain none of those either.
  T7-D  A NEW direct embedding call added anywhere in the retrieval package is
        caught immediately, so a future regression fails the build (mirrors
        R16-D1 T4-AC4). A gateway-only call is NOT flagged (no false positives).
  T7-E  Sanity: the retrieval embedder's only embedding path is the gateway —
        it imports ``get_embedding_provider`` / gateway ``embed`` and no direct
        provider SDK — so the scan-based guarantee matches the real import graph.

This file reuses the canonical scanner from ``test_model_gateway_no_bypass``
rather than reimplementing the forbidden-pattern list, so there is a single
source of truth. Every forbidden pattern it defines of its own is built by string
concatenation so this file never self-trips either scan.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import List, Tuple

# Reuse the canonical R16-D1 scanner — single source of truth for the forbidden
# generation-provider patterns and the collection/scan logic (no duplicated
# literals here). pytest's prepend import mode + rootdir put tests/contract on the
# import path, so the sibling module imports by its package-qualified name.
from tests.contract.test_model_gateway_no_bypass import (
    BACKEND_ROOT,
    GATEWAY_PACKAGE,
    _collect_scan_targets,
    _scan_file,
)

# ---------------------------------------------------------------------------
# Paths — the R18-B1 code this task guards.
# ---------------------------------------------------------------------------

# The greenfield retrieval substrate package (chunking / embedder / store / api).
RETRIEVAL_PACKAGE: Path = BACKEND_ROOT / "app" / "retrieval"
# The T3 async embedding worker — the other piece of "this story's code" that
# drives embedding; it must be gateway-only for the same reason.
EMBEDDING_WORKER: Path = BACKEND_ROOT / "app" / "jobs" / "embedding_worker.py"

# ---------------------------------------------------------------------------
# Embedding-specific forbidden patterns (T7-C).
#
# The canonical R16-D1 list catches hosted generation-style calls (the hosted
# endpoint, the API-key header, the generation SDK, its create-method, and the
# version header). An embedding-only bypass need not touch any of those — a local
# model or a dedicated embeddings SDK/endpoint would not. These patterns close
# that gap for the embedding path. Each is concatenated so this file does not
# self-match under either scan. Matching is case-insensitive, consistent with the
# R16-D1 scanner.
# ---------------------------------------------------------------------------

_PAT_EMB_HTTP_PATH = "/v1/" + "embeddings"          # hosted-style embeddings REST path
_PAT_EMB_SDK_CALL = "embeddings" + ".create"        # hosted-provider SDK embeddings method
_PAT_LOCAL_MODEL = "Sentence" + "Transformer"       # local/self-hosted embedding model
_PAT_VOYAGE = "voyage" + "ai"                        # Voyage AI embeddings SDK
_PAT_COHERE = "cohere" + ".embed"                    # Cohere embeddings SDK
_PAT_LC_EMBED_DOCS = ".embed_" + "documents"         # LangChain embedding method
_PAT_LC_EMBED_QUERY = ".embed_" + "query"            # LangChain embedding method

EMBEDDING_FORBIDDEN_PATTERNS: List[str] = [
    _PAT_EMB_HTTP_PATH,
    _PAT_EMB_SDK_CALL,
    _PAT_LOCAL_MODEL,
    _PAT_VOYAGE,
    _PAT_COHERE,
    _PAT_LC_EMBED_DOCS,
    _PAT_LC_EMBED_QUERY,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _retrieval_scope_files() -> List[Path]:
    """Every .py file this task guards: the retrieval package + embedding worker."""
    files = [p for p in RETRIEVAL_PACKAGE.rglob("*.py") if "__pycache__" not in p.parts]
    if EMBEDDING_WORKER.exists():
        files.append(EMBEDDING_WORKER)
    return files


def _scan_for(path: Path, patterns: List[str]) -> List[Tuple[int, str, str]]:
    """Return (line_number, line_text, matched_pattern) for every violation."""
    violations: List[Tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return violations
    for lineno, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        for pattern in patterns:
            if pattern.lower() in lowered:
                violations.append((lineno, line.rstrip(), pattern))
                break
    return violations


# ===========================================================================
# T7-A — the canonical R16-D1 scan COVERS the retrieval package + worker
# ===========================================================================


def test_retrieval_package_is_covered_by_r16d1_scan():
    """The retrieval package's .py files are among the R16-D1 scan targets.

    The whole point of T7: confirm the no-bypass enforcement actually reaches the
    new package. If a future change moved retrieval under the gateway, added it to
    the scanner's skip list, or otherwise dropped it, this fails — the enforcement
    would be silently skipping the release's critical-path code.
    """
    scan_targets = set(_collect_scan_targets())
    retrieval_targets = {
        p for p in scan_targets if RETRIEVAL_PACKAGE in p.parents
    }

    # Non-vacuous: the substrate's real modules must all be covered.
    expected = {"__init__.py", "chunking.py", "embedder.py", "store.py", "api.py"}
    covered = {p.name for p in retrieval_targets}
    missing = expected - covered
    assert not missing, (
        "R16-D1 no-bypass scan does not cover these retrieval modules: "
        f"{sorted(missing)}. The enforcement must scan every file in "
        "backend/app/retrieval/."
    )


def test_embedding_worker_is_covered_by_r16d1_scan():
    """The T3 async embedding worker is also among the R16-D1 scan targets."""
    assert EMBEDDING_WORKER.exists(), "the embedding worker job must exist (R18-B1 T3)"
    scan_targets = set(_collect_scan_targets())
    assert EMBEDDING_WORKER.resolve() in {p.resolve() for p in scan_targets}, (
        "the retrieval embedding worker must be covered by the R16-D1 no-bypass scan"
    )


def test_retrieval_package_is_not_inside_the_gateway_boundary():
    """The retrieval package lives OUTSIDE the gateway, so it must be scanned.

    Gateway files are (correctly) excluded from the scan — they are the one
    permitted home for provider literals. Retrieval is application code: it must be
    on the enforced side of that boundary, never excluded.
    """
    gateway = GATEWAY_PACKAGE.resolve()
    assert gateway not in RETRIEVAL_PACKAGE.resolve().parents, (
        "backend/app/retrieval/ must not live inside the gateway package — it is "
        "application code subject to the no-bypass scan, not permitted-location code."
    )


# ===========================================================================
# T7-B — the retrieval package + worker pass the canonical R16-D1 scan
# ===========================================================================


def test_retrieval_scope_has_no_direct_model_calls():
    """No retrieval-package or worker file contains a direct model-provider
    reference under the canonical R16-D1 patterns.

    This is the R16-D1 guarantee re-confirmed for the retrieval substrate: adding
    the package introduced no hosted-style direct model call. Route all embedding
    through get_embedding_provider() / the gateway embed() wrapper.
    """
    violations: List[str] = []
    for py_file in _retrieval_scope_files():
        for lineno, line, pattern in _scan_file(py_file):
            rel = py_file.relative_to(BACKEND_ROOT)
            violations.append(f"  {rel}:{lineno}: [{pattern!r}]  {line}")

    assert not violations, (
        "Direct model-provider references found in the retrieval substrate.\n"
        "Route every model call through the gateway (get_embedding_provider()).\n\n"
        "Violations:\n" + "\n".join(violations)
    )


# ===========================================================================
# T7-C — stronger embedding-specific guard over the retrieval scope
# ===========================================================================


def test_retrieval_scope_has_no_direct_embedding_calls():
    """No retrieval-package or worker file contains a direct embedding SDK/endpoint.

    The canonical R16-D1 patterns are generation-centric; an embedding-only bypass
    (a local SentenceTransformer, a hosted-style /embeddings path, an SDK
    embeddings create-method, voyageai, cohere, LangChain embed_*) need not touch
    any of them. This closes that gap: the embedding path stays gateway-only (AC2).
    """
    violations: List[str] = []
    for py_file in _retrieval_scope_files():
        for lineno, line, pattern in _scan_for(py_file, EMBEDDING_FORBIDDEN_PATTERNS):
            rel = py_file.relative_to(BACKEND_ROOT)
            violations.append(f"  {rel}:{lineno}: [{pattern!r}]  {line}")

    assert not violations, (
        "Direct embedding-provider references found in the retrieval substrate.\n"
        "Embeddings are model calls — produce them ONLY through "
        "get_embedding_provider() / the gateway embed() wrapper.\n\n"
        "Violations:\n" + "\n".join(violations)
    )


# ===========================================================================
# T7-D — a new embedding bypass is caught immediately (mirrors R16-D1 T4-AC4)
# ===========================================================================


def test_new_direct_embedding_call_in_retrieval_is_detected(tmp_path):
    """A new file that makes a direct embedding call is flagged by the combined
    scan — proving a future embedding bypass in the retrieval package fails the
    build. Content is built at runtime so this test file does not self-match.
    """
    bypass_file = tmp_path / "rogue_embedder.py"
    sdk_call = "embeddings" + ".create"
    local_model = "Sentence" + "Transformer"
    bypass_file.write_text(
        textwrap.dedent(
            f"""\
            # A direct, gateway-bypassing embedding call.
            from vendor_client import client
            vecs = client.{sdk_call}(model="text-embedding-3-small", input=texts)
            local = {local_model}("all-MiniLM-L6-v2")
            """
        ),
        encoding="utf-8",
    )

    violations = _scan_for(bypass_file, EMBEDDING_FORBIDDEN_PATTERNS)
    assert violations, "scanner failed to detect a direct embedding bypass"
    detected = {v[2] for v in violations}
    assert detected.issubset(set(EMBEDDING_FORBIDDEN_PATTERNS))


def test_new_hosted_call_in_retrieval_is_detected_by_canonical_scan(tmp_path):
    """A hosted-style direct call is caught by the canonical R16-D1 scanner too —
    the retrieval package is on the enforced side of the boundary for BOTH the
    generation and embedding pattern sets."""
    bypass_file = tmp_path / "rogue_caller.py"
    forbidden_header = "x-api-" + "key"
    bypass_file.write_text(
        f'HEADERS = {{"{forbidden_header}": "sk-..."}}\n', encoding="utf-8"
    )
    assert _scan_file(bypass_file), (
        "canonical R16-D1 scanner failed to detect a hosted-style direct call"
    )


def test_gateway_only_embedding_call_is_not_flagged(tmp_path):
    """A file that embeds ONLY through the gateway is NOT flagged — the guard does
    not produce false positives on compliant retrieval code."""
    clean_file = tmp_path / "good_embedder.py"
    clean_file.write_text(
        textwrap.dedent(
            """\
            from app.model_gateway import embed, get_embedding_provider

            identity = get_embedding_provider().embedding_identity()
            vectors = embed(["chunk one", "chunk two"])
            """
        ),
        encoding="utf-8",
    )
    assert _scan_for(clean_file, EMBEDDING_FORBIDDEN_PATTERNS) == []
    assert _scan_file(clean_file) == []


# ===========================================================================
# T7-E — sanity: the embedder's only embedding path is the gateway
# ===========================================================================


def test_scan_scope_is_non_empty():
    """Guard against a vacuous pass: the retrieval scope must cover real files, so
    a clean result means 'checked', not 'checked nothing'."""
    files = _retrieval_scope_files()
    names = {p.name for p in files}
    assert {"embedder.py", "api.py", "store.py", "chunking.py"} <= names, (
        f"expected the retrieval modules in the scan scope; got {sorted(names)}"
    )
    assert EMBEDDING_WORKER.name in names, "the embedding worker must be in scope"


def test_embedder_embedding_path_is_the_gateway():
    """The embedder resolves embeddings through the gateway and nothing else.

    A positive complement to the negative scans: the real import graph confirms the
    only embedding entry point is the gateway (``get_embedding_provider`` and the
    instrumented ``embed``), so the no-bypass guarantee matches how the code works.
    """
    embedder_src = (RETRIEVAL_PACKAGE / "embedder.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "from app.model_gateway import" in embedder_src, (
        "embedder.py must obtain its embedding entry point from app.model_gateway"
    )
    assert "get_embedding_provider" in embedder_src
    # And it defines no direct-provider embedding call of its own.
    assert _scan_for(RETRIEVAL_PACKAGE / "embedder.py", EMBEDDING_FORBIDDEN_PATTERNS) == []
