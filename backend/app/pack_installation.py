"""Authored-pack installation — 2.0-C3 T4 (AT-839), T6 (AT-841).

The rule this module owns:

    A signed pack bundle; install/activate flows through C1/C2 — compatibility
    checked, level enforced by org policy.

The install pipeline, and why it is in this order
--------------------------------------------------
Five gates, each returning a NAMED refusal rather than a generic failure, because
an operator handed "installation failed" cannot act on it:

    1. **signature** (AT-839 / :mod:`discovery.packs.sdk.bundle`) — an unsigned,
       tampered, or untrusted-publisher bundle is refused BEFORE its bytes are
       written anywhere. Extraction is the step that puts partner-supplied content
       on disk, so nothing precedes verification.
    2. **validation** — the manifest schema, the author's fixtures, and the lint
       pass, run through the SAME ``check_pack_directory`` the author ran locally,
       BOUNDED by the sandbox (T6 / :mod:`app.pack_sandbox`). One code path means
       "it passed on my machine" and "it passed on install" cannot diverge; the
       bounds mean a fixture suite nobody could afford to run is a named refusal
       rather than a request that never returns.
    3. **compatibility (2.0-C1)** — the manifest's declared platform range and
       required concepts, judged by the shared
       ``check_declaration_compatibility`` rule the runner enforces, not a copy.
    4. **certification policy (2.0-C2)** — an authored pack is COMMUNITY until
       CloudFulcrum signs a certification for it, so an org with a Certified-only
       floor refuses it, naming the floor. Fail-CLOSED: a policy that cannot be
       read refuses, matching the posture of the policy module itself.
    5. **persist** — and only then.

Installing does not activate
-----------------------------
Installation records a pack; activation is a separate, explicit decision, and
EVERYTHING that can change underneath an installed pack is re-checked when it is
activated rather than trusted from install time:

  * the **sandbox validation** (T6) — the stored manifest and the author's stored
    fixtures, re-judged against the platform as it is today. This is why the
    fixtures are persisted at all: a pack that cannot be re-validated is one the
    platform would have to take on trust at the moment it starts executing;
  * the **platform version** and the org's **certification floor**.

A pack that was installable last month is not automatically activatable today,
and pretending otherwise is how a Certified-only deployment ends up running a
Community pack — or how a pack whose concepts the platform has since withdrawn
starts producing nothing while reporting success.

Withdrawal runs no gates. Taking a pack OUT of service must never be blocked by
the pack's own condition.

Nothing is ever deleted
-----------------------
There is no delete path. Removing a pack from service writes ``inactive``; the
record, its manifest, and its provenance stay, which is what keeps "which pack
produced this historical finding, and where did it come from" answerable
(2.0-C1 AC4's discipline applied to the installed registry).

Read posture
------------
Reads are fail-soft for DISPLAY (:func:`installed_packs_safe`) and never fail-soft
for the gates: an install or activation that did not persist must never look like
it succeeded, and a policy that cannot be verified must never read as compliant.
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app import db
from discovery.packs.pack_certification import LEVEL_COMMUNITY, LEVEL_LABELS
from discovery.packs.pack_compatibility import (
    PackCompatibility,
    check_declaration_compatibility,
)
from discovery.packs.pack_config import PACK_REGISTRY
from discovery.packs.sdk.bundle import BundleVerification, extract_bundle, verify_bundle
from discovery.packs.sdk.manifest import PackManifest, manifest_to_pack_config
from .pack_sandbox import (
    STAGE_ADMISSION,
    SandboxReport,
    run_sandbox_validation,
    sandbox_pack_directory,
)

from .pack_certification_policy import (
    PackCertificationPolicyUnavailable,
    get_certification_policy,
)

logger = logging.getLogger(__name__)

# ── Installed-pack status ─────────────────────────────────────────────────────

#: Present and validated, not executing.
STATUS_INSTALLED = "installed"
#: Present and eligible to execute in future runs.
STATUS_ACTIVE = "active"
#: Withdrawn from service. The record and its provenance remain.
STATUS_INACTIVE = "inactive"

INSTALLED_PACK_STATUSES = frozenset({STATUS_INSTALLED, STATUS_ACTIVE, STATUS_INACTIVE})

# ── Refusal reasons ───────────────────────────────────────────────────────────

REASON_BUNDLE_UNVERIFIED = "bundle_unverified"
REASON_VALIDATION_FAILED = "validation_failed"
#: The pack's own fixtures are too large or too slow to judge. Distinct from a
#: validation failure on purpose: "your pack is wrong" and "your fixtures cost
#: more than this deployment will spend" need different actions from an author.
REASON_SANDBOX_LIMIT = "sandbox_limit_exceeded"
REASON_INCOMPATIBLE = "incompatible_with_platform"
REASON_CERTIFICATION_POLICY = "certification_policy_violation"
REASON_CERTIFICATION_POLICY_UNAVAILABLE = "certification_policy_unavailable"
REASON_RESERVED_PACK_ID = "reserved_pack_id"
REASON_NOT_INSTALLED = "pack_not_installed"


class PackInstallRefused(Exception):
    """An install or activation was refused, with the gate that refused it named.

    ``reason`` is machine-readable (the ``REASON_*`` constants) and ``failures``
    carries the specific problems, so an API can report *what* failed rather than
    that something did.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        failures: Optional[Sequence[str]] = None,
        pack_id: str = "",
    ) -> None:
        self.reason = reason
        self.failures: List[str] = list(failures or [])
        self.pack_id = pack_id
        super().__init__(message)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": self.reason,
            "message": str(self),
            "packId": self.pack_id,
            "failures": list(self.failures),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


@dataclass(frozen=True)
class InstalledPack:
    """One authored pack installed into one org."""

    org_id: str
    pack_id: str
    pack_version: str
    status: str
    manifest: Mapping[str, Any]
    manifest_fingerprint: str
    bundle_digest: str
    publisher: str = ""
    signing_key_id: str = ""
    requested_level: str = LEVEL_COMMUNITY
    #: The author's fixtures, kept so activation can re-run them (AT-841). A pack
    #: that cannot be re-validated is one the platform must take on trust at the
    #: moment it starts executing.
    fixtures: Sequence[Mapping[str, Any]] = ()
    #: The most recent sandbox verdict, as ``SandboxReport.to_dict()``.
    validation: Mapping[str, Any] = field(default_factory=dict)
    revision: int = 1
    installed_by: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def certification_level(self) -> str:
        """Always ``community``.

        An authored pack carries a certification REQUEST, never a grant: only a
        CloudFulcrum signature (2.0-C2 AT-831) can raise a level, and it is issued
        against a reviewed pack rather than shipped inside the artifact being
        reviewed. Returning the requested level here would let a publisher set
        their own badge by editing a JSON field.
        """
        return LEVEL_COMMUNITY

    def to_dict(self) -> Dict[str, Any]:
        return {
            "packId": self.pack_id,
            "packName": str(
                (self.manifest.get("pack") or {}).get("packName") or self.pack_id
            ),
            "packVersion": self.pack_version,
            "status": self.status,
            "active": self.status == STATUS_ACTIVE,
            "manifestFingerprint": self.manifest_fingerprint,
            "bundleDigest": self.bundle_digest,
            "publisher": self.publisher,
            "signingKeyId": self.signing_key_id,
            "certificationLevel": self.certification_level,
            "certificationLabel": LEVEL_LABELS.get(self.certification_level, ""),
            "requestedCertificationLevel": self.requested_level,
            "revision": self.revision,
            "validation": dict(self.validation) if self.validation else None,
            "fixtureCount": len(self.fixtures),
            "installedBy": self.installed_by,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


# ── Store ─────────────────────────────────────────────────────────────────────


class InstalledPackStore:
    """Read/write contract for the installed-pack registry. No delete operation."""

    def get(self, org_id: str, pack_id: str) -> Optional[InstalledPack]:
        raise NotImplementedError

    def list(self, org_id: str) -> List[InstalledPack]:
        raise NotImplementedError

    def upsert(self, record: InstalledPack) -> InstalledPack:
        raise NotImplementedError

    def set_status(self, org_id: str, pack_id: str, status: str, actor_id: str) -> InstalledPack:
        raise NotImplementedError

    def record_validation(
        self, org_id: str, pack_id: str, validation: Mapping[str, Any]
    ) -> InstalledPack:
        """Store a fresh sandbox verdict without touching status.

        Separate from ``set_status`` because a re-validation that REFUSES must
        still be recorded — an operator needs to see why activation was blocked,
        and folding it into the status write would only ever save the passes.
        """
        raise NotImplementedError


class InMemoryInstalledPackStore(InstalledPackStore):
    """Thread-safe contract implementation for offline runs and tests."""

    def __init__(self) -> None:
        self._rows: Dict[tuple, InstalledPack] = {}
        self._lock = threading.RLock()

    def get(self, org_id: str, pack_id: str) -> Optional[InstalledPack]:
        with self._lock:
            return self._rows.get((_required(org_id, "org_id"), _required(pack_id, "pack_id")))

    def list(self, org_id: str) -> List[InstalledPack]:
        org = _required(org_id, "org_id")
        with self._lock:
            return sorted(
                (row for (row_org, _), row in self._rows.items() if row_org == org),
                key=lambda record: record.pack_id,
            )

    def upsert(self, record: InstalledPack) -> InstalledPack:
        with self._lock:
            key = (record.org_id, record.pack_id)
            existing = self._rows.get(key)
            stored = InstalledPack(
                **{
                    **record.__dict__,
                    "revision": (existing.revision + 1) if existing else 1,
                    "created_at": existing.created_at if existing else record.created_at,
                }
            )
            self._rows[key] = stored
            return stored

    def set_status(self, org_id: str, pack_id: str, status: str, actor_id: str) -> InstalledPack:
        with self._lock:
            key = (_required(org_id, "org_id"), _required(pack_id, "pack_id"))
            existing = self._rows.get(key)
            if existing is None:
                raise PackInstallRefused(
                    REASON_NOT_INSTALLED,
                    f"pack '{pack_id}' is not installed for this org",
                    pack_id=str(pack_id),
                )
            updated = InstalledPack(
                **{
                    **existing.__dict__,
                    "status": status,
                    "revision": existing.revision + 1,
                    "updated_at": _now(),
                    "installed_by": actor_id or existing.installed_by,
                }
            )
            self._rows[key] = updated
            return updated

    def record_validation(
        self, org_id: str, pack_id: str, validation: Mapping[str, Any]
    ) -> InstalledPack:
        with self._lock:
            key = (_required(org_id, "org_id"), _required(pack_id, "pack_id"))
            existing = self._rows.get(key)
            if existing is None:
                raise PackInstallRefused(
                    REASON_NOT_INSTALLED,
                    f"pack '{pack_id}' is not installed for this org",
                    pack_id=str(pack_id),
                )
            updated = InstalledPack(
                **{**existing.__dict__, "validation": dict(validation)}
            )
            self._rows[key] = updated
            return updated


#: The stored column list, written once. It appears in five statements below and
#: is positionally coupled to ``_row_to_record`` — five hand-maintained copies of
#: a column order is a bug waiting for the next column.
_COLUMNS = (
    "org_id, pack_id, pack_version, status, manifest, manifest_fingerprint, "
    "bundle_digest, publisher, signing_key_id, requested_level, fixtures, "
    "validation, revision, installed_by, created_at, updated_at"
)


class PostgresInstalledPackStore(InstalledPackStore):
    """Production store. Migration 0036 / provision.sql provision the table."""

    def get(self, org_id: str, pack_id: str) -> Optional[InstalledPack]:
        org = _required(org_id, "org_id")
        pack = _required(pack_id, "pack_id")
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                f"SELECT {_COLUMNS} FROM installed_packs "
                "WHERE org_id = %s AND pack_id = %s",
                (org, pack),
            )
            row = cur.fetchone()
        finally:
            con.close()
        return _row_to_record(row) if row else None

    def list(self, org_id: str) -> List[InstalledPack]:
        org = _required(org_id, "org_id")
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                f"SELECT {_COLUMNS} FROM installed_packs "
                "WHERE org_id = %s ORDER BY pack_id",
                (org,),
            )
            rows = cur.fetchall()
        finally:
            con.close()
        return [_row_to_record(row) for row in rows]

    def upsert(self, record: InstalledPack) -> InstalledPack:
        now = _now()
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                f"""
                INSERT INTO installed_packs (
                    org_id, pack_id, pack_version, status, manifest,
                    manifest_fingerprint, bundle_digest, publisher, signing_key_id,
                    requested_level, fixtures, validation, revision, installed_by,
                    created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s)
                ON CONFLICT (org_id, pack_id) DO UPDATE SET
                    pack_version = EXCLUDED.pack_version,
                    status = EXCLUDED.status,
                    manifest = EXCLUDED.manifest,
                    manifest_fingerprint = EXCLUDED.manifest_fingerprint,
                    bundle_digest = EXCLUDED.bundle_digest,
                    publisher = EXCLUDED.publisher,
                    signing_key_id = EXCLUDED.signing_key_id,
                    requested_level = EXCLUDED.requested_level,
                    fixtures = EXCLUDED.fixtures,
                    validation = EXCLUDED.validation,
                    revision = installed_packs.revision + 1,
                    installed_by = EXCLUDED.installed_by,
                    updated_at = EXCLUDED.updated_at
                RETURNING {_COLUMNS}
                """,
                (
                    record.org_id,
                    record.pack_id,
                    record.pack_version,
                    record.status,
                    json.dumps(dict(record.manifest)),
                    record.manifest_fingerprint,
                    record.bundle_digest,
                    record.publisher,
                    record.signing_key_id,
                    record.requested_level,
                    json.dumps(list(record.fixtures)),
                    json.dumps(dict(record.validation)),
                    record.installed_by,
                    record.created_at or now,
                    now,
                ),
            )
            row = cur.fetchone()
            con.commit()
        finally:
            con.close()
        return _row_to_record(row)

    def set_status(self, org_id: str, pack_id: str, status: str, actor_id: str) -> InstalledPack:
        org = _required(org_id, "org_id")
        pack = _required(pack_id, "pack_id")
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                f"""
                UPDATE installed_packs
                   SET status = %s,
                       revision = revision + 1,
                       installed_by = COALESCE(NULLIF(%s, ''), installed_by),
                       updated_at = %s
                 WHERE org_id = %s AND pack_id = %s
                RETURNING {_COLUMNS}
                """,
                (status, actor_id or "", _now(), org, pack),
            )
            row = cur.fetchone()
            con.commit()
        finally:
            con.close()
        if row is None:
            raise PackInstallRefused(
                REASON_NOT_INSTALLED,
                f"pack '{pack}' is not installed for this org",
                pack_id=pack,
            )
        return _row_to_record(row)

    def record_validation(
        self, org_id: str, pack_id: str, validation: Mapping[str, Any]
    ) -> InstalledPack:
        org = _required(org_id, "org_id")
        pack = _required(pack_id, "pack_id")
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                f"""
                UPDATE installed_packs
                   SET validation = %s,
                       updated_at = %s
                 WHERE org_id = %s AND pack_id = %s
                RETURNING {_COLUMNS}
                """,
                (json.dumps(dict(validation)), _now(), org, pack),
            )
            row = cur.fetchone()
            con.commit()
        finally:
            con.close()
        if row is None:
            raise PackInstallRefused(
                REASON_NOT_INSTALLED,
                f"pack '{pack}' is not installed for this org",
                pack_id=pack,
            )
        return _row_to_record(row)


def _json_column(value: Any, fallback: Any) -> Any:
    """Read a JSONB column that psycopg2 may hand back as text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:  # pragma: no cover - defensive
            return fallback
    return value if value is not None else fallback


def _row_to_record(row: Sequence[Any]) -> InstalledPack:
    manifest = _json_column(row[4], {})
    return InstalledPack(
        org_id=str(row[0]),
        pack_id=str(row[1]),
        pack_version=str(row[2]),
        status=str(row[3]),
        manifest=manifest or {},
        manifest_fingerprint=str(row[5] or ""),
        bundle_digest=str(row[6] or ""),
        publisher=str(row[7] or ""),
        signing_key_id=str(row[8] or ""),
        requested_level=str(row[9] or LEVEL_COMMUNITY),
        fixtures=tuple(_json_column(row[10], []) or ()),
        validation=_json_column(row[11], {}) or {},
        revision=int(row[12] or 1),
        installed_by=str(row[13] or ""),
        created_at=_iso(row[14]),
        updated_at=_iso(row[15]),
    )


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


_STORE: Optional[InstalledPackStore] = None


def get_installed_pack_store() -> InstalledPackStore:
    global _STORE
    if _STORE is None:
        _STORE = PostgresInstalledPackStore()
    return _STORE


def set_installed_pack_store(store: Optional[InstalledPackStore]) -> None:
    """Test/offline injection seam; ``None`` restores the production store."""
    global _STORE
    _STORE = store


# ── The gates ─────────────────────────────────────────────────────────────────


def _verify_bundle_or_refuse(bundle: Any) -> BundleVerification:
    verification = verify_bundle(bundle)
    if not verification.ok:
        raise PackInstallRefused(
            REASON_BUNDLE_UNVERIFIED,
            f"Bundle could not be verified ({verification.reason}): {verification.detail}",
            failures=[verification.detail],
            pack_id=verification.pack_id,
        )
    return verification


def _refuse_sandbox(report: SandboxReport, pack_id: str) -> "PackInstallRefused":
    """Translate a failed sandbox verdict into the refusal for its stage."""
    if report.stage == STAGE_ADMISSION:
        return PackInstallRefused(
            REASON_SANDBOX_LIMIT,
            (
                f"Pack '{pack_id}' could not be validated within this deployment's "
                f"sandbox limits"
            ),
            failures=report.reasons,
            pack_id=pack_id,
        )
    return PackInstallRefused(
        REASON_VALIDATION_FAILED,
        f"Pack '{pack_id}' failed {report.stage} validation",
        failures=report.reasons,
        pack_id=pack_id,
    )


def _validate_or_refuse(
    bundle: Any, pack_id: str
) -> "tuple[PackManifest, List[Dict[str, Any]], SandboxReport]":
    """Run the author's own toolkit check over the extracted bundle, bounded.

    Extraction happens into a temporary directory that is removed on the way out,
    whatever the outcome: an installation that refused must leave nothing behind.
    The fixtures are read out before that directory goes, because activation
    (AT-841) has to be able to run them again later — see
    :func:`set_installed_pack_activation`.
    """
    with tempfile.TemporaryDirectory(prefix="agentiq-pack-install-") as workspace:
        extract_bundle(bundle, workspace)
        report = sandbox_pack_directory(Path(workspace))
        if not report.ok or report.manifest is None:
            raise _refuse_sandbox(report, pack_id)
        return report.manifest, list(report.cases), report


def check_installed_compatibility(manifest: PackManifest) -> PackCompatibility:
    """The 2.0-C1 gate for an authored pack, via the SHARED declaration rule."""
    return check_declaration_compatibility(
        pack_id=manifest.pack_id,
        pack_name=manifest.pack_name,
        pack_version=manifest.pack_version,
        declaration={
            "minPlatformVersion": manifest.min_platform_version,
            "maxPlatformVersion": manifest.max_platform_version,
            "requiredConcepts": list(manifest.required_concepts),
            "optionalConcepts": list(manifest.optional_concepts),
        },
    )


def _compatibility_or_refuse(manifest: PackManifest) -> PackCompatibility:
    report = check_installed_compatibility(manifest)
    if not report.compatible:
        raise PackInstallRefused(
            REASON_INCOMPATIBLE,
            report.reason,
            failures=[item.detail for item in report.unmet],
            pack_id=manifest.pack_id,
        )
    return report


def _certification_policy_or_refuse(org_id: str, pack_id: str) -> None:
    """The 2.0-C2 gate. Fail-CLOSED, deliberately.

    An authored pack is Community. Under a Certified-only floor it is refused with
    the floor named; and if the policy cannot be READ, the install is refused
    rather than assumed compliant — a control that fails open lifts the
    restriction exactly when it matters.
    """
    try:
        policy = get_certification_policy(org_id)
    except PackCertificationPolicyUnavailable as exc:
        raise PackInstallRefused(
            REASON_CERTIFICATION_POLICY_UNAVAILABLE,
            (
                "This organisation's pack certification policy could not be read, so "
                "compliance cannot be verified; installation is refused."
            ),
            pack_id=pack_id,
        ) from exc
    if not policy.permits(LEVEL_COMMUNITY):
        raise PackInstallRefused(
            REASON_CERTIFICATION_POLICY,
            (
                f"Pack '{pack_id}' is {LEVEL_LABELS[LEVEL_COMMUNITY]} — an authored "
                f"pack holds no CloudFulcrum-signed certification — and this "
                f"organisation restricts activation to "
                f"{LEVEL_LABELS.get(policy.minimum_level, policy.minimum_level)} "
                f"packs or above."
            ),
            pack_id=pack_id,
        )


# ── Public API ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InstallOutcome:
    """What an install did, including the gate reports it passed."""

    record: InstalledPack
    compatibility: PackCompatibility
    verification: BundleVerification
    activated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.record.to_dict(),
            "activated": self.activated,
            "compatibility": self.compatibility.to_dict(),
            "bundle": {
                "digest": self.verification.bundle_digest,
                "keyId": self.verification.key_id,
                "fileCount": len(self.verification.files),
            },
        }


def install_pack_bundle(
    org_id: str,
    bundle: Any,
    *,
    actor_id: str = "",
    activate: bool = False,
) -> InstallOutcome:
    """Install a signed bundle for one org, running every gate in order.

    Raises :class:`PackInstallRefused` naming the gate that refused. Returns the
    stored record on success.
    """
    org = _required(org_id, "org_id")
    verification = _verify_bundle_or_refuse(bundle)

    if verification.pack_id in PACK_REGISTRY:
        # The manifest schema already refuses a first-party id; this is the guard
        # for a bundle built before a pack of that name shipped.
        raise PackInstallRefused(
            REASON_RESERVED_PACK_ID,
            (
                f"'{verification.pack_id}' is a first-party pack id and cannot be "
                f"installed over"
            ),
            pack_id=verification.pack_id,
        )

    manifest, cases, validation = _validate_or_refuse(bundle, verification.pack_id)
    compatibility = _compatibility_or_refuse(manifest)
    _certification_policy_or_refuse(org, manifest.pack_id)

    now = _now()
    record = InstalledPack(
        org_id=org,
        pack_id=manifest.pack_id,
        pack_version=manifest.pack_version,
        status=STATUS_ACTIVE if activate else STATUS_INSTALLED,
        manifest=manifest.to_dict(),
        manifest_fingerprint=verification.manifest_fingerprint,
        bundle_digest=verification.bundle_digest,
        publisher=manifest.author.name,
        signing_key_id=verification.key_id,
        requested_level=manifest.requested_certification_level,
        fixtures=tuple(cases),
        validation=validation.to_dict(),
        installed_by=actor_id,
        created_at=now,
        updated_at=now,
    )
    stored = get_installed_pack_store().upsert(record)
    return InstallOutcome(
        record=stored,
        compatibility=compatibility,
        verification=verification,
        activated=stored.status == STATUS_ACTIVE,
    )


def set_installed_pack_activation(
    org_id: str, pack_id: str, *, active: bool, actor_id: str = ""
) -> InstalledPack:
    """Activate or withdraw an installed pack, re-running the gates on activation.

    Activation re-checks compatibility and the certification floor because both can
    move after install: the platform can be upgraded past the pack's declared
    ceiling, and an owner can raise the floor to Certified-only. Trusting the
    install-time verdict is how a restricted deployment quietly ends up running a
    pack it now forbids.

    Deactivation runs no gates — withdrawing a pack from service is always allowed
    — and never deletes: the record stays with its provenance intact.
    """
    org = _required(org_id, "org_id")
    pack = _required(pack_id, "pack_id")
    store = get_installed_pack_store()
    existing = store.get(org, pack)
    if existing is None:
        raise PackInstallRefused(
            REASON_NOT_INSTALLED,
            f"pack '{pack}' is not installed for this org",
            pack_id=pack,
        )
    if not active:
        return store.set_status(org, pack, STATUS_INACTIVE, actor_id)

    # AT-841: the manifest and the author's fixtures are re-run against TODAY's
    # platform before the pack is allowed to execute. The install-time verdict is
    # not carried forward — a pack installed months ago was judged by a platform
    # that has since moved.
    revalidated = revalidate_installed_pack(existing)
    if not revalidated.ok:
        raise _refuse_sandbox(revalidated, pack)

    manifest = revalidated.manifest or _manifest_of(existing)
    if manifest is not None:
        _compatibility_or_refuse(manifest)
    _certification_policy_or_refuse(org, pack)
    return store.set_status(org, pack, STATUS_ACTIVE, actor_id)


def revalidate_installed_pack(
    record: InstalledPack, *, persist: bool = True
) -> SandboxReport:
    """Re-run the sandbox over an installed pack's stored manifest and fixtures.

    The verdict is persisted whether it passed or failed (``persist``), because an
    operator looking at a pack that will not activate needs the reasons without
    re-uploading the bundle. A persistence failure is logged and swallowed: the
    verdict itself is what the caller acts on, and losing the audit copy must not
    turn a passing pack into a refused one.
    """
    report = run_sandbox_validation(
        dict(record.manifest or {}), list(record.fixtures or ())
    )
    if persist:
        try:
            get_installed_pack_store().record_validation(
                record.org_id, record.pack_id, report.to_dict()
            )
        except Exception:  # noqa: BLE001 - the verdict outranks its audit copy
            logger.warning(
                "Could not persist the sandbox verdict for pack %s", record.pack_id,
                exc_info=True,
            )
    return report


def _manifest_of(record: InstalledPack) -> Optional[PackManifest]:
    """Re-parse a stored manifest, or ``None`` if it can no longer be parsed.

    A stored manifest that no longer validates (the platform moved on) yields
    ``None`` rather than raising, so the CALLER's gate decides — activation still
    runs the certification floor, and the compatibility gate is skipped only
    because there is no declaration left to judge.
    """
    from discovery.packs.sdk.manifest import validate_manifest

    result = validate_manifest(dict(record.manifest))
    return result.manifest if result.ok else None


def get_installed_pack(org_id: str, pack_id: str) -> Optional[InstalledPack]:
    return get_installed_pack_store().get(_required(org_id, "org_id"), _required(pack_id, "pack_id"))


def list_installed_packs(org_id: str) -> List[InstalledPack]:
    return get_installed_pack_store().list(_required(org_id, "org_id"))


def get_installed_pack_validation(org_id: str, pack_id: str) -> Optional[Dict[str, Any]]:
    """The stored sandbox verdict for an installed pack, or ``None`` if unknown."""
    record = get_installed_pack(org_id, pack_id)
    return dict(record.validation or {}) if record is not None else None


def installed_packs_safe(org_id: str) -> List[InstalledPack]:
    """Display read. Degrades to an empty list rather than breaking a page."""
    try:
        return list_installed_packs(org_id)
    except Exception:  # noqa: BLE001
        logger.warning("Could not read installed packs for org=%s", org_id, exc_info=True)
        return []


def active_installed_pack_ids(org_id: str) -> List[str]:
    """Ids of the org's ACTIVE authored packs — what a run would consider."""
    return [
        record.pack_id
        for record in installed_packs_safe(org_id)
        if record.status == STATUS_ACTIVE
    ]


def installed_pack_config(org_id: str, pack_id: str) -> Optional[Dict[str, Any]]:
    """The registry-shaped config for an installed pack, or ``None``.

    The seam a runner integration reads: the manifest projected into the shape the
    pack lifecycle already understands (2.0-C3 T1's ``manifest_to_pack_config``),
    so an authored pack rides the existing paths rather than a parallel one.
    """
    record = get_installed_pack(org_id, pack_id)
    if record is None:
        return None
    manifest = _manifest_of(record)
    if manifest is None:
        return None
    config = manifest_to_pack_config(manifest)
    config["installed"] = {
        "orgId": record.org_id,
        "status": record.status,
        "bundleDigest": record.bundle_digest,
        "signingKeyId": record.signing_key_id,
    }
    return config


__all__ = [
    "INSTALLED_PACK_STATUSES",
    "REASON_BUNDLE_UNVERIFIED",
    "REASON_CERTIFICATION_POLICY",
    "REASON_CERTIFICATION_POLICY_UNAVAILABLE",
    "REASON_INCOMPATIBLE",
    "REASON_NOT_INSTALLED",
    "REASON_RESERVED_PACK_ID",
    "REASON_SANDBOX_LIMIT",
    "REASON_VALIDATION_FAILED",
    "STATUS_ACTIVE",
    "STATUS_INACTIVE",
    "STATUS_INSTALLED",
    "InMemoryInstalledPackStore",
    "InstallOutcome",
    "InstalledPack",
    "InstalledPackStore",
    "PackInstallRefused",
    "PostgresInstalledPackStore",
    "active_installed_pack_ids",
    "check_installed_compatibility",
    "get_installed_pack",
    "get_installed_pack_store",
    "get_installed_pack_validation",
    "install_pack_bundle",
    "installed_pack_config",
    "installed_packs_safe",
    "list_installed_packs",
    "revalidate_installed_pack",
    "set_installed_pack_activation",
    "set_installed_pack_store",
]
