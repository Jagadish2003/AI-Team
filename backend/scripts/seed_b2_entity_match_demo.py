"""Seed a reproducible 2.0-B2 cross-source entity match for a demo/review.

Why this exists
---------------
The Entity Matches page (``/entity-matches``) shows the cross-source identity
PROPOSALS the ranked resolution engine (:mod:`app.cross_source_resolution`)
refuses to auto-merge — the tier-3 ``name_similarity`` decisions a human must
confirm or reject. A proposal only exists when the org's graph actually holds a
cross-source identity candidate: two entities from DIFFERENT source systems,
of a scannable type, with the SAME normalised name AND a corroborating observed
relationship (each has an observed edge to a common third entity).

The shipped offline fixtures deliberately contain no such overlap — every
fixture entity is unique to its source — so an offline discovery produces zero
proposals and the page correctly shows "none". That is the feature working as
designed, not a bug; but it leaves nobody a reproducible way to SEE a match.

This script creates exactly one genuine, review-worthy candidate, per the
engine's real rules, so the page shows a real tier-3 proposal:

    Salesforce  system "Payments Platform"  ─depends_on─┐
                                                        ├─►  "Payments Ledger DB"
    ServiceNow  system "Payments Platform"  ─depends_on─┘   (shared neighbour =
                                                             corroboration)

Both sides share the exact canonical name ``payments platform`` and a shared
observed neighbour, so the engine PROPOSES the pair (tier 3) — it never merges
it (``name_similarity`` is excluded from ``AUTO_MERGE_TIERS`` by construction).

It is deliberately NOT a fuzzy/loosened matcher and it changes no engine
default: it only supplies the org with honest data the standing engine then
evaluates under its own unchanged rules.

Idempotent: entity/relationship ids are derived deterministically from the org,
so re-running replaces the seeded rows rather than duplicating them. Only the
seeded ids are ever touched; nothing else in the org is modified.

Usage
-----
    python -m scripts.seed_b2_entity_match_demo                 # org "default"
    python -m scripts.seed_b2_entity_match_demo --org <org_id>  # a specific org
    python -m scripts.seed_b2_entity_match_demo --org a --org b # several orgs

Run it against the org your session is scoped to (the raw dev token resolves to
"default"; a logged-in session uses that member's org). Then open the Entity
Matches page for that org — one pending proposal will be listed.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Make the backend package importable whether run as a module or a file.
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(_BACKEND / ".env"))

from app import db  # noqa: E402
from app.entity_match_proposals import (  # noqa: E402
    list_proposals,
    scan_for_proposals,
    status_counts,
)

#: Stable namespace so ids are reproducible across runs (idempotency).
_NS = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000000")
_SEED_RUN_ID = "run_seed_b2_entity_match_demo"

#: The cross-source identity to seed. Same canonical name, two source systems,
#: of a scannable type — the tier-3 candidate. Plus a shared neighbour that both
#: sides observe, which is the corroborating relationship the tier requires.
_ENTITY_TYPE = "system"
_MATCH_DISPLAY = "Payments Platform"
_MATCH_CANON = "payments platform"
_NEIGHBOUR_TYPE = "system"
_NEIGHBOUR_DISPLAY = "Payments Ledger DB"
_NEIGHBOUR_CANON = "payments ledger db"

# (source_system, source_record_id) for each side of the match.
_SIDES = (
    ("salesforce", "seed-b2-sf-payments-platform"),
    ("servicenow", "seed-b2-sn-payments-platform"),
)
_NEIGHBOUR_SIDE = ("servicenow", "seed-b2-sn-payments-ledger-db")


def _eid(org: str, source: str, record_id: str) -> str:
    return str(uuid.uuid5(_NS, f"{org}|{source}|{record_id}"))


def _rid(org: str, from_id: str, to_id: str) -> str:
    return str(uuid.uuid5(_NS, f"{org}|rel|{from_id}->{to_id}"))


def _entity_metadata(source: str, record_id: str, now_iso: str) -> str:
    return json.dumps(
        {
            "seeded": True,
            "seeded_for": "2.0-B2 entity-match demo",
            "evidence_pointer": {
                "source_system": source,
                "source_artifact": record_id,
                "source_timestamp": now_iso,
                "origin": "observed",
                "source_artifact_type": "record_id",
                "confidence": 1.0,
            },
        }
    )


def _upsert_entity(cur, org, eid, etype, canon, display, source, record_id, now, now_iso):
    cur.execute("DELETE FROM entities WHERE id = %s", (eid,))
    cur.execute(
        """
        INSERT INTO entities (
            id, org_id, entity_type, canonical_name, display_name, source_system,
            source_record_id, resolution_confidence, resolution_status,
            first_seen_run_id, last_seen_run_id, run_count, metadata,
            created_at, updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            eid, org, etype, canon, display, source, record_id, 1.0, "resolved",
            _SEED_RUN_ID, _SEED_RUN_ID, 1, _entity_metadata(source, record_id, now_iso),
            now, now,
        ),
    )


def _upsert_relationship(cur, org, from_id, to_id, now):
    rid = _rid(org, from_id, to_id)
    cur.execute("DELETE FROM entity_relationships WHERE id = %s", (rid,))
    cur.execute(
        """
        INSERT INTO entity_relationships (
            id, org_id, from_entity_id, to_entity_id, relationship_type,
            confidence, inferred, evidence, first_seen_run_id, last_seen_run_id,
            run_count, created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            rid, org, from_id, to_id, "depends_on", 0.9, False,
            json.dumps({"seeded": True}), _SEED_RUN_ID, _SEED_RUN_ID, 1, now,
        ),
    )


def seed_org(org: str) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = datetime.now(timezone.utc).isoformat()

    neighbour_id = _eid(org, *_NEIGHBOUR_SIDE)
    side_ids = []

    con = db.connect()
    try:
        cur = con.cursor()
        # Shared neighbour both sides depend on (the corroborating third entity).
        _upsert_entity(
            cur, org, neighbour_id, _NEIGHBOUR_TYPE, _NEIGHBOUR_CANON,
            _NEIGHBOUR_DISPLAY, _NEIGHBOUR_SIDE[0], _NEIGHBOUR_SIDE[1], now, now_iso,
        )
        # The two cross-source sides of the match.
        for source, record_id in _SIDES:
            eid = _eid(org, source, record_id)
            side_ids.append(eid)
            _upsert_entity(
                cur, org, eid, _ENTITY_TYPE, _MATCH_CANON, _MATCH_DISPLAY,
                source, record_id, now, now_iso,
            )
            _upsert_relationship(cur, org, eid, neighbour_id, now)
        con.commit()
    finally:
        con.close()

    # Run the ranked engine and record whatever it PROPOSES (writes proposal rows
    # only; the engine never mutates the graph).
    outcome = scan_for_proposals(org)
    counts = status_counts(org)
    proposals = list_proposals(org, limit=50)

    print(f"\n=== org: {org} ===")
    print(
        f"  scan: created={outcome.created} refreshed={outcome.refreshed} "
        f"skipped_already_decided={outcome.skipped_already_decided}"
    )
    print(f"  proposal status_counts: {counts}")
    seen = 0
    for p in proposals:
        d = p.to_dict()
        ev = d.get("evidence") or {}
        subj = ev.get("subject") or {}
        tgt = ev.get("target") or {}
        subj_name = subj.get("display_name") or subj.get("canonical_name")
        tgt_name = tgt.get("display_name") or tgt.get("canonical_name")
        if _MATCH_CANON not in (
            str(subj.get("canonical_name", "")),
            str(tgt.get("canonical_name", "")),
        ):
            continue
        seen += 1
        print(
            f"  PROPOSAL [{d['status']}] type={d['entity_type']} tier={d['tier']} "
            f"confidence={d['confidence']}"
        )
        print(
            f"    {subj.get('source_system')} :: {subj_name}"
            f"   <->   {tgt.get('source_system')} :: {tgt_name}"
        )
        corr = ev.get("corroborating_relationships")
        if corr:
            print(f"    corroborating_relationships: {corr}")
    if seen:
        print(f"  -> {seen} 'Payments Platform' proposal(s) now visible on the Entity Matches page for org '{org}'.")
    else:
        print("  -> WARNING: no 'Payments Platform' proposal was produced — check the engine policy.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Seed a reproducible 2.0-B2 cross-source entity match.")
    ap.add_argument(
        "--org", action="append", dest="orgs", default=None,
        help="Org id to seed (repeatable). Defaults to 'default'.",
    )
    args = ap.parse_args(argv)
    orgs = args.orgs or ["default"]
    for org in orgs:
        seed_org(org.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
