"""Pack certification metadata — 2.0-C2 T1 (AT-831).

The rule this module owns (parent story AC1):

    Certification metadata is signature-verified; a pack claiming Certified
    without a valid signature is treated as Community.

Three levels
------------
``certified`` (CloudFulcrum Certified), ``partner``, ``community`` — ordered, so a
policy can express "Certified only" (2.0-C2 T4 / AT-834) as a floor rather than an
enumeration.

Why a signature at all
----------------------
Certification is a *claim about who reviewed this pack*. With partner-authored
packs coming (2.0-C3), the pack manifest is supplied by the party being vouched
for, so an unsigned ``"level": "certified"`` string is worth nothing — the author
would be certifying themselves. The metadata is therefore signed by CloudFulcrum
with a key whose PRIVATE half never ships, and this module verifies against the
PUBLIC half that does. A pack that cannot prove its badge does not lose its
metadata, it loses its **badge**: the declared level is preserved and reported,
the *effective* level falls back to ``community``, and the reason is named.

``community`` is deliberately the un-signed level. It is the honest label for
"nobody has vouched for this", so requiring a signature to claim it would be
backwards — a community pack self-declares, which is exactly what the badge means.

What is signed
--------------
The canonical JSON of :func:`certification_payload` — pack id, level, certifying
entity, review date, reviewed-against platform version, and the certification's
scope. Every field a reader would rely on is inside the signature, so none of them
can be edited after issuance without invalidating it.

The pack's own ``packVersion`` is deliberately NOT signed. A certification is a
statement about a reviewed *pack and its criteria*, and binding it to a version
string would invalidate every signature on a patch bump — turning routine
maintenance into a re-issuance ceremony, which in practice trains people to
disable the check. Version-scoped review lands where it belongs: on the review
date and the reviewed-against platform version (see "Review due" below).

Review due
----------
A certification carries the platform version it was reviewed against.
:attr:`PackCertification.review_due` reports when the running platform has moved
past it at MAJOR.MINOR granularity — the pack keeps its (validly signed) badge and
is additionally flagged as due for review, rather than silently retaining a badge
earned against a platform that no longer exists. Patch-level platform movement does
not trigger it. Date-based expiry is 2.0-C2 T5's concern; this module supplies the
``review_date`` it will read.

Deliberately dependency-free of ``app``
---------------------------------------
Same posture as ``platform_capabilities.py`` / ``pack_compatibility.py``: no ``app``
import and no DB, so the activation edges AND the discovery runner can both consult
certification without the runner taking an ``app`` dependency.

Fail closed
-----------
Every failure path — malformed signature, unknown key id, unsupported algorithm,
missing metadata, an unavailable crypto backend — downgrades to ``community`` with a
named reason. There is no path where an unverifiable claim keeps its badge.

Scope note
----------
This is the metadata + signature half of 2.0-C2. The internal review workflow
(AT-832), surfacing (AT-833), org policy control (AT-834), and date-based expiry
(AT-835) are separate tasks layered on top of what this module reports.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .pack_config import (
    get_pack,
    get_pack_certification_declaration,
    get_pack_version,
    normalize_pack_ids,
)
from .platform_capabilities import get_platform_version, parse_version

logger = logging.getLogger(__name__)

# ── Levels ────────────────────────────────────────────────────────────────────

#: Reviewed and vouched for by CloudFulcrum itself.
LEVEL_CERTIFIED = "certified"
#: Authored by a partner, reviewed and signed by CloudFulcrum.
LEVEL_PARTNER = "partner"
#: Self-declared. The honest label for "nobody has vouched for this".
LEVEL_COMMUNITY = "community"

#: Every legal level, strongest first.
CERTIFICATION_LEVELS: List[str] = [LEVEL_CERTIFIED, LEVEL_PARTNER, LEVEL_COMMUNITY]

#: Ordering for policy floors (2.0-C2 T4): higher rank == stronger assurance.
LEVEL_RANK: Dict[str, int] = {
    LEVEL_COMMUNITY: 0,
    LEVEL_PARTNER: 1,
    LEVEL_CERTIFIED: 2,
}

#: Human-readable labels — the exact strings surfacing and exports use, so a board
#: paper and the run-configuration screen cannot word the same badge differently.
LEVEL_LABELS: Dict[str, str] = {
    LEVEL_CERTIFIED: "CloudFulcrum Certified",
    LEVEL_PARTNER: "Partner",
    LEVEL_COMMUNITY: "Community",
}

#: The levels whose claim must be proved by a CloudFulcrum signature (AC1).
SIGNATURE_REQUIRED_LEVELS = frozenset({LEVEL_CERTIFIED, LEVEL_PARTNER})

#: The only entity that may certify at the ``certified`` level. Carried inside the
#: signed payload, so it cannot be forged independently of the signature.
CLOUDFULCRUM = "CloudFulcrum"


# ── Signature envelope ────────────────────────────────────────────────────────

#: Signature payload schema tag. Part of the signed bytes, so a future schema change
#: cannot be replayed against this one. Bump alongside any payload-shape change.
SIGNATURE_PAYLOAD_VERSION = "agentiq-pack-certification-v1"

#: The only signature algorithm accepted. Ed25519: small keys, no parameter choices
#: to get wrong, and verification needs only the public half.
ALGORITHM_ED25519 = "ed25519"

#: CloudFulcrum's pack-certification signing keys, ``{key_id: base64 raw public key}``.
#:
#: PUBLIC halves only — the private halves live in CloudFulcrum's secrets management
#: and never ship, which is precisely what stops a pack author self-applying a badge.
#: Rotation is additive: add the new key id here, re-issue signatures, and retire the
#: old id in a later release once nothing references it.
CLOUDFULCRUM_SIGNING_KEYS: Dict[str, str] = {
    "cloudfulcrum-pack-signing-2026": (
        "r3WNaYXXgAvgcqWGWUfuT433vSSbF/GNLWi1CWw9aHw="
    ),
}

#: Optional deployment-supplied ADDITIONAL trust anchors, as a JSON object
#: ``{"key_id": "<base64 raw ed25519 public key>"}``. Public keys only — never a
#: credential, so this is not a secret env var. Used for a signing-key rotation that
#: must land without a code deploy, and by pack authors trusting their own dev key
#: locally. A built-in key id can NEVER be overridden from the environment: swapping
#: the CloudFulcrum anchor for another key would make the badge meaningless.
TRUSTED_KEYS_ENV_VAR = "PACK_CERTIFICATION_TRUSTED_KEYS"


# ── Verification failure kinds ────────────────────────────────────────────────

#: Level claimed is not one of the three legal levels.
REASON_INVALID_LEVEL = "invalid_level"
#: A signature-required level declared metadata this module needs but did not get.
REASON_MISSING_METADATA = "missing_certification_metadata"
#: A signature-required level shipped no signature at all (the self-applied case).
REASON_SIGNATURE_MISSING = "signature_missing"
#: The signature block is present but not decodable / not the expected shape.
REASON_SIGNATURE_MALFORMED = "signature_malformed"
#: Signed by a key this platform does not trust.
REASON_SIGNATURE_UNKNOWN_KEY = "signature_unknown_key"
#: Signed with an algorithm this platform does not accept.
REASON_SIGNATURE_UNSUPPORTED_ALGORITHM = "signature_unsupported_algorithm"
#: Signature present, trusted key, correct algorithm — and it does not verify.
REASON_SIGNATURE_INVALID = "signature_invalid"
#: No crypto backend available to verify with. Fails closed, never "assume valid".
REASON_SIGNATURE_BACKEND_UNAVAILABLE = "signature_backend_unavailable"

#: Review-due kinds.
REVIEW_DUE_PLATFORM_MOVED = "reviewed_against_older_platform"
REVIEW_DUE_UNDECLARED = "reviewed_against_platform_version_undeclared"


class PackCertificationError(ValueError):
    """Raised by the SIGNING helpers on malformed input. Verification never raises."""


# ── Canonical payload ─────────────────────────────────────────────────────────


def certification_payload(
    pack_id: str, declaration: Mapping[str, Any]
) -> Dict[str, Any]:
    """The exact object a certification signature covers.

    Deterministic and closed: every reader-facing certification field is inside it,
    and nothing else is. ``declaration`` is a normalised block from
    :func:`pack_config.get_pack_certification_declaration`.
    """
    scope = declaration.get("scope") or {}
    return {
        "payloadVersion": SIGNATURE_PAYLOAD_VERSION,
        "packId": str(pack_id or "").strip(),
        "level": str(declaration.get("level") or "").strip().lower(),
        "certifyingEntity": _text(declaration.get("certifyingEntity")),
        "reviewDate": _text(declaration.get("reviewDate")),
        "reviewedAgainstPlatformVersion": _text(
            declaration.get("reviewedAgainstPlatformVersion")
        ),
        "scope": {
            "summary": _text(scope.get("summary")),
            "criteria": [str(item) for item in (scope.get("criteria") or [])],
        },
    }


def canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """Canonical UTF-8 bytes of a payload — sorted keys, no insignificant space.

    One serialisation, used for BOTH signing and verification, so a signature can
    never depend on dict ordering or formatting.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


# ── Trust anchors ─────────────────────────────────────────────────────────────

_TRUSTED_KEYS_OVERRIDE: Optional[Dict[str, str]] = None


def trusted_signing_keys() -> Dict[str, str]:
    """Every trusted public key, ``{key_id: base64 raw public key}``.

    Built-ins, plus any additional anchors from :data:`TRUSTED_KEYS_ENV_VAR`. A
    built-in id present in the environment map is IGNORED with a warning — an
    operator may add trust, never silently substitute CloudFulcrum's.
    """
    if _TRUSTED_KEYS_OVERRIDE is not None:
        return dict(_TRUSTED_KEYS_OVERRIDE)

    keys = dict(CLOUDFULCRUM_SIGNING_KEYS)
    raw = os.environ.get(TRUSTED_KEYS_ENV_VAR, "").strip()
    if not raw:
        return keys
    try:
        extra = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s is not valid JSON; ignoring deployment-supplied trust anchors",
            TRUSTED_KEYS_ENV_VAR,
        )
        return keys
    if not isinstance(extra, dict):
        logger.warning(
            "%s must be a JSON object of {keyId: base64PublicKey}; ignoring",
            TRUSTED_KEYS_ENV_VAR,
        )
        return keys
    for key_id, material in extra.items():
        key = str(key_id or "").strip()
        if not key or not isinstance(material, str) or not material.strip():
            continue
        if key in CLOUDFULCRUM_SIGNING_KEYS:
            logger.warning(
                "%s tried to redefine built-in signing key %r; ignoring that entry",
                TRUSTED_KEYS_ENV_VAR,
                key,
            )
            continue
        keys[key] = material.strip()
    return keys


def trusted_key_ids() -> List[str]:
    """Sorted ids of every key whose signatures this platform accepts."""
    return sorted(trusted_signing_keys())


def set_trusted_signing_keys(keys: Optional[Mapping[str, str]]) -> None:
    """Test/authoring injection seam; ``None`` restores the shipped anchors."""
    global _TRUSTED_KEYS_OVERRIDE
    _TRUSTED_KEYS_OVERRIDE = dict(keys) if keys is not None else None


# ── Signature verification ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SignatureVerification:
    """Verdict on one certification signature. Never raises; the verdict is the value."""

    verified: bool
    key_id: str
    algorithm: str
    present: bool
    reason: Optional[str] = None
    detail: Optional[str] = None


def verify_certification_signature(
    pack_id: str, declaration: Mapping[str, Any]
) -> SignatureVerification:
    """Verify a declaration's signature over its canonical payload.

    Fails closed on every error path, each with a distinct ``reason`` so a refusal
    or downgrade can say precisely what went wrong.
    """
    signature = declaration.get("signature") or {}
    key_id = _text(signature.get("keyId"))
    algorithm = _text(signature.get("algorithm")).lower()
    value = _text(signature.get("value"))
    present = bool(key_id or algorithm or value)

    if not value:
        return SignatureVerification(
            False, key_id, algorithm, present,
            REASON_SIGNATURE_MISSING,
            "no signature was supplied with the certification metadata",
        )
    if algorithm and algorithm != ALGORITHM_ED25519:
        return SignatureVerification(
            False, key_id, algorithm, present,
            REASON_SIGNATURE_UNSUPPORTED_ALGORITHM,
            f"signature algorithm {algorithm!r} is not accepted "
            f"(expected {ALGORITHM_ED25519!r})",
        )

    keys = trusted_signing_keys()
    public_key_material = keys.get(key_id) if key_id else None
    if public_key_material is None:
        return SignatureVerification(
            False, key_id, algorithm, present,
            REASON_SIGNATURE_UNKNOWN_KEY,
            f"signature key id {key_id!r} is not a trusted certification key",
        )

    try:
        signature_bytes = base64.b64decode(value, validate=True)
        public_key_bytes = base64.b64decode(public_key_material, validate=True)
    except (binascii.Error, ValueError):
        return SignatureVerification(
            False, key_id, algorithm, present,
            REASON_SIGNATURE_MALFORMED,
            "signature or trusted key material is not valid base64",
        )

    payload = canonical_payload_bytes(certification_payload(pack_id, declaration))
    try:
        verifier = _load_ed25519_public_key(public_key_bytes)
    except _CryptoBackendUnavailable as exc:
        # Never "assume valid": an environment that cannot verify has not verified.
        return SignatureVerification(
            False, key_id, algorithm, present,
            REASON_SIGNATURE_BACKEND_UNAVAILABLE,
            f"no signature verification backend available ({exc})",
        )
    except Exception:  # noqa: BLE001 — malformed key material
        return SignatureVerification(
            False, key_id, algorithm, present,
            REASON_SIGNATURE_MALFORMED,
            f"trusted key {key_id!r} is not a usable ed25519 public key",
        )

    try:
        verifier.verify(signature_bytes, payload)
    except Exception:  # noqa: BLE001 — cryptography raises InvalidSignature
        return SignatureVerification(
            False, key_id, algorithm, present,
            REASON_SIGNATURE_INVALID,
            "certification signature does not match the declared metadata",
        )
    return SignatureVerification(True, key_id, algorithm or ALGORITHM_ED25519, True)


class _CryptoBackendUnavailable(RuntimeError):
    """No Ed25519 implementation is importable in this environment."""


def _load_ed25519_public_key(public_key_bytes: bytes):
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except ImportError as exc:  # pragma: no cover - cryptography is a hard dep
        raise _CryptoBackendUnavailable(str(exc)) from exc
    return Ed25519PublicKey.from_public_bytes(public_key_bytes)


# ── Signing (issuance side) ───────────────────────────────────────────────────


def sign_certification(
    pack_id: str,
    declaration: Mapping[str, Any],
    private_key_seed: bytes,
    *,
    key_id: str,
) -> str:
    """Sign a declaration's canonical payload, returning the base64 signature.

    Issuance-side helper used by ``backend/scripts/sign_pack_certifications.py``
    and by tests that mint an ephemeral key. The private key never lives in this
    repository — it is supplied by whoever holds the release signing key.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except ImportError as exc:  # pragma: no cover - cryptography is a hard dep
        raise PackCertificationError(
            "cryptography is required to sign certification metadata"
        ) from exc
    if not str(key_id or "").strip():
        raise PackCertificationError("key_id is required to sign")
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_seed)
    payload = canonical_payload_bytes(certification_payload(pack_id, declaration))
    return base64.b64encode(private_key.sign(payload)).decode("ascii")


# ── The verdict ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PackCertification:
    """One pack's certification, as verified against this platform.

    ``declared_level`` is what the pack CLAIMS; ``effective_level`` is what the
    platform is willing to say on its behalf. They differ exactly when a claim could
    not be proved — the claim is never erased, because "claimed Certified, could not
    be verified" is more useful to a reviewer than a silently rewritten field.
    """

    pack_id: str
    pack_name: str
    pack_version: str
    declared_level: str
    effective_level: str
    certifying_entity: str
    review_date: str
    reviewed_against_platform_version: str
    platform_version: str
    scope: Dict[str, Any] = field(default_factory=dict)
    signature_present: bool = False
    signature_verified: bool = False
    signature_key_id: str = ""
    signature_algorithm: str = ""
    downgrade_reason: Optional[str] = None
    downgrade_detail: Optional[str] = None
    review_due_reason: Optional[str] = None

    @property
    def downgraded(self) -> bool:
        """True when the pack could not prove the level it claimed."""
        return self.declared_level != self.effective_level

    @property
    def review_due(self) -> bool:
        """True when this certification's platform scope no longer covers the platform."""
        return self.review_due_reason is not None

    @property
    def label(self) -> str:
        """The badge to display — always the EFFECTIVE level, never the claim."""
        return LEVEL_LABELS.get(self.effective_level, LEVEL_LABELS[LEVEL_COMMUNITY])

    @property
    def status_label(self) -> str:
        """Badge plus its qualifier, for a single-line surface."""
        if self.downgraded:
            return f"{self.label} (unverified {LEVEL_LABELS.get(self.declared_level, self.declared_level)} claim)"
        if self.review_due:
            return f"{self.label} — review due"
        return self.label

    @property
    def summary(self) -> str:
        """One human sentence explaining the effective level. Never empty."""
        if self.downgraded:
            return (
                f"Pack '{self.pack_id}' claims "
                f"{LEVEL_LABELS.get(self.declared_level, self.declared_level)} "
                f"certification but it could not be verified "
                f"({self.downgrade_detail or self.downgrade_reason}); it is treated "
                f"as {LEVEL_LABELS[LEVEL_COMMUNITY]}."
            )
        if self.effective_level == LEVEL_COMMUNITY:
            return (
                f"Pack '{self.pack_id}' is {LEVEL_LABELS[LEVEL_COMMUNITY]} — "
                f"self-declared, not reviewed or signed by {CLOUDFULCRUM}."
            )
        base = (
            f"Pack '{self.pack_id}' is {self.label}, certified by "
            f"{self.certifying_entity} on {self.review_date} against platform "
            f"version {self.reviewed_against_platform_version}."
        )
        if self.review_due:
            return (
                f"{base} This platform is version {self.platform_version}, so the "
                f"certification is due for review."
            )
        return base

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable shape — what surfacing (AT-833), policy control
        (AT-834), and exports consume."""
        return {
            "packId": self.pack_id,
            "packName": self.pack_name,
            "packVersion": self.pack_version,
            "declaredLevel": self.declared_level,
            "level": self.effective_level,
            "levelLabel": self.label,
            "statusLabel": self.status_label,
            "certifyingEntity": self.certifying_entity,
            "reviewDate": self.review_date,
            "reviewedAgainstPlatformVersion": (
                self.reviewed_against_platform_version
            ),
            "platformVersion": self.platform_version,
            "scope": dict(self.scope),
            "signaturePresent": self.signature_present,
            "signatureVerified": self.signature_verified,
            "signatureKeyId": self.signature_key_id,
            "signatureAlgorithm": self.signature_algorithm,
            "downgraded": self.downgraded,
            "downgradeReason": self.downgrade_reason,
            "downgradeDetail": self.downgrade_detail,
            "reviewDue": self.review_due,
            "reviewDueReason": self.review_due_reason,
            "summary": self.summary,
        }


def get_pack_certification(
    pack_id: Optional[str] = None,
    *,
    platform_version: Optional[str] = None,
) -> PackCertification:
    """Verify one pack's certification metadata. Never raises.

    ``pack_id`` resolves through ``get_pack()``, so an unknown id reports the
    default pack's certification exactly as it reports its detectors — an unknown id
    is not a certification failure.
    """
    pack = get_pack(pack_id)
    resolved_id = pack["packId"]
    declaration = get_pack_certification_declaration(pack_id)
    effective_platform = platform_version or get_platform_version()

    declared_level = str(declaration.get("level") or LEVEL_COMMUNITY).strip().lower()
    entity = _text(declaration.get("certifyingEntity"))
    review_date = _text(declaration.get("reviewDate"))
    reviewed_against = _text(declaration.get("reviewedAgainstPlatformVersion"))

    verification = SignatureVerification(
        False,
        _text((declaration.get("signature") or {}).get("keyId")),
        _text((declaration.get("signature") or {}).get("algorithm")).lower(),
        bool(_text((declaration.get("signature") or {}).get("value"))),
    )
    downgrade_reason: Optional[str] = None
    downgrade_detail: Optional[str] = None
    effective_level = LEVEL_COMMUNITY

    if declared_level not in LEVEL_RANK:
        downgrade_reason = REASON_INVALID_LEVEL
        downgrade_detail = (
            f"declared level {declared_level!r} is not one of "
            f"{', '.join(CERTIFICATION_LEVELS)}"
        )
        declared_level = declared_level or LEVEL_COMMUNITY
    elif declared_level not in SIGNATURE_REQUIRED_LEVELS:
        # Community is self-declared by definition — nothing to prove, nothing to
        # downgrade.
        effective_level = LEVEL_COMMUNITY
    else:
        missing = [
            name
            for name, value in (
                ("certifyingEntity", entity),
                ("reviewDate", review_date),
                ("reviewedAgainstPlatformVersion", reviewed_against),
            )
            if not value
        ]
        if missing:
            downgrade_reason = REASON_MISSING_METADATA
            downgrade_detail = (
                f"certification is missing required metadata: {', '.join(missing)}"
            )
        elif declared_level == LEVEL_CERTIFIED and entity != CLOUDFULCRUM:
            downgrade_reason = REASON_MISSING_METADATA
            downgrade_detail = (
                f"only {CLOUDFULCRUM} may certify at the "
                f"{LEVEL_LABELS[LEVEL_CERTIFIED]} level (declared "
                f"certifying entity was {entity!r})"
            )
        else:
            verification = verify_certification_signature(resolved_id, declaration)
            if verification.verified:
                effective_level = declared_level
            else:
                downgrade_reason = verification.reason
                downgrade_detail = verification.detail

    if downgrade_reason is not None:
        logger.warning(
            "Pack %r claims %s certification but it could not be verified (%s): %s "
            "— treating it as %s",
            resolved_id,
            declared_level,
            downgrade_reason,
            downgrade_detail,
            LEVEL_COMMUNITY,
        )

    review_due_reason = _review_due_reason(
        effective_level, reviewed_against, effective_platform
    )

    return PackCertification(
        pack_id=resolved_id,
        pack_name=pack.get("packName", resolved_id),
        pack_version=get_pack_version(pack_id),
        declared_level=declared_level,
        effective_level=effective_level,
        certifying_entity=entity,
        review_date=review_date,
        reviewed_against_platform_version=reviewed_against,
        platform_version=effective_platform,
        scope=dict(declaration.get("scope") or {}),
        signature_present=verification.present,
        signature_verified=verification.verified,
        signature_key_id=verification.key_id,
        signature_algorithm=verification.algorithm,
        downgrade_reason=downgrade_reason,
        downgrade_detail=downgrade_detail,
        review_due_reason=review_due_reason,
    )


def _review_due_reason(
    effective_level: str, reviewed_against: str, platform_version: str
) -> Optional[str]:
    """Why this certification is due for review, or ``None``.

    Only meaningful for a level that was actually reviewed — a community pack was
    never certified against any platform version, so it is never "due".

    MAJOR.MINOR granularity: a patch-level platform release does not change the
    capability surface a pack was reviewed against, so treating it as expiry would
    make the flag noise and train reviewers to ignore it.
    """
    if effective_level not in SIGNATURE_REQUIRED_LEVELS:
        return None
    reviewed = parse_version(reviewed_against)
    current = parse_version(platform_version)
    if reviewed is None or current is None:
        return REVIEW_DUE_UNDECLARED
    if current[:2] > reviewed[:2]:
        return REVIEW_DUE_PLATFORM_MOVED
    return None


# ── Selection-level helpers ───────────────────────────────────────────────────


def get_certification_level(
    pack_id: Optional[str] = None, *, platform_version: Optional[str] = None
) -> str:
    """The EFFECTIVE certification level of one pack — the value a policy reads."""
    return get_pack_certification(
        pack_id, platform_version=platform_version
    ).effective_level


def meets_minimum_level(level: str, minimum: str) -> bool:
    """True when ``level`` is at least as strong as ``minimum``.

    The primitive 2.0-C2 T4's org policy ("Certified only") is expressed with.
    An unrecognised level is treated as ``community`` — fail closed.
    """
    return LEVEL_RANK.get(
        str(level or "").strip().lower(), 0
    ) >= LEVEL_RANK.get(str(minimum or "").strip().lower(), 0)


def certify_pack_selection(
    pack_ids: Optional[Iterable[str]] = None,
    *,
    platform_version: Optional[str] = None,
) -> List[PackCertification]:
    """Certification verdicts for a whole (multi-)pack selection.

    Order-preserving and de-duplicated by RESOLVED pack id, mirroring
    ``pack_compatibility.check_pack_selection`` — an empty selection reports the
    DEFAULT pack, which is what such a run would actually execute.
    """
    selection: List[Optional[str]] = list(normalize_pack_ids(list(pack_ids or [])))
    if not selection:
        selection = [None]

    reports: List[PackCertification] = []
    seen: set = set()
    for pack_id in selection:
        report = get_pack_certification(
            pack_id, platform_version=platform_version
        )
        if report.pack_id in seen:
            continue
        seen.add(report.pack_id)
        reports.append(report)
    return reports


def certification_badge(
    pack_id: Optional[str] = None, *, platform_version: Optional[str] = None
) -> Dict[str, Any]:
    """The COMPACT display shape — 2.0-C2 T3 (AT-833).

    :meth:`PackCertification.to_dict` is the full audit shape (signature key ids,
    downgrade reasons, scope). That is the right payload for a certification API and
    the wrong one to staple onto every finding in a 200-item list, so surfacing gets
    this five-field projection instead.

    ``level`` is always the EFFECTIVE level — the badge a reader may act on. A
    declared claim that could not be verified is reported as ``community`` here too,
    with ``declaredLevel`` preserving what the pack asked for, because a surface must
    never display an unproved Certified claim as Certified (AC1 carried into AC2).
    """
    certification = get_pack_certification(
        pack_id, platform_version=platform_version
    )
    return {
        "packId": certification.pack_id,
        "level": certification.effective_level,
        "label": certification.label,
        "statusLabel": certification.status_label,
        "declaredLevel": certification.declared_level,
        "reviewDue": certification.review_due,
    }


def certification_badges(
    pack_ids: Optional[Iterable[str]] = None,
    *,
    platform_version: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """``{pack_id: badge}`` for a set of packs — resolved ONCE per surface.

    Every serve site that labels many rows (a findings list, the run-health packs
    panel, the pack selection list) calls this once and threads the map down, rather
    than verifying a signature per row.

    An unknown pack id resolves through ``get_pack()`` like everywhere else, so the
    map is keyed by the RESOLVED pack id.
    """
    badges: Dict[str, Dict[str, Any]] = {}
    for pack_id in normalize_pack_ids(list(pack_ids or [])) or _all_pack_ids():
        badge = certification_badge(pack_id, platform_version=platform_version)
        badges[badge["packId"]] = badge
    return badges


def _all_pack_ids() -> List[str]:
    """Every registered pack id — the default set when no selection is given."""
    from .pack_config import PACK_REGISTRY

    return list(PACK_REGISTRY)


def certification_summary(
    pack_ids: Optional[Iterable[str]] = None,
    *,
    platform_version: Optional[str] = None,
) -> Dict[str, Any]:
    """JSON-serialisable certification snapshot for a selection.

    Persisted alongside a run (as ``pack_compatibility``'s summary is) so a report
    states the level a pack held WHEN IT RAN, rather than re-deriving it later from
    a registry whose certification may have been re-issued or expired since.
    """
    reports = certify_pack_selection(pack_ids, platform_version=platform_version)
    return {
        "platformVersion": platform_version or get_platform_version(),
        "trustedKeyIds": trusted_key_ids(),
        "allVerified": all(
            not report.downgraded for report in reports
        ),
        "reviewDue": [
            report.pack_id for report in reports if report.review_due
        ],
        "packs": [report.to_dict() for report in reports],
    }


__all__ = [
    "ALGORITHM_ED25519",
    "CERTIFICATION_LEVELS",
    "CLOUDFULCRUM",
    "CLOUDFULCRUM_SIGNING_KEYS",
    "LEVEL_CERTIFIED",
    "LEVEL_COMMUNITY",
    "LEVEL_LABELS",
    "LEVEL_PARTNER",
    "LEVEL_RANK",
    "PackCertification",
    "PackCertificationError",
    "REASON_INVALID_LEVEL",
    "REASON_MISSING_METADATA",
    "REASON_SIGNATURE_BACKEND_UNAVAILABLE",
    "REASON_SIGNATURE_INVALID",
    "REASON_SIGNATURE_MALFORMED",
    "REASON_SIGNATURE_MISSING",
    "REASON_SIGNATURE_UNKNOWN_KEY",
    "REASON_SIGNATURE_UNSUPPORTED_ALGORITHM",
    "REVIEW_DUE_PLATFORM_MOVED",
    "REVIEW_DUE_UNDECLARED",
    "SIGNATURE_PAYLOAD_VERSION",
    "SIGNATURE_REQUIRED_LEVELS",
    "SignatureVerification",
    "TRUSTED_KEYS_ENV_VAR",
    "canonical_payload_bytes",
    "certification_payload",
    "certification_badge",
    "certification_badges",
    "certification_summary",
    "certify_pack_selection",
    "get_certification_level",
    "get_pack_certification",
    "meets_minimum_level",
    "set_trusted_signing_keys",
    "sign_certification",
    "trusted_key_ids",
    "trusted_signing_keys",
    "verify_certification_signature",
]
