"""AI-mode parity for context assembly — 2.0-B3 T6 (AC6).

Assembly must behave IDENTICALLY across the three AI modes — `hosted`,
`in_boundary`, `customer_tenant` — except for model quality, and where a mode
cannot support a step the finding must degrade with a VISIBLE label, never a
silent gap (the 1.9 pattern; see `discovery/packs/security_ops_ai_mode.py`).

Why parity holds by construction. `context_assembly.assemble_context` makes NO
model call — it is pure, deterministic, rule-based selection over the
`(opportunity, graph, policy, evidence_source)` it is handed. So given the SAME
seeded inputs, the `ContextPackage` is byte-identical in every mode; the AI mode
cannot change an assembly decision because assembly never consults a model. The
contract test in `tests/contract/test_r2_0_b3_t6_mode_parity.py` proves this by
running one seeded assembly under all three modes and asserting the packages are
equal, and structurally by asserting `assemble_context` imports no gateway.

Where a mode CAN'T support a step. The only step that is STRUCTURALLY
unavailable by mode is **evidence retrieval**, which needs embeddings: the
default `hosted` provider has no embeddings endpoint (`HostedModelProvider.embed`
always returns `[]`), so in hosted EMBEDDING mode retrieval is inert and a
finding is composed from graph context only. Today that degradation is loud at
startup (`model_gateway.validate_provider_config` WARNING) but SILENT on the
finding — this module makes it visible. Narrative generation is NOT a
mode-structural gap: all three modes can generate, and a runtime generation
failure is already labelled on the enrichment result (`llmGenerated=False`), so
this module models retrieval and leaves narrative to that existing label.

Retrieval capability keys on the EMBEDDING mode, resolved INDEPENDENTLY of the
generation mode (the gateway resolves `MODEL_EMBEDDING_PROVIDER` and
`MODEL_GENERATION_PROVIDER` separately), so a deployment generating in-boundary
while still embedding on hosted is correctly reported as retrieval-degraded.

"Supported by mode" is a STRUCTURAL statement (hosted embeddings can never
serve retrieval; the other two can when configured), distinct from a runtime
config failure — an unconfigured in-boundary embedder returns `[]` too, but that
is a deployment gap the gateway already degrades gracefully, not a mode that
cannot support the step. This module reports the structural case.

PURE, mirroring `security_ops_ai_mode.py`: it only reads the active provider
NAMES from the gateway (no network, no assembly change) and fails SAFE to
`hosted` — the most degraded reading — so a resolution failure never hides a
degradation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MODE_HOSTED = "hosted"
MODE_IN_BOUNDARY = "in_boundary"
MODE_CUSTOMER_TENANT = "customer_tenant"

#: The three AI modes, in a fixed order for deterministic reporting/tests.
ALL_MODES: Tuple[str, ...] = (MODE_HOSTED, MODE_IN_BOUNDARY, MODE_CUSTOMER_TENANT)

STEP_RETRIEVAL = "retrieval"

#: Embedding modes that structurally cannot serve evidence retrieval. Only the
#: hosted provider lacks an embeddings endpoint; in-boundary and customer-tenant
#: can embed when configured.
EMBEDDING_MODES_WITHOUT_RETRIEVAL = frozenset({MODE_HOSTED})

#: The verbatim visible label stamped on a finding whose evidence-retrieval step
#: is unavailable in the active mode. Verbatim like SecOps' HOSTED_NARRATIVE_LABEL
#: — the restriction is STATED on the finding, never an unexplained empty
#: evidence list.
RETRIEVAL_UNAVAILABLE_LABEL = (
    "Evidence retrieval unavailable in this AI mode: the hosted provider has no "
    "embeddings endpoint, so this finding is composed from graph context only. "
    "Configure an in-boundary or customer-tenant embedding provider to enable "
    "retrieved evidence."
)

__all__ = [
    "MODE_HOSTED",
    "MODE_IN_BOUNDARY",
    "MODE_CUSTOMER_TENANT",
    "ALL_MODES",
    "STEP_RETRIEVAL",
    "EMBEDDING_MODES_WITHOUT_RETRIEVAL",
    "RETRIEVAL_UNAVAILABLE_LABEL",
    "StepDegradation",
    "AssemblyModeReport",
    "active_generation_mode",
    "active_embedding_mode",
    "retrieval_supported",
    "assembly_mode_report",
    "mode_degradations",
]


def _provider_name(getter_name: str) -> str:
    """Resolve an active provider name from the gateway, failing safe to hosted.

    Reads only the provider NAME (no network). Any failure — the gateway not
    importable, an unregistered provider — resolves to ``hosted``, the most
    degraded reading, so a misconfiguration can never HIDE a degradation by
    reporting a more-capable mode than is actually active.
    """
    try:
        try:
            from backend.app import model_gateway as gw
        except ModuleNotFoundError:  # pragma: no cover - import shim
            from app import model_gateway as gw
        return str(getattr(gw, getter_name)().name)
    except Exception as exc:  # noqa: BLE001 — never raise from a capability read.
        logger.warning(
            "mode_parity: provider resolution (%s) failed (%s); defaulting to hosted",
            getter_name, exc,
        )
        return MODE_HOSTED


def active_generation_mode() -> str:
    """The active generation mode (`MODEL_GENERATION_PROVIDER`), fail-safe hosted."""
    return _provider_name("get_generation_provider")


def active_embedding_mode() -> str:
    """The active embedding mode (`MODEL_EMBEDDING_PROVIDER`), fail-safe hosted."""
    return _provider_name("get_embedding_provider")


def retrieval_supported(embedding_mode: Optional[str] = None) -> bool:
    """True when the active (or given) embedding mode can serve retrieval.

    False only for a mode whose provider structurally lacks embeddings (hosted).
    """
    mode = embedding_mode or active_embedding_mode()
    return mode not in EMBEDDING_MODES_WITHOUT_RETRIEVAL


@dataclass(frozen=True)
class StepDegradation:
    """One assembly step that the active mode cannot support, with its label."""

    step: str
    mode: str
    label: str
    supported: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "mode": self.mode,
            "supported": self.supported,
            "label": self.label,
        }


@dataclass(frozen=True)
class AssemblyModeReport:
    """What the active AI mode can and cannot support for context assembly.

    ``generation_mode`` / ``embedding_mode`` are the active provider names
    (resolved independently). ``retrieval_supported`` is the one structural
    mode-capability that varies. ``degradations`` lists every unsupported step
    with its visible label — empty when the mode supports every step.
    """

    generation_mode: str
    embedding_mode: str
    retrieval_supported: bool
    degradations: Tuple[StepDegradation, ...] = ()

    @property
    def degraded(self) -> bool:
        return bool(self.degradations)

    def labels(self) -> List[str]:
        """The visible degradation labels, in step order."""
        return [d.label for d in self.degradations]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generation_mode": self.generation_mode,
            "embedding_mode": self.embedding_mode,
            "retrieval_supported": self.retrieval_supported,
            "degraded": self.degraded,
            "degradations": [d.to_dict() for d in self.degradations],
        }


def assembly_mode_report(
    generation_mode: Optional[str] = None,
    embedding_mode: Optional[str] = None,
) -> AssemblyModeReport:
    """Build the mode-capability report for the active (or given) modes.

    Deterministic: identical modes always produce an identical report — the
    property the parity test leans on. Pass explicit modes to describe a mode
    other than the process's active one (the test drives all three this way).
    """
    gen = generation_mode or active_generation_mode()
    emb = embedding_mode or active_embedding_mode()

    can_retrieve = retrieval_supported(emb)
    degradations: List[StepDegradation] = []
    if not can_retrieve:
        degradations.append(
            StepDegradation(
                step=STEP_RETRIEVAL,
                mode=emb,
                label=RETRIEVAL_UNAVAILABLE_LABEL,
                supported=False,
            )
        )

    return AssemblyModeReport(
        generation_mode=gen,
        embedding_mode=emb,
        retrieval_supported=can_retrieve,
        degradations=tuple(degradations),
    )


def mode_degradations(
    generation_mode: Optional[str] = None,
    embedding_mode: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """The active mode's unsupported-step labels as plain dicts (empty when none).

    The serialisable form a finding surface carries so an operator sees WHY a
    step degraded rather than an unexplained empty result.
    """
    return [d.to_dict() for d in assembly_mode_report(generation_mode, embedding_mode).degradations]
