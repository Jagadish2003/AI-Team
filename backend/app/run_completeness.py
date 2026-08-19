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

HP-2.6 — provider-caused degradation is a RUN fact, not only a live one
----------------------------------------------------------------------
The model reader above answered "can this deployment embed content *right now*?",
which is the right question for ``GET /api/run-health/degradation`` and the wrong
one for a run that finished last Tuesday. It was therefore reached only under
``include_environment=True``, so the two surfaces a customer actually reads for a
specific run — ``GET /api/runs/{runId}/status`` and the executive report — said
nothing at all when a run lost its AI narrative or its retrieval evidence because
a provider could not be reached.

HP-2.6 closes that by making the posture a **stamped run field**
(:data:`PROVIDER_POSTURE_RUN_FIELD`), written by the materializers from HP-2.3's
cached startup posture and read back by :func:`_model_components` as an ordinary
record reader — so it travels with the run, needs no probe on a read, and a
historical run cannot be re-described by today's environment. Three decisions are
load-bearing:

*The stamp writer and the stamp reader live in ONE module.* A shape written in
``materialize_t2`` and parsed here could disagree; the ``ALL_ENTITIES_DDL``
precedent (one DDL shared by the migration and the runtime creator) is the
pattern followed instead.

*Only a REACHABILITY failure is reported as a degradation*, decided by HP-2.5's
:func:`~app.model_provider_health.role_degrades_health` rather than a second copy
of the rule. A missing credential or an unconfigured endpoint already refuses boot
under ``customer_hosted`` and is a SUPPORTED configuration under ``saas`` — LLM
enrichment is optional by design and the shipped dev/test setup has no key — so
reporting it would flip every keyless deployment's every run to "treat these
findings as partial", and a completeness verdict that cries wolf is one nobody
reads. ``unknown`` does not degrade either: "we did not look" is not "it is
broken".

*No endpoint host is carried.* HP-2.5 withholds it because ``/api/health`` is
public; the reason here is narrower but real — a run record is served to viewers,
exported, and diffed, and an in-boundary deployment's model host is internal
network topology. The consequence is stated in the remedy rather than hidden: the
host and the transport error ("connection refused", "host name could not be
resolved") stay in the startup log, where HP-2.3 already writes them at WARNING.
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
        reason = errors.get(system) if isinstance(errors, Mapping) else None
        delivered_something = system in succeeded_set

        if delivered_something and not reason:
            continue

        if delivered_something and reason:
            # PARTIAL, not failed. A source can succeed and error in the same run
            # — Salesforce delivers 400 records and then times out — and it lands
            # in BOTH sets. Reporting that as `failed` with "No salesforce data
            # contributed" is factually wrong: it did contribute, and saying
            # otherwise makes every finding drawn from it look unsupported. The
            # customer's real question is "how much did I get?", so the answer
            # says partial and does not pretend to a record count the run record
            # does not carry.
            out.append(
                ComponentDegradation(
                    kind=COMPONENT_CONNECTOR,
                    component=system,
                    status=STATUS_PARTIAL,
                    native_status="partial",
                    attempted=f"Ingest {system} for this run",
                    delivered=(
                        f"{system} contributed data before failing — findings "
                        "citing it are supported, but incomplete"
                    ),
                    missing=(
                        f"An unknown remainder of {system} data was not read for "
                        "this run"
                    ),
                    reason=str(reason),
                    remedy=(
                        f"Re-run discovery to pick up the {system} records this "
                        "run did not reach. Findings from other sources are "
                        "unaffected."
                    ),
                )
            )
            continue

        if reason:
            # Requested, attempted, and failed with a named reason.
            out.append(
                ComponentDegradation(
                    kind=COMPONENT_CONNECTOR,
                    component=system,
                    status=STATUS_FAILED,
                    native_status="error",
                    attempted=f"Ingest {system} for this run",
                    delivered=None,
                    missing=f"No {system} data contributed to this run's findings",
                    reason=str(reason),
                    remedy=(
                        f"Check the {system} connection on the Integration Hub, "
                        "then re-run discovery. Findings from other sources "
                        "remain valid."
                    ),
                )
            )
            continue

        # Requested, but the run recorded NEITHER a success nor an error for it.
        # This is deliberately UNKNOWN rather than UNAVAILABLE. `unavailable`
        # means "produced nothing, for a reason that is not an error, and the
        # customer can act on it" — a missing credential, a plugin that is not
        # activated. A silent empty from a source that was asked for does not
        # meet that bar: it is equally likely to be an ingestor defect that
        # swallowed its own failure, and labelling it `unavailable` sends an
        # operator hunting for a credential problem that may not exist.
        out.append(
            ComponentDegradation(
                kind=COMPONENT_CONNECTOR,
                component=system,
                status=STATUS_UNKNOWN,
                native_status=None,
                attempted=f"Ingest {system} for this run",
                delivered=None,
                missing=f"No {system} data contributed to this run's findings",
                reason=(
                    f"{system} was requested but reported neither a successful "
                    "read nor an error, so why it produced nothing could not be "
                    "established."
                ),
                remedy=(
                    f"Check the {system} connection on the Integration Hub AND "
                    "this run's ingest logs — a source that fails silently is "
                    "usually a credential or configuration problem, but may be a "
                    "connector defect."
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


# --------------------------------------------------------------------------
# HP-2.6 — the model-provider posture, recorded on the run and rendered
# identically wherever it is read.
# --------------------------------------------------------------------------

#: The additive run-record field carrying the provider posture a run ran under.
#: Written by the materializers via :func:`provider_posture_record`, read back by
#: :func:`_model_components`. A run recorded before HP-2.6 simply has no field and
#: is therefore never described — the honest outcome, and the same posture 2.0-A2
#: takes to un-baselined findings.
PROVIDER_POSTURE_RUN_FIELD = "modelProviders"

#: Bumped when the stamp's SHAPE changes in a way a reader must notice.
PROVIDER_POSTURE_SCHEMA_VERSION = "1.0.0"

#: Where the posture being rendered came from. Reported on ``detail`` so an
#: engineer can tell "this run recorded it" from "this is the environment now".
POSTURE_SOURCE_RUN_RECORD = "run_record"
POSTURE_SOURCE_STARTUP = "startup_posture"

#: Model-gateway role -> the component id its degradation is reported under.
#: DISTINCT ids are what make "embedding is reported distinctly from generation"
#: structural rather than a wording convention: a consumer can filter on the id.
#: Keyed on the literal role names rather than importing them, so this module
#: keeps its no-gateway-at-import-time posture; a contract test pins the keys
#: against ``ROLE_GENERATION`` / ``ROLE_EMBEDDING`` so they cannot drift.
ROLE_COMPONENT_IDS: Dict[str, str] = {
    "generation": "generation_provider",
    "embedding": "embedding_provider",
}

#: What each role's absence actually costs a run, in the customer's terms. A
#: generation outage costs narrative and leaves the deterministic findings intact;
#: an embedding outage costs citations. Collapsing them into one "AI unavailable"
#: sentence would tell a reader nothing about which half of the product they lost.
_ROLE_COPY: Dict[str, Dict[str, str]] = {
    "generation": {
        "attempted": "Generate AI-assisted narrative for this run's findings",
        "missing": (
            "AI-assisted narrative (summaries, rationale and executive-report "
            "prose) was not generated for this run — findings carry the "
            "platform's deterministic wording instead"
        ),
        "consequence": "generation calls had no reachable provider",
    },
    "embedding": {
        "attempted": "Embed retrieval content so this run's findings can cite documents",
        "missing": (
            "Document and conversation content was not embedded for this run — "
            "retrieval matched nothing, so findings could not cite indexed content"
        ),
        "consequence": "retrieval content could not be embedded",
    },
}


def provider_posture_record() -> Optional[Dict[str, Any]]:
    """The model-provider posture as a run-record stamp, or ``None``.

    Reads HP-2.3's CACHED startup posture through the gateway's public
    ``provider_posture()``. It never re-probes — a materializer that opened
    sockets per run would turn a customer's own model server into something this
    platform hammers, which is the same reason HP-2.5 refuses to probe on a health
    read.

    Returns ``None`` when the posture was never evaluated (a process that did not
    run the lifespan, or a gateway that could not be imported). ``None`` means "we
    have nothing to say", which is different from — and must never be written as —
    a healthy posture.

    Carries no endpoint host and no secret: only the role, the provider NAME, the
    variable that selected it, the canonical status, which check produced it, and
    whether the reachability probe actually ran.
    """
    try:
        from .model_gateway import provider_posture
    except Exception:  # noqa: BLE001 — a stamp must never break a run
        logger.debug("run posture: model gateway not importable", exc_info=True)
        return None

    try:
        posture = provider_posture()
    except Exception:  # noqa: BLE001
        logger.debug("run posture: posture unreadable", exc_info=True)
        return None

    if posture is None:
        return None

    roles: Dict[str, Any] = {}
    try:
        for role in posture.roles:
            roles[str(role.role)] = {
                "provider": role.provider,
                "variable": role.env_var,
                "status": role.status,
                "check": role.check,
                "probed": bool(role.probed),
            }
        status = posture.status
    except Exception:  # noqa: BLE001
        logger.debug("run posture: posture shape unexpected", exc_info=True)
        return None

    return {
        "schemaVersion": PROVIDER_POSTURE_SCHEMA_VERSION,
        "status": status,
        "roles": roles,
    }


def stamp_provider_posture(run: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Record the provider posture on a run record. Never raises.

    The single call the materializers make, so neither of them has to know the
    field name or the shape. A run whose posture cannot be resolved is left
    untouched rather than stamped with an empty record: an absent field reads as
    "not recorded", while an empty one would read as "recorded, and fine".
    """
    try:
        record = provider_posture_record()
    except Exception:  # noqa: BLE001
        logger.debug("run posture: record could not be built", exc_info=True)
        return None
    if not record:
        return None
    try:
        run[PROVIDER_POSTURE_RUN_FIELD] = record
    except Exception:  # noqa: BLE001 — a mapping that refuses assignment
        logger.debug("run posture: run record not writable", exc_info=True)
        return None
    return record


def _role_degrades_run(status: str, check: str) -> bool:
    """Whether one role's posture is a degradation worth reporting on a run.

    Delegates to HP-2.5's rule so "which provider conditions count as a real
    degradation" has exactly ONE answer across ``/api/health``, the run status and
    the run-health surface. A second copy here would be free to drift, which is
    the defect HP-1 was created to remove in a different guise.

    The one fail-quiet path is the module not importing — a packaging fault CI
    catches — logged with its consequence named, because inlining a duplicate of
    the rule to cover it would reintroduce exactly the drift this avoids.
    """
    try:
        from .model_provider_health import role_degrades_health
    except Exception:  # noqa: BLE001
        logger.warning(
            "run posture: model_provider_health is not importable, so provider "
            "reachability failures will NOT be reported on this run's "
            "completeness. Every other degradation is unaffected.",
            exc_info=True,
        )
        return False
    try:
        return bool(role_degrades_health(status, check))
    except Exception:  # noqa: BLE001
        logger.debug("run posture: degradation rule raised", exc_info=True)
        return False


def _provider_component(
    role: str,
    entry: Mapping[str, Any],
    *,
    source: str,
) -> Optional[ComponentDegradation]:
    """One role's posture as a degradation, or ``None`` when it is not one.

    This is the ONLY place provider-degradation wording is composed. Every
    surface renders the same sentences because none of them writes any.
    """
    native = entry.get("status")
    status = canonical_status(str(native) if native is not None else None)
    check = str(entry.get("check") or "")
    if not _role_degrades_run(status, check):
        return None

    provider = str(entry.get("provider") or "unknown")
    variable = str(entry.get("variable") or "the provider selection variable")
    copy = _ROLE_COPY.get(
        role,
        {
            # A role this module has not been taught still reports — silence for an
            # unrecognised role would be the exact silent degradation HP-2 removes.
            "attempted": f"Serve the {role} model role for this run",
            "missing": f"The {role} model role produced nothing for this run",
            "consequence": f"{role} calls had no reachable provider",
        },
    )

    return ComponentDegradation(
        kind=COMPONENT_MODEL,
        component=ROLE_COMPONENT_IDS.get(role, f"{role}_provider"),
        status=status,
        native_status=str(native) if native is not None else None,
        attempted=f"{copy['attempted']} using the '{provider}' provider",
        delivered=None,
        missing=copy["missing"],
        reason=(
            f"The {role} provider '{provider}' ({variable}) was unreachable at "
            "this deployment's startup posture check, so "
            f"{copy['consequence']}. The check is not repeated per run, so this "
            "run ran against a provider last seen as unreachable."
        ),
        remedy=(
            f"Restore network reach to the configured {role} endpoint, or point "
            f"{variable} at a reachable provider, then restart the service so the "
            "posture is re-checked. The endpoint host and the transport error are "
            "in the startup log at WARNING — deliberately not carried here, "
            "because a run record is not the place for internal network "
            "topology. Findings from structured sources are unaffected."
        ),
        detail={
            "role": role,
            "provider": provider,
            "variable": variable,
            "check": check,
            "probed": bool(entry.get("probed")),
            "postureSource": source,
        },
    )


def _provider_components(
    record: Mapping[str, Any],
    *,
    source: str,
) -> List[ComponentDegradation]:
    """Every degrading role in one posture record, in a deterministic order."""
    roles = record.get("roles")
    if not isinstance(roles, Mapping):
        return []
    out: List[ComponentDegradation] = []
    for role_name in sorted(str(r) for r in roles):
        entry = roles.get(role_name)
        if not isinstance(entry, Mapping):
            continue
        component = _provider_component(role_name, entry, source=source)
        if component is not None:
            out.append(component)
    return out


def _model_components(run: Mapping[str, Any]) -> List[ComponentDegradation]:
    """HP-2.6 — provider degradation as the RUN recorded it.

    A record reader, so it runs on every surface including the ones that must not
    probe (``GET /api/runs/{runId}/status``, the executive report). What it reports
    is what that run stamped, never what the environment looks like now.
    """
    record = run.get(PROVIDER_POSTURE_RUN_FIELD)
    if not isinstance(record, Mapping):
        return []
    return _provider_components(record, source=POSTURE_SOURCE_RUN_RECORD)


def model_degradation(run: Optional[Mapping[str, Any]] = None) -> List[ComponentDegradation]:
    """Whether the configured model providers can actually serve this deployment.

    The subtlety worth stating: an embedding provider that is configured but has
    no embeddings endpoint does not fail loudly. It returns an empty list, every
    chunk stays unembedded, retrieval matches nothing, and the run completes
    looking entirely normal. That is precisely the silent-incompleteness this
    subtask exists to prevent, so it is reported as a run-visible degradation
    rather than only a startup log line.

    HP-2.6 adds the LIVE posture here, and only for a record that carries no
    stamp of its own — the run's own record is authoritative about the run, and
    letting the environment answer over it is how a historical run gets
    re-described by today's outage. With no run at all (``/api/run-health/``
    ``degradation`` with no ``run_id``, which is a question about now) the live
    posture is the only answer there is.
    """
    out: List[ComponentDegradation] = []
    record = run if isinstance(run, Mapping) else {}
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

    # HP-2.6: the live startup posture, for a record that stamped none of its own.
    # Appended AFTER the inertness check so the two facts stay in a stable order,
    # and composed through the SAME helper the record reader uses — a live answer
    # and a recorded one must read identically or the surfaces disagree again.
    if not isinstance(record.get(PROVIDER_POSTURE_RUN_FIELD), Mapping):
        try:
            live = provider_posture_record()
            if live:
                out.extend(_provider_components(live, source=POSTURE_SOURCE_STARTUP))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read the live provider posture: %s", exc)
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
        # STATUS_FAILED, not STATUS_UNAVAILABLE. An exception out of a database
        # query is a component that was reached and did not work, which is this
        # module's definition of `failed`. `unavailable` is explicitly "not an
        # error, and actionable by the customer" — a missing plugin, an absent
        # credential — and a pgvector misconfiguration (say, retrieval_chunks
        # absent after a half-applied migration raising UndefinedTable) is
        # neither. Getting this wrong understated the problem twice over: it
        # ranks below `failed` in worst(), so a run-wide roll-up read one notch
        # healthier than reality, and it pointed the remedy at the customer's
        # configuration rather than at the operator who needs to fix a migration.
        out.append(
            ComponentDegradation(
                kind=COMPONENT_STORAGE, component="retrieval_store",
                status=STATUS_FAILED, native_status="error",
                attempted="Read retrieval freshness metrics",
                missing=(
                    "Findings for this run could not cite indexed document or "
                    "conversation content"
                ),
                reason=f"The retrieval store did not answer: {str(exc)[:180]}",
                remedy=(
                    "Check that the pgvector extension is installed and that the "
                    "retrieval_chunks table exists and is readable — an error "
                    "here usually means a migration did not complete, not a "
                    "customer configuration problem. Findings from structured "
                    "sources are unaffected."
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

    # HP-2.6: `_model_components` is a RECORD reader, not an environment probe, so
    # it sits here rather than below the `include_environment` gate. That is the
    # whole point — provider degradation has to reach the run-scoped surfaces
    # (`GET /api/runs/{runId}/status`, the executive report), which deliberately
    # never probe, and it reaches them from what the run itself recorded.
    for reader in (_connector_components, _stage_components, _model_components):
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
    "POSTURE_SOURCE_RUN_RECORD",
    "POSTURE_SOURCE_STARTUP",
    "PROVIDER_POSTURE_RUN_FIELD",
    "PROVIDER_POSTURE_SCHEMA_VERSION",
    "ROLE_COMPONENT_IDS",
    "build_run_completeness",
    "model_degradation",
    "provider_posture_record",
    "stamp_provider_posture",
    "storage_degradation",
]
