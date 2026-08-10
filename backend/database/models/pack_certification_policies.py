"""Per-org pack certification activation policy.

2.0-C2 T4 (AT-834) — an org restricts which certification levels may be activated.

Design notes
------------
* **One row per org, and the ABSENCE of a row means "no restriction."** Provisioning
  this table changes no behaviour until an org opts in — the same discipline as
  ``pack_states``. There is no seed and no backfill.
* **No delete path.** Lifting a restriction WRITES ``community`` (the permissive
  floor) rather than removing the row, so "the floor was lowered on this date by this
  person" stays answerable. For a setting whose entire purpose is to keep uncertified
  packs out of a federal deployment, that is the property that matters.
* **The change history lives in ``audit_log``**, not in a sibling history table.
  Every policy write emits a ``pack_certification_policy_changed`` audit event, which
  is the org-wide immutable trail an auditor actually reads. ``pack_state_history``
  exists because run health surfaces per-pack transitions in the product; nothing
  surfaces a policy timeline, so a second table would be an unread duplicate.
* ``minimum_level`` holds a certification level, validated in
  ``app/pack_certification_policy.py`` against the three levels rather than by a CHECK
  constraint — the vocabulary lives in one place, and a DB constraint would have to be
  migrated in lockstep with it.
"""

CREATE_PACK_CERTIFICATION_POLICIES_TABLE = """
CREATE TABLE IF NOT EXISTS pack_certification_policies (
    org_id        VARCHAR(64)  PRIMARY KEY,
    minimum_level VARCHAR(16)  NOT NULL,
    revision      INTEGER      NOT NULL DEFAULT 0,
    reason        TEXT,
    updated_by    VARCHAR(128),
    created_at    TIMESTAMPTZ  NOT NULL,
    updated_at    TIMESTAMPTZ  NOT NULL
)
"""

ALL_PACK_CERTIFICATION_POLICY_DDL = (
    CREATE_PACK_CERTIFICATION_POLICIES_TABLE,
)

DROP_PACK_CERTIFICATION_POLICY_DDL = (
    "DROP TABLE IF EXISTS pack_certification_policies",
)
