"""2.0-C3 T4 (AT-839) — the signed pack bundle, DB-free.

Sub-task scope (packaging half): *a signed pack bundle.*

Parent-story criterion discharged here:

  * AC4 — packaging produces a signed bundle; a TAMPERED bundle fails
    installation.

Tampering has more than one shape, and a format that catches only the obvious one
is not integrity-protected. Each is seeded separately: editing a file, adding a
file the index does not cover, removing an indexed file, swapping the signature
for one made with another key, and presenting no signature at all.
"""
from __future__ import annotations

import base64
import json
import os
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from discovery.packs.sdk import bundle as bundle_module  # noqa: E402
from discovery.packs.sdk.bundle import (  # noqa: E402
    BUNDLE_INDEX_FILENAME,
    BUNDLE_SIGNATURE_FILENAME,
    MAX_BUNDLE_FILES,
    REASON_CONTENT_MISMATCH,
    REASON_SIGNATURE_INVALID,
    REASON_SIGNATURE_MISSING,
    REASON_SIGNATURE_UNTRUSTED,
    REASON_UNEXPECTED_FILE,
    BundleError,
    build_bundle,
    extract_bundle,
    set_trusted_publisher_keys,
    trusted_publisher_keys,
    verify_bundle,
)
from discovery.packs.sdk.scaffold import scaffold_pack  # noqa: E402


def make_keypair() -> tuple:
    private = Ed25519PrivateKey.generate()
    seed = base64.b64encode(
        private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode()
    public = base64.b64encode(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    return seed, public


@pytest.fixture()
def publisher():
    seed, public = make_keypair()
    set_trusted_publisher_keys({"acme-2026": public})
    yield {"seed": seed, "public": public, "key_id": "acme-2026"}
    set_trusted_publisher_keys(None)


@pytest.fixture()
def pack_dir(tmp_path):
    scaffold_pack(
        tmp_path / "pack",
        pack_id="acme_service_desk",
        author_name="Acme Ltd",
        author_contact="packs@acme.test",
    )
    return tmp_path / "pack"


@pytest.fixture()
def built(tmp_path, pack_dir, publisher):
    path = tmp_path / "acme.aiqpack"
    verification = build_bundle(
        pack_dir, path, signing_key=publisher["seed"], key_id=publisher["key_id"]
    )
    assert verification.ok, verification.detail
    return path


def repack(source: Path, target: Path, mutate) -> Path:
    """Rebuild an archive with mutated contents — the attacker's move."""
    with zipfile.ZipFile(source) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    mutate(contents)
    with zipfile.ZipFile(target, "w") as archive:
        for name, payload in contents.items():
            archive.writestr(name, payload)
    return target


# ── Building ──────────────────────────────────────────────────────────────────


def test_a_built_bundle_verifies(built):
    verification = verify_bundle(built)
    assert verification.ok
    assert verification.pack_id == "acme_service_desk"
    assert verification.pack_version == "0.1.0"
    assert verification.key_id == "acme-2026"
    assert {entry.path for entry in verification.files} >= {"pack.json", "README.md"}


def test_building_is_deterministic(tmp_path, pack_dir, publisher):
    first = build_bundle(
        pack_dir, tmp_path / "a.aiqpack", signing_key=publisher["seed"], key_id="acme-2026"
    )
    second = build_bundle(
        pack_dir, tmp_path / "b.aiqpack", signing_key=publisher["seed"], key_id="acme-2026"
    )
    assert first.bundle_digest == second.bundle_digest
    assert (tmp_path / "a.aiqpack").read_bytes() == (tmp_path / "b.aiqpack").read_bytes()


def test_an_unsigned_bundle_cannot_be_built(tmp_path, pack_dir, monkeypatch):
    monkeypatch.delenv("PACK_BUNDLE_SIGNING_KEY", raising=False)
    with pytest.raises(BundleError) as excinfo:
        build_bundle(pack_dir, tmp_path / "unsigned.aiqpack")
    assert "must be signed" in str(excinfo.value)


def test_an_invalid_manifest_cannot_be_packaged(tmp_path, pack_dir, publisher):
    document = json.loads((pack_dir / "pack.json").read_text("utf-8"))
    document["detectors"][0]["primitive"] = "telepathy"
    (pack_dir / "pack.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(BundleError) as excinfo:
        build_bundle(
            pack_dir, tmp_path / "x.aiqpack", signing_key=publisher["seed"], key_id="k"
        )
    assert "telepathy" in str(excinfo.value)


def test_an_unexpected_file_in_the_project_is_refused(tmp_path, pack_dir, publisher):
    """A bundle carries the manifest, fixtures, and docs — nothing else."""
    (pack_dir / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(BundleError) as excinfo:
        build_bundle(
            pack_dir, tmp_path / "x.aiqpack", signing_key=publisher["seed"], key_id="k"
        )
    assert "install.sh" in str(excinfo.value)


# ── Tampering (AC4) ───────────────────────────────────────────────────────────


def test_editing_a_file_fails_verification(tmp_path, built):
    def mutate(contents):
        contents["pack.json"] = contents["pack.json"].replace(
            b'"min_occurrences": 4', b'"min_occurrences": 1'
        )

    tampered = repack(built, tmp_path / "tampered.aiqpack", mutate)
    verification = verify_bundle(tampered)
    assert not verification.ok
    assert verification.reason == REASON_CONTENT_MISMATCH


def test_adding_a_file_fails_verification(tmp_path, built):
    """The added file is not in the index, so every digest still matches — this is
    the check that catches it."""

    def mutate(contents):
        contents["fixtures/99_smuggled.json"] = b"{}"

    tampered = repack(built, tmp_path / "added.aiqpack", mutate)
    verification = verify_bundle(tampered)
    assert not verification.ok
    assert verification.reason == REASON_UNEXPECTED_FILE


def test_removing_an_indexed_file_fails_verification(tmp_path, built):
    def mutate(contents):
        contents.pop("fixtures/01_recurring_work_fires.json")

    tampered = repack(built, tmp_path / "removed.aiqpack", mutate)
    verification = verify_bundle(tampered)
    assert not verification.ok
    assert verification.reason == REASON_CONTENT_MISMATCH


def test_a_signature_from_another_key_fails_verification(tmp_path, built, publisher):
    """Re-signing tampered content with a key the deployment does not trust must
    not rescue it."""
    other_seed, _other_public = make_keypair()

    def mutate(contents):
        contents["pack.json"] = contents["pack.json"].replace(b"0.1.0", b"9.9.9")
        index = contents[BUNDLE_INDEX_FILENAME]
        from discovery.packs.sdk.bundle import sign_index

        # Re-index and re-sign so the digests are internally consistent.
        import hashlib

        parsed = json.loads(index)
        for entry in parsed["files"]:
            entry["sha256"] = hashlib.sha256(contents[entry["path"]]).hexdigest()
            entry["bytes"] = len(contents[entry["path"]])
        contents[BUNDLE_INDEX_FILENAME] = bundle_module.canonical_index_bytes(parsed)
        contents[BUNDLE_SIGNATURE_FILENAME] = json.dumps(
            sign_index(parsed, signing_key=other_seed, key_id="attacker-key")
        ).encode()

    tampered = repack(built, tmp_path / "resigned.aiqpack", mutate)
    verification = verify_bundle(tampered)
    assert not verification.ok
    assert verification.reason == REASON_SIGNATURE_UNTRUSTED


def test_a_signature_that_does_not_match_fails_verification(tmp_path, built, publisher):
    """Same trusted key id, but the signature covers different content."""
    other_seed, _ = make_keypair()

    def mutate(contents):
        from discovery.packs.sdk.bundle import sign_index

        parsed = json.loads(contents[BUNDLE_INDEX_FILENAME])
        contents[BUNDLE_SIGNATURE_FILENAME] = json.dumps(
            sign_index(parsed, signing_key=other_seed, key_id=publisher["key_id"])
        ).encode()

    tampered = repack(built, tmp_path / "badsig.aiqpack", mutate)
    verification = verify_bundle(tampered)
    assert not verification.ok
    assert verification.reason == REASON_SIGNATURE_INVALID


def test_a_stripped_signature_fails_verification(tmp_path, built):
    def mutate(contents):
        contents.pop(BUNDLE_SIGNATURE_FILENAME)

    tampered = repack(built, tmp_path / "unsigned.aiqpack", mutate)
    verification = verify_bundle(tampered)
    assert not verification.ok
    assert verification.reason == REASON_SIGNATURE_MISSING


def test_an_untrusted_publisher_is_refused(built):
    """The default is to trust nobody — installing third-party code is a deliberate
    act of trust, not a default."""
    set_trusted_publisher_keys({})
    try:
        verification = verify_bundle(built)
    finally:
        set_trusted_publisher_keys(None)
    assert not verification.ok
    assert verification.reason == REASON_SIGNATURE_UNTRUSTED


def test_no_publisher_key_is_trusted_by_default(monkeypatch):
    set_trusted_publisher_keys(None)
    monkeypatch.delenv("PACK_BUNDLE_TRUSTED_KEYS", raising=False)
    assert trusted_publisher_keys() == {}


def test_a_malformed_trust_map_trusts_nobody(monkeypatch):
    set_trusted_publisher_keys(None)
    monkeypatch.setenv("PACK_BUNDLE_TRUSTED_KEYS", "not json")
    assert trusted_publisher_keys() == {}


def test_a_non_archive_is_refused(tmp_path):
    junk = tmp_path / "junk.aiqpack"
    junk.write_bytes(b"definitely not a zip")
    assert verify_bundle(junk).reason == "bundle_unreadable"


def test_a_bundle_with_too_many_members_is_refused(tmp_path):
    crowded = tmp_path / "crowded.aiqpack"
    with zipfile.ZipFile(crowded, "w") as archive:
        for index in range(MAX_BUNDLE_FILES + 2):
            archive.writestr(f"f{index}.json", b"{}")
    assert verify_bundle(crowded).reason == "bundle_too_large"


def test_a_path_traversal_member_is_refused(tmp_path):
    """Zip-slip: a member that would escape the extraction root."""
    evil = tmp_path / "evil.aiqpack"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../escaped.json", b"{}")
    assert verify_bundle(evil).reason == REASON_UNEXPECTED_FILE


# ── Extraction ────────────────────────────────────────────────────────────────


def test_extraction_writes_the_pack_files(tmp_path, built):
    target = tmp_path / "extracted"
    extract_bundle(built, target)
    assert (target / "pack.json").is_file()
    assert list((target / "fixtures").glob("*.json"))


def test_extraction_refuses_an_unverified_bundle(tmp_path, built):
    """Extraction puts partner bytes on disk, so it must never run unverified."""

    def mutate(contents):
        contents["pack.json"] = contents["pack.json"].replace(b"0.1.0", b"6.6.6")

    tampered = repack(built, tmp_path / "bad.aiqpack", mutate)
    with pytest.raises(BundleError):
        extract_bundle(tampered, tmp_path / "out")
    assert not (tmp_path / "out" / "pack.json").exists()
