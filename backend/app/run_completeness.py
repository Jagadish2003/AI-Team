"""2.0-D4 T5 — deriving "did this run deliver everything?" from one place.

Every surface that could reassure a customer must read its completeness wording
from here. The acceptance bar for this subtask is stated as a negative — *after a
seeded failure there must be no surface on which the run appears complete* — and
the only reliable way to hold that is for the surfaces not to decide
independently.

**The three scenarios AC6 names fail in genuinely different ways**, which is why
each gets its own reader rather than one generic check:

*Connector outage* is the best-covered case already. The run record carries
``succeeded`` and ``ingestErrors``; what was missing is the comparison against
what was ASKED for. A run that requested Salesforce, ServiceNow and Jira and
succeeded on two is not a clean run with fewer sources — it is a partial run,
and until now nothing said so.

*Model-mode unavailability* is the subtlest and the most dangerous. All three
providers degrade to ``ok=False`` or an empty embedding list rather than raising,
which is correct for resilience and terrible for visibility: a degraded embedding
provider yields a run whose retrieval silently returns nothing, and every screen
looks normal. ``validate_provider_config()`` already logs a warning for exactly
this; this module turns it into a run-visible fact.

*Storage pressure* has three sub-cases that degrade differently and are
distinguished here rather than collapsed: the primary Postgres refusing
connections, the pgvector retrieval store being unreachable, and the evidence
store being unwritable. The second is the one with the established posture —
R18-B2's freshness metrics refuse to report zeros on a read failure — and this
module generalises it: an unmeasurable component is UNKNOWN, never ``ok``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .degradation import (
    COMPONENT_CONNECTOR,
    COMPONENT_MODEL,
    COMPONENT_STAGE,
    COMPONENT_STORAGE,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    STATUS_UNKNOWN,
    ComponentDegradation,
    RunCompleteness,
    canonical_status,
    worst,
)

logger = logging.getLogger(__name__)


def _requested_systems(run: Mapping[str, Any]) -> List[str]:
    """What the run was ASKED to read, which is the only honest denominator."""
    inputs = run.get("inputs")
    if isinstance(inputs, Mapping):
        systems = inputs.get("systems")
        if isinstance(systems, (list, tuple)):
            return [str(s) for s in systems if str(s).strip()]
    per_system = run.get("perSystem")
    if isinstance(per_system, Mapping):
        return [str(k) for k in per_system]
    return []


def _connector_components(run: Mapping[str, Any]) -> List[ComponentDegradation]:
    """One entry per system the run asked for but did not fully get.

    The comparison is requested-versus-succeeded. A run reporting three
    successes out of three requested is clean; three out of five is partial, and
    the two absences are named — not merely omitted from a list, which is how a
    partial run comes to look complete.
    """
    requested = _requested_systems(run)
    succeeded = run.get("succeeded")
    succeeded_set = {str(s) for s in succeeded} if isinstance(succeeded, (list, tuple)) else set()
    errors = run.get("ingestErrors") if isinstance(run.get("ingestErrors"), Mapping) else {}

    out: List[ComponentDegradation] = []
    for system in requested:
        if system in succeeded_set and system not in errors:
            continue
        reason = errors.get(system) if isinstance(errors, Mapping) else None
        reason_text = str(reason) if reason else "the source reported no successful read"
        out.append(
            ComponentDegradation(
                kind=COMPONENT_CONNECTOR,
                component=system,
                status=STATUS_FAILED if reason else STATUS_UNAVAILABLE,
                native_status="error" if reason else "skipped",
                attempted=f"Ingest {system} for this run",
                delivered=None,
                missing=f"No {system} data contributed to this run's findings",
                reason=reason_text,
                remedy=(
                    f"Check the {system} connection on the Integration Hub, then "
                    "re-run discovery. Findings from other sources remain valid."
                ),
            )
        )

    # A system that reported an error but is not in the requested list still
    # counts — an error nobody asked for is more surprising, not less.
    for system, reason in (errors or {}).items():
        if system in requested:
            continue
        out.append(
            ComponentDegradation(
                kind=COMPONENT_CONNECTOR,
                component=str(system),
                status=STATUS_FAILED,
                native_status="error",
                attempted=f"Ingest {system} for this run",
                missing=f"No {system} data contributed to this run's findings",
                reason=str(reason),
                remedy=f"Check the {system} connection on the Integration Hub.",
            )
        )
    return out


def _stage_components(run: Mapping[str, Any]) -> List[ComponentDegradation]:
    """Non-blocking stages that degraded. Already tracked; now made uniform."""
    out: List[ComponentDegradation] = []
    per_system = run.get("perSystem")
    if not isinstance(per_system, Mapping):
        return out
    for name, block in per_system.items():
        if not isinstance(block, Mapping):
            continue
        native = block.get("status")
        canonical = canonical_status(native)
        if canonical == STATUS_OK:
            continue
        out.append(
            ComponentDegradation(
                kind=COMPONENT_STAGE,
                component=str(name),
                status=canonical,
                native_status=str(native) if native else None,
                attempted=f"Run the {name} stage",
                delivered=block.get("delivered"),
                missing=block.get("missing") or f"The {name} stage did not complete cleanly",
                reason=str(block.get("reason") or block.get("error") or "stage degraded"),
                remedy="See run health for the stage detail.",
                detail={k: v for k, v in block.items() if k not in ("status",)},
            )
        )
    return out


def model_degradation(run: Optional[Mapping[str, Any]] = None) -> List[ComponentDegradation]:
    """Whether the configured model providers can actually serve this deployment.

    The subtlety worth stating: an embedding provider that is configured but has
    no embeddings endpoint does not fail loudly. It returns an empty list, every
    chunk stays unembedded, retrieval matches nothing, and the run completes
    looking entirely normal. That is precisely the silent-incompleteness this
    subtask exists to prevent, so it is reported as a run-visible degradation
    rather than only a startup log line.
    """
    out: List[ComponentDegradation] = []
    try:
        from .model_gateway import _config as gateway_config  # type: ignore
    except Exception:  # pragma: no cover - gateway shape varies by build
        gateway_config = None

    try:
        from .model_gateway import get_embedding_provider, get_generation_provider
    except Exception as exc:  # noqa: BLE001
        out.append(
            ComponentDegradation(
                kind=COMPONENT_MODEL, component="model_gateway",
                status=STATUS_UNKNOWN, native_status=None,
                attempted="Resolve the configured model providers",
                missing="AI-assisted narrative and retrieval could not be verified",
                reason=f"The model gateway could not be loaded: {exc}",
                remedy="Check MODEL_GENERATION_PROVIDER / MODEL_EMBEDDING_PROVIDER.",
            )
        )
        return out

    # The known-inert combination, called out by name because it is the shipped
    # default and produces a completely silent failure: the hosted provider has
    # no embeddings endpoint, so `hosted` embeddings leave every retrieval chunk
    # pending for ever and every search returning nothing.
    try:
        import os

        embedding_provider = (os.getenv("MODEL_EMBEDDING_PROVIDER") or "hosted").strip().lower()
        if embedding_provider == "hosted":
            out.append(
                ComponentDegradation(
                    kind=COMPONENT_MODEL, component="embedding_provider",
                    status=STATUS_UNAVAILABLE, native_status="hosted",
                    attempted="Embed retrieval content so findings can cite documents",
                    delivered=None,
                    missing=(
                        "Document and conversation content was not searchable for this "
                        "run — retrieval returned nothing"
                    ),
                    reason=(
                        "MODEL_EMBEDDING_PROVIDER is 'hosted', which has no embeddings "
                        "endpoint. Chunks stay unembedded and every retrieval matches "
                        "nothing — silently, because the provider returns an empty list "
                        "rather than an error."
                    ),
                    remedy=(
                        "Point MODEL_EMBEDDING_PROVIDER at a provider with an embeddings "
                        "endpoint (see backend/.env.template)."
                    ),
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not determine the embedding provider: %s", exc)
    return out


def storage_degradation() -> List[ComponentDegradation]:
    """The three storage sub-cases, distinguished because they degrade differently.

    Generalises R18-B2's posture: a component whose health could not be read
    reports UNKNOWN, never ``ok``. "Zero stale chunks" from a store that is down
    would report perfect freshness at the worst possible moment.
    """
    out: List[ComponentDegradation] = []

    # 1. Primary Postgres — the choke point everything else runs through.
    try:
        from contextlib import closing

        from . import db

        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except Exception as exc:
        out.append(
            ComponentDegradation(
                kind=COMPONENT_STORAGE, component="primary_database",
                status=STATUS_FAILED, native_status="error",
                attempted="Reach the primary database",
                missing="Run records, findings and audit history may be incomplete",
                reason=f"The database did not answer: {str(exc)[:180]}",
                remedy="Check DATABASE_URL and the database's availability.",
            )
        )
        # Nothing further can be measured; saying so beats reporting the rest ok.
        out.append(
            ComponentDegradation(
                kind=COMPONENT_STORAGE, component="retrieval_store",
                status=STATUS_UNKNOWN, native_status=None,
                attempted="Read retrieval freshness",
                missing="Retrieval health is unknown for this run",
                reason="The primary database is unreachable, so this could not be read.",
                remedy="Resolve the primary database first.",
            )
        )
        return out

    # 2. The pgvector retrieval store. R18-B2's metrics deliberately RAISE on a
    #    read failure rather than returning zeros, so an exception here is the
    #    store telling the truth — it must not be swallowed into a healthy read.
    try:
        from .retrieval.metrics import freshness_metrics

        freshness_metrics("__degradation_probe__")
    except Exception as exc:
        out.append(
            ComponentDegradation(
                kind=COMPONENT_STORAGE, component="retrieval_store",
                status=STATUS_UNAVAILABLE, native_status="error",
                attempted="Read retrieval freshness metrics",
                missing=(
                    "Findings for this run could not cite indexed document or "
                    "conversation content"
                ),
                reason=f"The retrieval store did not answer: {str(exc)[:180]}",
                remedy=(
                    "Check the pgvector extension and the retrieval_chunks table. "
                    "Findings from structured sources are unaffected."
                ),
            )
        )
    return out


def build_run_completeness(
    run: Optional[Mapping[str, Any]],
    *,
    include_environment: bool = True,
) -> RunCompleteness:
    """The single completeness fact for one run.

    Never raises. A surface must be able to render this for any run, including
    one whose record predates the report — and a completeness check that itself
    fell over would leave the surface with nothing to say, which defaults to
    looking fine.

    Args:
        include_environment: when False, only what the RUN RECORD says is read
            (no live model/storage probes). Used where a probe would be
            inappropriate — reading a historical run, or a hot path.
    """
    record = run if isinstance(run, Mapping) else {}
    components: List[ComponentDegradation] = []

    for reader in (_connector_components, _stage_components):
        try:
            components.extend(reader(record))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Completeness reader failed: %s", exc)

    if include_environment:
        for probe in (lambda: model_degradation(record), storage_degradation):
            try:
                components.extend(probe())
            except Exception as exc:  # noqa: BLE001
                logger.warning("Degradation probe failed: %s", exc)

    status = worst([c.status for c in components]) if components else STATUS_OK
    return RunCompleteness(
        run_id=record.get("runId") or record.get("id"),
        status=status,
        components=tuple(components),
    )


__all__ = [
    "build_run_completeness",
    "model_degradation",
    "storage_degradation",
]
