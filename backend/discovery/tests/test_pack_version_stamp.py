"""R16-B1 T5 — pack id + pack version stamped on every opportunity instance.

AC6 (the part owned by this task): each opportunity_instance records its run id,
pack id, AND pack version. Pack governance (1.9) needs this version history; it
is unreconstructable if not captured when the opportunity is created.

These run the OFFLINE discovery runner (no live credentials or database needed —
graph persistence is non-blocking) and assert the stamp lands on the raw runner
opportunities, on the STORED Track A opportunity instances, and on the run
metadata, and that get_pack_version() resolves a version for every pack.
"""
from __future__ import annotations

import os

import pytest

os.environ["INGEST_MODE"] = "offline"

from discovery.packs.pack_config import (  # noqa: E402
    DEFAULT_PACK_VERSION,
    PACK_REGISTRY,
    get_pack_version,
    list_packs,
)


# ── pack_config: every pack is versioned, accessor resolves + falls back ──────


def test_every_registered_pack_declares_a_version():
    for pid in list_packs():
        assert PACK_REGISTRY[pid].get("packVersion"), f"{pid} is missing packVersion"


def test_get_pack_version_returns_declared_version():
    assert get_pack_version("ncino") == PACK_REGISTRY["ncino"]["packVersion"]
    assert (
        get_pack_version("sqlserver_opsignal")
        == PACK_REGISTRY["sqlserver_opsignal"]["packVersion"]
    )


def test_get_pack_version_unknown_pack_falls_back_to_default():
    assert get_pack_version("no-such-pack") == DEFAULT_PACK_VERSION


def test_get_pack_version_none_uses_default_pack():
    assert get_pack_version(None) == get_pack_version("service_cloud")


# ── AC6: raw runner opportunities + run payload carry pack id + version ───────


@pytest.fixture(scope="module")
def ncino_payload():
    from discovery.runner import run

    return run(mode="offline", run_id="test-pack-stamp-ncino", pack="ncino")


def test_runner_opps_carry_pack_id_and_version(ncino_payload):
    opps = ncino_payload["opportunities"]
    assert opps, "offline ncino run should produce opportunities"
    for o in opps:
        assert o["packId"] == "ncino"
        assert o["packVersion"] == get_pack_version("ncino")


def test_run_payload_carries_pack_version(ncino_payload):
    assert ncino_payload["packId"] == "ncino"
    assert ncino_payload["packVersion"] == get_pack_version("ncino")


def test_default_pack_is_stamped_when_unspecified():
    from discovery.runner import run

    payload = run(mode="offline", run_id="test-pack-stamp-default", pack=None)
    for o in payload["opportunities"]:
        assert o["packId"] == "service_cloud"
        assert o["packVersion"] == get_pack_version("service_cloud")


# ── AC6: the STORED (Track A) opportunity instance carries pack id + version ──


def test_stored_opportunity_instance_is_stamped(ncino_payload):
    from discovery.track_a_adapter import export_track_a_seed

    seed = export_track_a_seed(ncino_payload)
    stored = seed["opportunities"]
    assert stored, "export should produce stored opportunity instances"
    for o in stored:
        # The persisted instance (served by GET /api/runs/{id}/opportunities)
        # must itself carry the pack id + version — not be re-derived later.
        assert o["packId"] == "ncino"
        assert o["packVersion"] == get_pack_version("ncino")


def test_run_meta_records_pack_id_and_version(ncino_payload):
    from discovery.track_a_adapter import export_track_a_seed

    run_meta = export_track_a_seed(ncino_payload)["run_meta"]
    assert run_meta["packId"] == "ncino"
    assert run_meta["packVersion"] == get_pack_version("ncino")
