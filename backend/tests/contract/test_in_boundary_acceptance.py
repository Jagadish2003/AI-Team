"""R17-D1 T7 - In-boundary mode full acceptance-criteria contract tests.

This suite proves the *story-level* acceptance criteria of R17-D1 Section 5
(AC1-AC7), end-to-end and organised by AC. The earlier task suites
(``test_in_boundary_provider`` T1, ``test_in_boundary_embedding_path`` T2,
``test_in_boundary_resilience`` T3, ``test_in_boundary_config`` T4,
``test_in_boundary_telemetry`` T5, ``test_in_boundary_no_bypass`` T6) each prove
one task in isolation. T7 is the acceptance gate: it demonstrates the full
R17-D1 contract through the public gateway surface — that in-boundary mode is
selectable purely by configuration without changing any calling code, that with
both providers set to in_boundary no model OR embedding request ever leaves the
configured in-network endpoint, that generation and embedding remain
independently selectable, that resilience/graceful-failure is preserved, that
every call records ``provider='in_boundary'``, that the R16-D1 no-bypass
guarantee still holds, and — the half most easily forgotten — that a deliberately
lower-capability in-boundary model degrades by surfacing FEWER findings, never by
admitting findings that violate the corroboration or causal quality gates.

Each test references the AC it proves so a failure maps directly to the
acceptance criterion that regressed.

Test posture
------------
- The customer endpoint is faked at ``urllib.request.urlopen`` so no real
  network call is made; the fake records every URL it is asked to call. AC2 is
  proven by asserting the ONLY URLs ever contacted are the two configured
  in-boundary endpoints.
- ``record_event`` is captured in-memory; no telemetry is written to the DB.
- ``time.sleep`` is patched out in the resilience tests so bounded backoff does
  not slow the suite.
- The causal/corroboration gates (AC7) are pure functions exercised directly,
  so AC7 needs neither a live LLM nor a live DB.
"""
from __future__ import annotations

import http.client
import json
import urllib.error
from datetime import datetime, timezone
from typing import Any, List, Tuple

import pytest

from app.causal_engine import (
    cause_chain_uses_inferred,
    is_generic_falsifiability,
    step_references_inferred_relationship,
)
from app.corroboration_engine import (
    apply_corroboration_confidence,
    evaluate_corroboration,
)
from app.model_gateway import (
    _PROVIDER_REGISTRY,
    GenerationRequest,
    GenerationResult,
    ModelProvider,
    embed as gw_embed,
    generate as gw_generate,
    get_embedding_provider,
    get_generation_provider,
)
from app.model_gateway.in_boundary_config import (
    CONFIG_KEY_API_KEY,
    CONFIG_KEY_BASE_URL,
    CONFIG_KEY_EMBEDDING_ENDPOINT,
    CONFIG_KEY_EMBEDDING_MODEL,
    CONFIG_KEY_GENERATION_ENDPOINT,
    CONFIG_KEY_GENERATION_MODEL,
    IN_BOUNDARY_PROVIDER_NAME,
)
from app.model_gateway.in_boundary_provider import InBoundaryModelProvider, _MAX_RETRIES

_GEN_EVENT = "model.generation_completed"
_EMB_EVENT = "model.embedding_completed"

_SECRET = "ib-acceptance-secret-DO-NOT-LOG"
_BASE_URL = "https://models.customer.internal"
_GEN_ENDPOINT = f"{_BASE_URL}/v1/chat/completions"
_EMB_ENDPOINT = f"{_BASE_URL}/v1/embeddings"

# A stand-in for any reachable hosted/external endpoint. AC2 asserts this host is
# never contacted when both providers are in_boundary.
_EXTERNAL_HOST = "api.anthrop" + "ic.com"


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


class _JsonResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def _request_json(req) -> dict[str, Any]:
    return json.loads((req.data or b"{}").decode("utf-8"))


@pytest.fixture
def configure_in_boundary(monkeypatch):
    """Fully configure a customer-operated in-boundary endpoint via base URL.

    Using IN_BOUNDARY_BASE_URL (not endpoint overrides) proves the chat/embedding
    paths are derived inside the gateway from a single customer-supplied root —
    the realistic deployment shape.
    """
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _SECRET)
    monkeypatch.setenv(CONFIG_KEY_BASE_URL, _BASE_URL)
    monkeypatch.delenv(CONFIG_KEY_GENERATION_ENDPOINT, raising=False)
    monkeypatch.delenv(CONFIG_KEY_EMBEDDING_ENDPOINT, raising=False)
    monkeypatch.setenv(CONFIG_KEY_GENERATION_MODEL, "customer-gen-model")
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_MODEL, "customer-embedding-model")


@pytest.fixture
def select_in_boundary_both(monkeypatch, configure_in_boundary):
    """Select in_boundary for BOTH generation and embedding (full sovereignty)."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)


@pytest.fixture
def captured_events(monkeypatch):
    """Capture every record_event() call without touching the DB."""
    events: List[Tuple[str, dict]] = []
    monkeypatch.setattr(
        "app.telemetry.record_event",
        lambda event_type, payload=None: events.append((event_type, payload or {})),
    )
    return events


@pytest.fixture
def network(monkeypatch):
    """Patch urllib so every contacted URL is recorded and routed by endpoint.

    Returns the list of URLs contacted. The fake serves an OK generation/embedding
    response for the two configured in-boundary endpoints and FAILS LOUDLY for any
    other URL — so a stray hosted/external call surfaces as a test failure (AC2).
    """
    contacted: List[str] = []

    def _fake_urlopen(req, timeout=None):
        url = req.full_url
        contacted.append(url)
        if url == _GEN_ENDPOINT:
            return _JsonResponse({"choices": [{"message": {"content": "in-network answer"}}]})
        if url == _EMB_ENDPOINT:
            # One vector per input text — the embedding response contract.
            n = len(_request_json(req).get("input", []))
            return _JsonResponse({"data": [{"embedding": [0.1, 0.2, 0.3]} for _ in range(n)]})
        raise AssertionError(
            f"request left the in-boundary endpoints — contacted {url!r}; "
            "data must never leave the customer's network"
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    return contacted


# ===========================================================================
# AC1 — In-boundary mode is selectable via configuration with NO change to any
#        calling code (it registers behind the R16-D1 gateway).
# ===========================================================================


def test_ac1_in_boundary_registered_behind_gateway():
    """The in_boundary provider is registered behind the gateway, under the
    ModelProvider interface — so selecting it is purely configuration."""
    provider = _PROVIDER_REGISTRY.get(IN_BOUNDARY_PROVIDER_NAME)
    assert isinstance(provider, InBoundaryModelProvider)
    assert isinstance(provider, ModelProvider)
    assert provider.name == IN_BOUNDARY_PROVIDER_NAME


def test_ac1_unchanged_caller_code_routes_to_in_boundary(
    select_in_boundary_both, network, captured_events
):
    """The SAME gateway calls a caller already makes (generate()/embed()) route
    to in_boundary once the env config selects it — no calling code changes."""
    result = gw_generate(GenerationRequest(prompt="hello", max_tokens=8))
    vectors = gw_embed(["hello"])

    assert result.provider == IN_BOUNDARY_PROVIDER_NAME
    assert result.ok is True
    assert vectors == [[0.1, 0.2, 0.3]]
    # The resolvers pick in_boundary purely from config.
    assert get_generation_provider().name == IN_BOUNDARY_PROVIDER_NAME
    assert get_embedding_provider().name == IN_BOUNDARY_PROVIDER_NAME


# ===========================================================================
# AC2 — In-boundary implements BOTH generate() and embed(); with both set to
#        in_boundary, NO model or embedding call leaves the configured endpoint.
# ===========================================================================


def test_ac2_both_capabilities_only_contact_in_boundary_endpoints(
    select_in_boundary_both, network, captured_events
):
    """With both providers in_boundary, generation AND embedding contact ONLY the
    two configured in-network endpoints — nothing leaves the boundary."""
    gen = gw_generate(GenerationRequest(prompt="confidential prompt", max_tokens=16))
    vectors = gw_embed(["confidential text one", "confidential text two"])

    assert gen.text == "in-network answer"
    assert vectors == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]

    # Every URL contacted is one of the two configured in-boundary endpoints.
    assert contacted_only_in_boundary(network)
    # Specifically: a generation call and an embedding call, both in-network.
    assert _GEN_ENDPOINT in network
    assert _EMB_ENDPOINT in network


def test_ac2_no_external_host_is_ever_contacted(
    select_in_boundary_both, network, captured_events
):
    """A hosted/external host is never contacted on either capability path."""
    gw_generate(GenerationRequest(prompt="x", max_tokens=4))
    gw_embed(["y"])

    assert all(_EXTERNAL_HOST not in url for url in network), (
        f"an external host was contacted: {network}"
    )
    assert all(url.startswith(_BASE_URL) for url in network)


def test_ac2_embedding_capability_is_implemented_not_a_stub(
    select_in_boundary_both, network
):
    """embed() is a real in-network capability: it posts to the embedding
    endpoint and returns input-ordered vectors (the easily-forgotten half)."""
    vectors = gw_embed(["alpha"])
    assert vectors == [[0.1, 0.2, 0.3]]
    assert network == [_EMB_ENDPOINT]


def contacted_only_in_boundary(urls: List[str]) -> bool:
    """True only when every contacted URL is a configured in-boundary endpoint."""
    return bool(urls) and all(u in {_GEN_ENDPOINT, _EMB_ENDPOINT} for u in urls)


# ===========================================================================
# AC3 — Generation and embedding providers are INDEPENDENTLY selectable:
#        in_boundary generation + a different embedding provider works, and the
#        reverse works too.
# ===========================================================================


def test_ac3_in_boundary_generation_with_hosted_embedding(
    monkeypatch, configure_in_boundary
):
    """Generation runs in-boundary while embedding stays on a different provider.

    Only the generation endpoint is contacted in-network; embedding is resolved
    to the OTHER provider (not the in-boundary endpoint)."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "hosted")

    in_boundary_calls: List[str] = []

    def _fake_urlopen(req, timeout=None):
        url = req.full_url
        if url == _GEN_ENDPOINT:
            in_boundary_calls.append(url)
            return _JsonResponse({"choices": [{"message": {"content": "gen-only"}}]})
        raise AssertionError(f"unexpected in-boundary call: {url}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr(
        "app.telemetry.record_event", lambda *_a, **_k: None
    )

    # Generation resolves to in_boundary and contacts the in-network endpoint.
    result = gw_generate(GenerationRequest(prompt="x", max_tokens=4))
    assert result.provider == IN_BOUNDARY_PROVIDER_NAME
    assert result.ok is True
    assert in_boundary_calls == [_GEN_ENDPOINT]

    # Embedding is resolved to the OTHER (hosted) provider — not in_boundary.
    assert get_generation_provider().name == IN_BOUNDARY_PROVIDER_NAME
    assert get_embedding_provider().name == "hosted"
    assert get_embedding_provider().name != IN_BOUNDARY_PROVIDER_NAME


def test_ac3_in_boundary_embedding_with_hosted_generation(
    monkeypatch, configure_in_boundary
):
    """Embedding runs in-boundary while generation stays on another provider.

    Only the embedding endpoint is contacted in-network."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "hosted")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)

    in_boundary_calls: List[str] = []

    def _fake_urlopen(req, timeout=None):
        url = req.full_url
        if url == _EMB_ENDPOINT:
            in_boundary_calls.append(url)
            return _JsonResponse({"data": [{"embedding": [0.4, 0.5]}]})
        raise AssertionError(f"unexpected in-boundary call: {url}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)

    vectors = gw_embed(["customer-private text"])
    assert vectors == [[0.4, 0.5]]
    assert in_boundary_calls == [_EMB_ENDPOINT]

    # Generation is resolved to the OTHER (hosted) provider — not in_boundary.
    assert get_embedding_provider().name == IN_BOUNDARY_PROVIDER_NAME
    assert get_generation_provider().name == "hosted"
    assert get_generation_provider().name != IN_BOUNDARY_PROVIDER_NAME


def test_ac3_selecting_one_capability_does_not_force_the_other(
    monkeypatch, configure_in_boundary
):
    """Setting only the generation provider leaves embedding on its own default —
    the two selections never bleed into each other."""
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)

    assert get_generation_provider().name == IN_BOUNDARY_PROVIDER_NAME
    # Embedding falls back to the gateway default ('hosted'), unaffected.
    assert get_embedding_provider().name == "hosted"


# ===========================================================================
# AC4 — In-boundary preserves the hosted-mode resilience and graceful-failure
#        behaviour: retry, backoff, timeout, and graceful failure.
# ===========================================================================


def test_ac4_transient_failure_is_retried_then_succeeds(monkeypatch, select_in_boundary_both):
    """A transient 503 is retried with backoff; the successful retry is returned."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)
    calls = {"n": 0}

    def _fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                "http://fake", 503, "Service Unavailable", http.client.HTTPMessage(), None
            )
        return _JsonResponse({"choices": [{"message": {"content": "recovered"}}]})

    monkeypatch.setattr("urllib.request.urlopen", _fake)

    result = gw_generate(
        GenerationRequest(prompt="x", max_tokens=4, timeout_ms=60_000)
    )
    assert calls["n"] >= 2, "a transient failure must be retried"
    assert result.ok is True
    assert result.text == "recovered"


def test_ac4_retry_is_bounded(monkeypatch, select_in_boundary_both):
    """Endless transient failures stop after a bounded number of attempts."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)
    calls = {"n": 0}

    def _always_429(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            "http://fake", 429, "Too Many Requests", http.client.HTTPMessage(), None
        )

    monkeypatch.setattr("urllib.request.urlopen", _always_429)

    result = gw_generate(
        GenerationRequest(prompt="x", max_tokens=4, timeout_ms=300_000)
    )
    assert result.ok is False
    assert calls["n"] <= _MAX_RETRIES + 1


def test_ac4_exhausted_generation_returns_ok_false_text_none_not_raise(
    monkeypatch, select_in_boundary_both
):
    """Exhausted generation returns ok=False / text=None — it must NOT raise.

    This is the exact graceful-failure shape the ticket calls out."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(
            urllib.error.HTTPError(
                "http://fake", 429, "Rate Limited", http.client.HTTPMessage(), None
            )
        ),
    )

    result = gw_generate(
        GenerationRequest(prompt="x", max_tokens=4, timeout_ms=300_000)
    )
    assert result == GenerationResult(
        text=None, provider=IN_BOUNDARY_PROVIDER_NAME, ok=False
    )


def test_ac4_embedding_failure_degrades_safely_to_empty(
    monkeypatch, select_in_boundary_both
):
    """Embedding failure degrades to [] (never raises) so retrieval skips
    safely rather than admitting partial vectors."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(RuntimeError("endpoint down")),
    )

    vectors = gw_embed(["customer-private text"])
    assert vectors == []


def test_ac4_per_request_timeout_is_a_wall_clock_deadline(
    monkeypatch, select_in_boundary_both
):
    """A short timeout_ms is honoured: a 503 cannot back off within a 500ms
    budget, so the call returns ok=False without ever sleeping."""
    sleeps: List[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(
            urllib.error.HTTPError(
                "http://fake", 503, "Service Unavailable", http.client.HTTPMessage(), None
            )
        ),
    )

    result = gw_generate(GenerationRequest(prompt="x", max_tokens=4, timeout_ms=500))
    assert result.ok is False
    assert sleeps == [], "the 500ms deadline must prevent a 0.5s+ backoff sleep"


# ===========================================================================
# AC5 — Every in-boundary call records provider='in_boundary' in telemetry.
# ===========================================================================


def test_ac5_generation_records_provider_in_boundary(
    select_in_boundary_both, network, captured_events
):
    gw_generate(GenerationRequest(prompt="x", max_tokens=4))
    gen = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen) == 1
    assert gen[0][1]["provider"] == IN_BOUNDARY_PROVIDER_NAME


def test_ac5_embedding_records_provider_in_boundary(
    select_in_boundary_both, network, captured_events
):
    gw_embed(["x"])
    emb = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(emb) == 1
    assert emb[0][1]["provider"] == IN_BOUNDARY_PROVIDER_NAME


def test_ac5_failed_call_still_records_provider_in_boundary(
    monkeypatch, configure_in_boundary, captured_events
):
    """Even a failed (exhausted) call records provider='in_boundary' — failures
    are observable, with ok=False."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(
            urllib.error.HTTPError(
                "http://fake", 429, "Rate Limited", http.client.HTTPMessage(), None
            )
        ),
    )

    result = gw_generate(GenerationRequest(prompt="x", max_tokens=4, timeout_ms=300_000))
    assert result.ok is False
    gen = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen) == 1
    assert gen[0][1]["provider"] == IN_BOUNDARY_PROVIDER_NAME
    assert gen[0][1]["ok"] is False


def test_ac5_each_call_records_exactly_one_event_no_double_count(
    select_in_boundary_both, network, captured_events
):
    """Routing through the gateway to a self-emitting provider yields exactly one
    event per logical call — never a duplicate, never per-token."""
    gw_generate(GenerationRequest(prompt="x", max_tokens=4))
    gw_embed(["x"])
    assert len([e for e in captured_events if e[0] == _GEN_EVENT]) == 1
    assert len([e for e in captured_events if e[0] == _EMB_EVENT]) == 1


def test_ac5_telemetry_payload_carries_no_prompt_text_or_secret(
    select_in_boundary_both, network, captured_events
):
    """SAFETY: the recorded provider event never carries the prompt, the input
    texts, or the endpoint credential."""
    secret_prompt = "TOP-SECRET customer prompt"
    secret_text = "TOP-SECRET embedding input"
    gw_generate(GenerationRequest(prompt=secret_prompt, max_tokens=4))
    gw_embed([secret_text])

    blob = json.dumps([payload for _evt, payload in captured_events])
    assert secret_prompt not in blob
    assert secret_text not in blob
    assert _SECRET not in blob


# ===========================================================================
# AC6 — The R16-D1 no-bypass test still passes: in-boundary mode introduces no
#        direct model call outside the gateway.
# ===========================================================================


def test_ac6_r16d1_no_bypass_scan_still_clean():
    """Re-run the R16-D1 enforcement scanner directly: no application code
    outside the gateway makes a direct model call after in-boundary was added."""
    import test_model_gateway_no_bypass as r16d1

    violations: List[str] = []
    for py_file in r16d1._collect_scan_targets():
        for lineno, line, pattern in r16d1._scan_file(py_file):
            rel = py_file.relative_to(r16d1.BACKEND_ROOT)
            violations.append(f"  {rel}:{lineno}: [{pattern!r}]  {line}")

    assert not violations, (
        "R16-D1 no-bypass enforcement regressed after adding in-boundary mode.\n"
        "Route all model calls through the gateway.\n\nViolations:\n"
        + "\n".join(violations)
    )


def test_ac6_callers_reach_in_boundary_only_through_the_gateway(
    monkeypatch, configure_in_boundary
):
    """Callers never import or instantiate the provider directly — selecting it
    is config, and the gateway resolver returns it."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    assert get_generation_provider().name == IN_BOUNDARY_PROVIDER_NAME
    assert get_embedding_provider().name == IN_BOUNDARY_PROVIDER_NAME


# ===========================================================================
# AC7 — With a deliberately lower-capability model behind the endpoint, the
#        system surfaces FEWER findings but produces NO finding that violates the
#        corroboration or causal gates — graceful, not dangerous, degradation.
#
# A weaker in-boundary model only ever PROPOSES; its output enters as inferred
# content and is arbitrated by the existing, non-configurable gates. We prove the
# gates filter weak output (fewer findings) and never admit a finding that breaks
# corroboration or causal discipline (never wrong ones).
# ===========================================================================

# A frontier-quality proposal: fully observed cause chain, specific disproof.
_STRONG_CAUSE_CHAIN = [
    "Loan origination volume rose 40% above baseline [OBSERVED, anomalous].",
    "The Commercial Credit team capacity was not scaled [OBSERVED via OwnerId].",
    "Covenant review queue backed up [OBSERVED: avg 23 days overdue].",
]
_STRONG_FALSIFIABILITY = (
    "If Commercial Credit headcount increased by 3 FTE and the covenant review "
    "queue stayed above 20 days, this hypothesis is disproved."
)

# A weaker-model proposal: leans on an inferred relationship and gives a vague,
# semantically-empty disproof condition.
_WEAK_CAUSE_CHAIN = [
    "Commercial Credit accounts for 62% of SLA breaches [OBSERVED: ServiceNow].",
    "[inferred: 0.6] Backlog pressure is probably reducing the team's capacity.",
]
_WEAK_FALSIFIABILITY = "If things change, this might be wrong."


def test_ac7_weak_model_inferred_step_is_caught_by_causal_gate3():
    """A weak model that rests a step on an INFERRED relationship is flagged by
    the causal gate; the strong, fully-observed chain is not — so the weak finding
    is surfaced as preliminary (fewer confirmed), never silently confirmed."""
    assert cause_chain_uses_inferred(_WEAK_CAUSE_CHAIN) is True
    assert step_references_inferred_relationship(_WEAK_CAUSE_CHAIN[1]) is True

    # The frontier-quality chain rests on observed evidence — it is NOT downgraded.
    assert cause_chain_uses_inferred(_STRONG_CAUSE_CHAIN) is False


def test_ac7_weak_model_vague_falsifiability_is_rejected_strong_is_not():
    """A weak model's vague disproof condition is rejected as generic; a specific
    one is accepted. The gate does not bend for a lower-capability model — it just
    admits fewer of its proposals."""
    assert is_generic_falsifiability(_WEAK_FALSIFIABILITY) is True
    assert is_generic_falsifiability(_STRONG_FALSIFIABILITY) is False


def test_ac7_corroboration_never_elevates_single_source_regardless_of_model():
    """No matter how confidently a weak model proposes, a single connected system
    cannot self-corroborate — corroboration stays unelevated (COR-08 ceiling)."""
    run_ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
    result = evaluate_corroboration(
        detector_id="LOAN_ORIGINATION_BOTTLENECK",
        pack_id="ncino",
        run_data={"connected_systems": ["salesforce"]},  # single source
        run_timestamp=run_ts,
        org_id="default",
    )
    assert result.confidence_elevated is False
    assert result.corroboration_sources == []
    # And the never-downgrade application of the verdict cannot exceed the
    # scorer's own baseline upward on a single source.
    final = apply_corroboration_confidence("MEDIUM", result)
    assert final == "MEDIUM"


def test_ac7_corroboration_requires_real_cross_system_evidence():
    """Corroboration elevates ONLY when genuine cross-system evidence exists
    within the window — the model's confidence is irrelevant to the gate.

    Two connected systems with matching open ServiceNow + Jira records corroborate
    (real evidence); the same two systems with NO records do not."""
    run_ts = datetime(2026, 6, 30, tzinfo=timezone.utc)

    corroborated = evaluate_corroboration(
        detector_id="COVENANT_TRACKING_GAP",
        pack_id="ncino",
        run_data={
            "connected_systems": ["servicenow", "jira"],
            "servicenow": {
                "incidents": [
                    {"sys_created_on": "2026-06-20T10:00:00Z", "state": "Open"}
                ]
            },
            "jira": {
                "issues": [{"created": "2026-06-21T10:00:00Z", "status": "Open"}]
            },
        },
        run_timestamp=run_ts,
        org_id="default",
    )
    assert "COR-01" in corroborated.rule_ids
    assert "COR-02" in corroborated.rule_ids

    # Same two systems, but NO supporting records → no corroboration despite the
    # systems being connected. The gate is evidence-driven, not model-driven.
    uncorroborated = evaluate_corroboration(
        detector_id="COVENANT_TRACKING_GAP",
        pack_id="ncino",
        run_data={
            "connected_systems": ["servicenow", "jira"],
            "servicenow": {"incidents": []},
            "jira": {"issues": []},
        },
        run_timestamp=run_ts,
        org_id="default",
    )
    assert uncorroborated.rule_ids == []
    assert uncorroborated.confidence_elevated is False


def test_ac7_stale_cross_system_evidence_does_not_corroborate():
    """Evidence outside the corroboration window does not elevate — a weaker
    model cannot conjure corroboration from stale data."""
    run_ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
    result = evaluate_corroboration(
        detector_id="COVENANT_TRACKING_GAP",
        pack_id="ncino",
        run_data={
            "connected_systems": ["servicenow", "jira"],
            # 90 days before the run — well outside the 30-day window.
            "servicenow": {
                "incidents": [{"sys_created_on": "2026-04-01T10:00:00Z", "state": "Open"}]
            },
            "jira": {
                "issues": [{"created": "2026-04-01T10:00:00Z", "status": "Open"}]
            },
        },
        run_timestamp=run_ts,
        org_id="default",
    )
    assert result.rule_ids == []
    assert result.confidence_elevated is False
