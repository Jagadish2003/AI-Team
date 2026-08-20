"""Signed pack bundle — 2.0-C3 T4 (AT-839).

The distribution artifact: a pack project (manifest + fixtures + docs) packaged
into one file, with a per-file digest index and a signature over that index.

    my_pack.aiqpack           (a zip)
      pack.json
      fixtures/*.json
      README.md
      BUNDLE.json             the digest index — every file, its sha256, its size
      BUNDLE.sig              {keyId, algorithm, value} over BUNDLE.json's bytes

Why a digest index AND a signature
-----------------------------------
Signing the whole archive would tie the signature to zip encoding — compression
level, entry order, extra fields — none of which is content. Signing a canonical
digest index instead means the signature covers exactly the *content*, so
verification is a content comparison rather than a byte-for-byte archive
comparison, and a bundle repacked by a mirror still verifies. Any tampering —
editing a file, adding one, removing one — changes the index or contradicts it,
and both are caught (:func:`verify_bundle`).

Trust is explicit, and the default is to trust nobody
------------------------------------------------------
There are **no built-in publisher keys**. A deployment declares the publishers it
trusts in ``PACK_BUNDLE_TRUSTED_KEYS`` (public halves only); with none declared,
no bundle verifies and no partner pack can be installed. That is the correct
default for the boundary this feature sits on: installing a third-party pack is a
deliberate act of trust, and a platform that shipped a convenient default anchor
would be making that decision on the customer's behalf.

This mirrors ``pack_certification``'s posture (public keys only, private half
never ships) but deliberately uses a SEPARATE anchor set: a certification key
attests *we reviewed this pack*, a publisher key attests *this artifact is what I
built*. One key doing both jobs would let either claim be mistaken for the other.

Determinism
-----------
Building the same project twice produces byte-identical bytes: entries sorted,
fixed timestamps, fixed compression. A rebuild is therefore itself a verification.

Dependency-free of ``app``; the crypto backend is imported lazily so authoring
tooling that never verifies does not need it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .harness import FIXTURES_DIRNAME, PACK_MANIFEST_FILENAME
from .manifest import manifest_fingerprint, validate_manifest

logger = logging.getLogger(__name__)

#: Bundle format tag. Inside the signed index, so a future format cannot be
#: replayed against this one's rules.
BUNDLE_FORMAT = "agentiq-pack-bundle-v1"

BUNDLE_INDEX_FILENAME = "BUNDLE.json"
BUNDLE_SIGNATURE_FILENAME = "BUNDLE.sig"
BUNDLE_SUFFIX = ".aiqpack"

ALGORITHM_ED25519 = "ed25519"

#: Deployment-declared publisher trust anchors, ``{"keyId": "<base64 public key>"}``.
TRUSTED_KEYS_ENV_VAR = "PACK_BUNDLE_TRUSTED_KEYS"
#: Publisher signing key (base64 32-byte ed25519 seed). Packaging tooling ONLY —
#: never read by the running platform, never in a deployment or a repo.
SIGNING_KEY_ENV_VAR = "PACK_BUNDLE_SIGNING_KEY"

#: Files a bundle may carry beyond the manifest and fixtures. Anything else is
#: refused: a bundle is configuration, and an unexplained payload in an artifact
#: that crosses a customer boundary is exactly what this format exists to prevent.
ALLOWED_EXTRA_FILES = ("README.md", "CHANGELOG.md", "LICENSE", "LICENSE.md")

#: Hard cap on a single file and on the whole bundle, checked BEFORE extraction —
#: a decompression bomb must fail as a refusal, not as a disk-full outage.
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_FILES = 256

# Refusal reasons. Named so an installer reports WHICH check failed.
REASON_UNREADABLE = "bundle_unreadable"
REASON_INDEX_MISSING = "bundle_index_missing"
REASON_INDEX_MALFORMED = "bundle_index_malformed"
REASON_FORMAT_UNSUPPORTED = "bundle_format_unsupported"
REASON_CONTENT_MISMATCH = "bundle_content_mismatch"
REASON_UNEXPECTED_FILE = "bundle_unexpected_file"
REASON_MANIFEST_INVALID = "bundle_manifest_invalid"
REASON_SIGNATURE_MISSING = "bundle_signature_missing"
REASON_SIGNATURE_UNTRUSTED = "bundle_signature_untrusted"
REASON_SIGNATURE_INVALID = "bundle_signature_invalid"
REASON_SIGNATURE_UNSUPPORTED = "bundle_signature_unsupported_algorithm"
REASON_BACKEND_UNAVAILABLE = "bundle_signature_backend_unavailable"
REASON_TOO_LARGE = "bundle_too_large"


class BundleError(ValueError):
    """A bundle cannot be built from the given project."""


@dataclass(frozen=True)
class BundleFile:
    path: str
    sha256: str
    bytes_: int

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes_}


@dataclass(frozen=True)
class BundleVerification:
    """The verdict for one bundle. Never raised — an installer renders it."""

    ok: bool
    reason: str = ""
    detail: str = ""
    pack_id: str = ""
    pack_version: str = ""
    manifest_fingerprint: str = ""
    bundle_digest: str = ""
    key_id: str = ""
    files: Sequence[BundleFile] = ()
    manifest_document: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "packId": self.pack_id,
            "packVersion": self.pack_version,
            "manifestFingerprint": self.manifest_fingerprint,
            "bundleDigest": self.bundle_digest,
            "keyId": self.key_id,
            "files": [entry.to_dict() for entry in self.files],
        }


# ── Trust anchors ─────────────────────────────────────────────────────────────

_TRUSTED_KEYS_OVERRIDE: Optional[Dict[str, str]] = None


def trusted_publisher_keys() -> Dict[str, str]:
    """Trusted publisher public keys, ``{key_id: base64 raw ed25519 public key}``.

    Empty by default — see the module docstring on why there is no built-in anchor.
    A malformed env value is logged and ignored (never a silent partial trust map).
    """
    if _TRUSTED_KEYS_OVERRIDE is not None:
        return dict(_TRUSTED_KEYS_OVERRIDE)
    raw = os.getenv(TRUSTED_KEYS_ENV_VAR, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.error(
            "%s is not valid JSON; no publisher keys are trusted", TRUSTED_KEYS_ENV_VAR
        )
        return {}
    if not isinstance(parsed, dict):
        logger.error(
            "%s must be an object of {keyId: publicKey}; no publisher keys are "
            "trusted",
            TRUSTED_KEYS_ENV_VAR,
        )
        return {}
    return {
        str(key): str(value)
        for key, value in parsed.items()
        if str(key).strip() and isinstance(value, str) and value.strip()
    }


def validate_trusted_keys_config() -> None:
    """Report a malformed :data:`TRUSTED_KEYS_ENV_VAR` at STARTUP, not at install.

    :func:`trusted_publisher_keys` already logs and degrades to "trust nobody", so a
    typo was never silent — but it was only ever logged on the first bundle read,
    which is the moment a customer is trying to install. The operator who mistyped
    the variable is not watching then, and the failure they see is
    ``bundle_unverified`` on a bundle that is in fact correctly signed, which points
    at the wrong thing entirely.

    So the same parse runs unconditionally at startup, following
    ``model_gateway.validate_provider_config``'s precedent. It does NOT raise: a
    deployment that never installs an authored pack must not be prevented from
    booting by a variable it does not use, and refusing to start would be a worse
    failure than the one being reported. Trust-anchor COUNT is logged, never a key
    id or value.
    """
    if _TRUSTED_KEYS_OVERRIDE is not None:
        return
    raw = os.getenv(TRUSTED_KEYS_ENV_VAR, "").strip()
    if not raw:
        logger.info(
            "%s is not set; no pack publisher keys are trusted and signed-bundle "
            "installation will be refused with %s",
            TRUSTED_KEYS_ENV_VAR,
            REASON_SIGNATURE_UNTRUSTED,
        )
        return
    # trusted_publisher_keys() logs the specific defect (bad JSON / wrong shape).
    # Reading it here is what surfaces that log at startup.
    keys = trusted_publisher_keys()
    if not keys:
        logger.error(
            "%s is set but yielded no usable trust anchors; every signed-bundle "
            "installation will be refused with %s until it is corrected",
            TRUSTED_KEYS_ENV_VAR,
            REASON_SIGNATURE_UNTRUSTED,
        )
        return
    logger.info(
        "%s declares %d pack publisher trust anchor(s)", TRUSTED_KEYS_ENV_VAR, len(keys)
    )


def set_trusted_publisher_keys(keys: Optional[Mapping[str, str]]) -> None:
    """Test/offline injection seam; ``None`` restores environment resolution."""
    global _TRUSTED_KEYS_OVERRIDE
    _TRUSTED_KEYS_OVERRIDE = dict(keys) if keys is not None else None


# ── Index construction ────────────────────────────────────────────────────────


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_index_bytes(index: Mapping[str, Any]) -> bytes:
    """Canonical UTF-8 bytes of the digest index — the exact signed payload."""
    return json.dumps(
        index, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def collect_pack_files(directory: Any) -> List[Tuple[str, bytes]]:
    """The files a bundle carries, as ``(archive path, bytes)``, sorted.

    Exactly the manifest, the fixtures, and the allowed docs. An unexpected file is
    a build-time refusal rather than a silent omission, so an author never ships a
    bundle missing something they thought was in it.
    """
    pack_dir = Path(directory)
    manifest_path = pack_dir / PACK_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise BundleError(f"no {PACK_MANIFEST_FILENAME} in {pack_dir}")

    collected: List[Tuple[str, bytes]] = [
        (PACK_MANIFEST_FILENAME, manifest_path.read_bytes())
    ]
    fixtures = pack_dir / FIXTURES_DIRNAME
    if fixtures.is_dir():
        for path in sorted(fixtures.glob("*.json")):
            collected.append((f"{FIXTURES_DIRNAME}/{path.name}", path.read_bytes()))
    for name in ALLOWED_EXTRA_FILES:
        candidate = pack_dir / name
        if candidate.is_file():
            collected.append((name, candidate.read_bytes()))

    known = {entry[0] for entry in collected}
    for path in sorted(pack_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(pack_dir).as_posix()
        if relative in known or relative.startswith("__pycache__"):
            continue
        raise BundleError(
            f"unexpected file {relative!r}: a bundle carries {PACK_MANIFEST_FILENAME}, "
            f"{FIXTURES_DIRNAME}/*.json, and {', '.join(ALLOWED_EXTRA_FILES)} only"
        )

    for name, payload in collected:
        if len(payload) > MAX_FILE_BYTES:
            raise BundleError(
                f"{name} is {len(payload)} bytes, above the {MAX_FILE_BYTES}-byte cap"
            )
    return sorted(collected, key=lambda entry: entry[0])


def build_index(files: Sequence[Tuple[str, bytes]], *, publisher: str = "") -> Dict[str, Any]:
    """The digest index for a set of files. Deterministic; carries no timestamp.

    No build time is recorded, deliberately: it would make two builds of identical
    content differ, and "when was this packed" is not a property anyone verifies.
    """
    manifest_bytes = dict(files).get(PACK_MANIFEST_FILENAME, b"")
    document = json.loads(manifest_bytes.decode("utf-8"))
    validation = validate_manifest(document)
    if not validation.ok or validation.manifest is None:
        raise BundleError(
            "manifest is not valid, so it cannot be packaged: "
            + "; ".join(f"{e.path}: {e.message}" for e in validation.errors)
        )
    manifest = validation.manifest
    return {
        "bundleFormat": BUNDLE_FORMAT,
        "packId": manifest.pack_id,
        "packVersion": manifest.pack_version,
        "manifestFingerprint": manifest_fingerprint(manifest),
        "publisher": publisher or manifest.author.name,
        "files": [
            {"path": name, "sha256": _sha256(payload), "bytes": len(payload)}
            for name, payload in files
        ],
    }


def sign_index(index: Mapping[str, Any], *, signing_key: str, key_id: str) -> Dict[str, str]:
    """Sign a digest index with a base64 ed25519 seed. Packaging tooling only."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        seed = base64.b64decode(signing_key.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BundleError("signing key is not valid base64") from exc
    if len(seed) != 32:
        raise BundleError("signing key must be a 32-byte ed25519 seed")
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    signature = private_key.sign(canonical_index_bytes(index))
    return {
        "keyId": str(key_id or "").strip(),
        "algorithm": ALGORITHM_ED25519,
        "value": base64.b64encode(signature).decode("ascii"),
    }


def build_bundle(
    directory: Any,
    output_path: Any,
    *,
    signing_key: Optional[str] = None,
    key_id: str = "",
    publisher: str = "",
) -> BundleVerification:
    """Package a pack project into a signed bundle and return its verification.

    Returning the VERIFICATION (rather than a path) means packaging proves what it
    produced: the same check installation will run, run once at build time.
    """
    files = collect_pack_files(directory)
    index = build_index(files, publisher=publisher)
    key = signing_key if signing_key is not None else os.getenv(SIGNING_KEY_ENV_VAR, "")
    if not str(key or "").strip():
        raise BundleError(
            f"a bundle must be signed; supply a signing key (or set "
            f"{SIGNING_KEY_ENV_VAR}). An unsigned bundle cannot be installed."
        )
    signature = sign_index(index, signing_key=str(key), key_id=key_id)

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payloads = list(files) + [
        (BUNDLE_INDEX_FILENAME, canonical_index_bytes(index)),
        (
            BUNDLE_SIGNATURE_FILENAME,
            json.dumps(signature, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ),
    ]
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(payloads, key=lambda entry: entry[0]):
            # Fixed timestamp: the same content must always produce the same bytes.
            info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)
    return verify_bundle(target)


# ── Verification ──────────────────────────────────────────────────────────────


def _refuse(reason: str, detail: str, **extra: Any) -> BundleVerification:
    return BundleVerification(ok=False, reason=reason, detail=detail, **extra)


def _read_archive(source: Any) -> Tuple[Optional[Dict[str, bytes]], Optional[BundleVerification]]:
    """Read every member into memory, refusing anything oversized or unsafe."""
    try:
        if isinstance(source, (bytes, bytearray)):
            import io

            handle: Any = io.BytesIO(bytes(source))
            total = len(source)
        else:
            path = Path(source)
            total = path.stat().st_size
            handle = path
        if total > MAX_BUNDLE_BYTES:
            return None, _refuse(
                REASON_TOO_LARGE,
                f"bundle is {total} bytes, above the {MAX_BUNDLE_BYTES}-byte cap",
            )
        with zipfile.ZipFile(handle) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_BUNDLE_FILES:
                return None, _refuse(
                    REASON_TOO_LARGE,
                    f"bundle holds {len(infos)} files, above the "
                    f"{MAX_BUNDLE_FILES}-file cap",
                )
            uncompressed = sum(info.file_size for info in infos)
            if uncompressed > MAX_BUNDLE_BYTES:
                # Checked from the header BEFORE reading: a decompression bomb must
                # be a refusal, not a disk-full outage.
                return None, _refuse(
                    REASON_TOO_LARGE,
                    f"bundle expands to {uncompressed} bytes, above the "
                    f"{MAX_BUNDLE_BYTES}-byte cap",
                )
            contents: Dict[str, bytes] = {}
            for info in infos:
                name = info.filename
                if name.endswith("/"):
                    continue
                if name.startswith("/") or ".." in Path(name).parts:
                    # Zip-slip: a member that escapes the extraction root.
                    return None, _refuse(
                        REASON_UNEXPECTED_FILE,
                        f"bundle member {name!r} points outside the bundle",
                    )
                contents[name] = archive.read(info)
        return contents, None
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad. These are untrusted third-party bytes, and the
        # failure modes are open-ended — a truncated archive raises BadZipFile, a
        # corrupted deflate stream raises zlib.error, an unreadable path raises
        # OSError. Every one of them means the same thing to a caller ("this is
        # not a readable bundle"), and any of them escaping as an unhandled
        # exception would turn a refusal into a 500.
        return None, _refuse(REASON_UNREADABLE, f"bundle could not be read: {exc}")


def verify_bundle(source: Any, *, trusted_keys: Optional[Mapping[str, str]] = None) -> BundleVerification:
    """Verify a bundle's contents against its signed index. Never raises.

    Every failure path returns a named reason, because an installer must be able
    to tell an operator *which* check failed: an unreadable archive, a tampered
    file, an untrusted publisher, and an invalid signature are four different
    conversations.
    """
    contents, failure = _read_archive(source)
    if failure is not None:
        return failure
    assert contents is not None

    raw_index = contents.get(BUNDLE_INDEX_FILENAME)
    if raw_index is None:
        return _refuse(
            REASON_INDEX_MISSING,
            f"bundle carries no {BUNDLE_INDEX_FILENAME}, so its contents cannot be "
            f"verified",
        )
    try:
        index = json.loads(raw_index.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _refuse(REASON_INDEX_MALFORMED, f"{BUNDLE_INDEX_FILENAME} is not valid JSON: {exc}")
    if not isinstance(index, dict) or index.get("bundleFormat") != BUNDLE_FORMAT:
        return _refuse(
            REASON_FORMAT_UNSUPPORTED,
            f"unsupported bundle format {index.get('bundleFormat')!r}; this platform "
            f"reads {BUNDLE_FORMAT!r}",
        )

    declared = index.get("files")
    if not isinstance(declared, list) or not declared:
        return _refuse(REASON_INDEX_MALFORMED, "the bundle index lists no files")

    files: List[BundleFile] = []
    indexed_names = set()
    for entry in declared:
        if not isinstance(entry, dict):
            return _refuse(REASON_INDEX_MALFORMED, "a file entry is not an object")
        name = str(entry.get("path") or "")
        digest = str(entry.get("sha256") or "")
        payload = contents.get(name)
        if payload is None:
            return _refuse(
                REASON_CONTENT_MISMATCH, f"indexed file {name!r} is missing from the bundle"
            )
        actual = _sha256(payload)
        if actual != digest:
            return _refuse(
                REASON_CONTENT_MISMATCH,
                f"{name} does not match its signed digest (the bundle has been altered)",
            )
        indexed_names.add(name)
        files.append(BundleFile(path=name, sha256=actual, bytes_=len(payload)))

    extra = set(contents) - indexed_names - {BUNDLE_INDEX_FILENAME, BUNDLE_SIGNATURE_FILENAME}
    if extra:
        # An added file changes nothing in the index, so the digests still match —
        # this is the check that catches it.
        return _refuse(
            REASON_UNEXPECTED_FILE,
            f"bundle carries file(s) not covered by its signature: "
            f"{', '.join(sorted(extra))}",
        )

    manifest_document: Optional[Mapping[str, Any]] = None
    try:
        manifest_document = json.loads(contents[PACK_MANIFEST_FILENAME].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
        return _refuse(
            REASON_MANIFEST_INVALID, f"bundle carries no readable {PACK_MANIFEST_FILENAME}"
        )

    common = {
        "pack_id": str(index.get("packId") or ""),
        "pack_version": str(index.get("packVersion") or ""),
        "manifest_fingerprint": str(index.get("manifestFingerprint") or ""),
        "bundle_digest": _sha256(raw_index),
        "files": tuple(files),
        "manifest_document": manifest_document,
    }

    raw_signature = contents.get(BUNDLE_SIGNATURE_FILENAME)
    if raw_signature is None:
        return _refuse(
            REASON_SIGNATURE_MISSING,
            "bundle is unsigned; an unsigned bundle cannot be installed",
            **common,
        )
    try:
        signature = json.loads(raw_signature.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _refuse(REASON_SIGNATURE_MISSING, f"signature is not readable: {exc}", **common)
    if not isinstance(signature, dict):
        return _refuse(REASON_SIGNATURE_MISSING, "signature is not an object", **common)

    key_id = str(signature.get("keyId") or "")
    algorithm = str(signature.get("algorithm") or "").lower()
    common["key_id"] = key_id
    if algorithm and algorithm != ALGORITHM_ED25519:
        return _refuse(
            REASON_SIGNATURE_UNSUPPORTED,
            f"unsupported signature algorithm {algorithm!r}",
            **common,
        )

    anchors = dict(trusted_keys) if trusted_keys is not None else trusted_publisher_keys()
    public_key = anchors.get(key_id)
    if not public_key:
        return _refuse(
            REASON_SIGNATURE_UNTRUSTED,
            (
                f"publisher key {key_id!r} is not trusted by this deployment; add it "
                f"to {TRUSTED_KEYS_ENV_VAR} to trust bundles from this publisher"
            ),
            **common,
        )

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception as exc:  # noqa: BLE001 - crypto backend absent
        # Never "assume valid": an environment that cannot verify has not verified.
        return _refuse(
            REASON_BACKEND_UNAVAILABLE,
            f"no signature verification backend available ({exc})",
            **common,
        )
    try:
        verifier = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key, validate=True)
        )
        verifier.verify(
            base64.b64decode(str(signature.get("value") or ""), validate=True),
            raw_index,
        )
    except (binascii.Error, ValueError):
        return _refuse(REASON_SIGNATURE_INVALID, "signature or key is malformed", **common)
    except Exception:  # noqa: BLE001 - cryptography raises InvalidSignature
        return _refuse(
            REASON_SIGNATURE_INVALID,
            "signature does not match the bundle's contents",
            **common,
        )

    return BundleVerification(ok=True, **common)


def extract_bundle(source: Any, target_dir: Any) -> Path:
    """Extract a VERIFIED bundle's pack files into ``target_dir``.

    Verifies first and refuses an unverified bundle: extraction is the step that
    puts partner-supplied bytes on disk, so it must never run on content whose
    signature has not been checked.
    """
    verification = verify_bundle(source)
    if not verification.ok:
        raise BundleError(f"{verification.reason}: {verification.detail}")

    contents, failure = _read_archive(source)
    if failure is not None or contents is None:  # pragma: no cover - re-read guard
        raise BundleError("bundle became unreadable between verification and extraction")

    root = Path(target_dir)
    root.mkdir(parents=True, exist_ok=True)
    for entry in verification.files:
        destination = root / entry.path
        # Belt and braces over the zip-slip check in _read_archive.
        if not str(destination.resolve()).startswith(str(root.resolve())):
            raise BundleError(f"bundle member {entry.path!r} escapes the target directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents[entry.path])
    return root


__all__ = [
    "ALGORITHM_ED25519",
    "ALLOWED_EXTRA_FILES",
    "BUNDLE_FORMAT",
    "BUNDLE_INDEX_FILENAME",
    "BUNDLE_SIGNATURE_FILENAME",
    "BUNDLE_SUFFIX",
    "MAX_BUNDLE_BYTES",
    "REASON_CONTENT_MISMATCH",
    "REASON_SIGNATURE_INVALID",
    "REASON_SIGNATURE_MISSING",
    "REASON_SIGNATURE_UNTRUSTED",
    "REASON_UNEXPECTED_FILE",
    "SIGNING_KEY_ENV_VAR",
    "TRUSTED_KEYS_ENV_VAR",
    "BundleError",
    "BundleFile",
    "BundleVerification",
    "build_bundle",
    "build_index",
    "canonical_index_bytes",
    "collect_pack_files",
    "extract_bundle",
    "set_trusted_publisher_keys",
    "sign_index",
    "trusted_publisher_keys",
    "validate_trusted_keys_config",
    "verify_bundle",
]
