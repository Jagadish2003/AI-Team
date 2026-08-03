"""Persisted, append-only pack certification review trail.

2.0-C2 T2 (AT-832) — checklist-driven certification review.

One table, unlike the pack-lifecycle pair: a review has no "current state" that
differs from its latest record, so a second convenience table would be a
denormalisation with nothing to gain. ``ORDER BY revision DESC LIMIT 1`` is the
latest review.

Design notes
------------
* **Append-only.** ``app/pack_certification_review.py`` exposes no update and no
  delete; re-reviewing writes the next revision. The table is listed in
  ``app.history_retention.PROTECTED_TABLES``, so migration 0034 also REVOKEs
  DELETE/TRUNCATE on it — the audit trail is enforced at the data layer, not just
  by convention (2.0-C1 AC4's mechanism, reused).
* **Org-scoped.** Every key and query includes ``org_id``.
* **The checklist lives in ``criteria`` as JSONB**, mirroring this codebase's
  ``{id, payload}`` convention. Verdicts are always read with their review and
  never queried across reviews, so a child table would buy nothing.
* ``platform_version`` and ``pack_version`` are recorded AS REVIEWED. They must
  never be re-derived from the registry later — the point of the record is what
  was true at review time.
* A review does not grant a badge; only the AT-831 signature does. Nothing in
  this schema is read by the verification path.
"""

CREATE_PACK_CERTIFICATION_REVIEWS_TABLE = """
CREATE TABLE IF NOT EXISTS pack_certification_reviews (
    id               VARCHAR(64)  PRIMARY KEY,
    org_id           VARCHAR(64)  NOT NULL,
    pack_id          VARCHAR(64)  NOT NULL,
    pack_version     VARCHAR(32)  NOT NULL,
    revision         INTEGER      NOT NULL,
    reviewer_id      VARCHAR(128) NOT NULL,
    reviewer_name    VARCHAR(256),
    reviewed_at      TIMESTAMPTZ  NOT NULL,
    platform_version VARCHAR(32)  NOT NULL,
    proposed_level   VARCHAR(16)  NOT NULL,
    decision         VARCHAR(16)  NOT NULL,
    criteria         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    scope_summary    TEXT         NOT NULL DEFAULT '',
    notes            TEXT,
    UNIQUE (org_id, pack_id, revision)
)
"""

# The trail read: newest-first reviews for one pack.
CREATE_PACK_CERTIFICATION_REVIEWS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_pack_certification_reviews_org_pack
    ON pack_certification_reviews (org_id, pack_id, revision DESC)
"""

ALL_PACK_CERTIFICATION_REVIEW_DDL = (
    CREATE_PACK_CERTIFICATION_REVIEWS_TABLE,
    CREATE_PACK_CERTIFICATION_REVIEWS_INDEX,
)

DROP_PACK_CERTIFICATION_REVIEW_DDL = (
    "DROP INDEX IF EXISTS idx_pack_certification_reviews_org_pack",
    "DROP TABLE IF EXISTS pack_certification_reviews",
)
