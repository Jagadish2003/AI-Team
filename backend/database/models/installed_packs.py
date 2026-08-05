"""Installed authored packs — 2.0-C3 T4 (AT-839).

The per-org registry of partner packs installed from a signed bundle.

Design notes
------------
* **One row per (org, pack).** Re-installing the same pack id is an upgrade: the
  row is updated and ``revision`` increments, so "which version is installed and
  how many times has it changed" stays answerable without a second table.
* **No delete path.** Withdrawing a pack writes ``status = 'inactive'``; the
  record, its manifest, and its bundle provenance stay. That is what keeps
  "which pack produced this historical finding, and where did that pack come
  from" answerable after the pack is out of service — the 2.0-C1 never-delete
  discipline applied to the installed registry.

  It is deliberately NOT in ``app/history_retention.PROTECTED_TABLES``: this table
  is current configuration (which packs an org has), not a record of what the
  platform found. Findings, evidence, and run records are what that set protects,
  and none of them live here.
* ``manifest`` stores the validated, normalised manifest document — the exact one
  the gates were run against, not a re-read of the bundle. An installed pack's
  behaviour must not change because a file on disk changed.
* ``bundle_digest`` and ``signing_key_id`` are the provenance of the artifact:
  which bytes were installed, and which publisher key vouched for them.
* ``status`` and ``requested_level`` are validated in ``app/pack_installation.py``
  rather than by CHECK constraints — the vocabulary lives in one place, and a DB
  constraint would have to be migrated in lockstep with it.
"""

CREATE_INSTALLED_PACKS_TABLE = """
CREATE TABLE IF NOT EXISTS installed_packs (
    org_id               VARCHAR(64)  NOT NULL,
    pack_id              VARCHAR(128) NOT NULL,
    pack_version         VARCHAR(32)  NOT NULL,
    status               VARCHAR(16)  NOT NULL,
    manifest             JSONB        NOT NULL,
    manifest_fingerprint VARCHAR(64)  NOT NULL,
    bundle_digest        VARCHAR(64)  NOT NULL,
    publisher            VARCHAR(256),
    signing_key_id       VARCHAR(128),
    requested_level      VARCHAR(16)  NOT NULL DEFAULT 'community',
    revision             INTEGER      NOT NULL DEFAULT 1,
    installed_by         VARCHAR(128),
    created_at           TIMESTAMPTZ  NOT NULL,
    updated_at           TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (org_id, pack_id)
)
"""

CREATE_INSTALLED_PACKS_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_installed_packs_org_status
    ON installed_packs (org_id, status)
"""

ALL_INSTALLED_PACK_DDL = (
    CREATE_INSTALLED_PACKS_TABLE,
    CREATE_INSTALLED_PACKS_STATUS_INDEX,
)

DROP_INSTALLED_PACK_DDL = (
    "DROP INDEX IF EXISTS idx_installed_packs_org_status",
    "DROP TABLE IF EXISTS installed_packs",
)
