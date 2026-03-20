# Task 2 — Seed Dataset Pack — CHANGELOG

## v2.1
- Added opp_010 (Complex, UNREVIEWED) to keep Stage 90 populated while retaining opp_009 as REJECTED for decision variety.
- Added seed_loader assertion to prevent Stage 90 from being empty.


## v2
- seed_loader.py: fixed end-of-run NameError and added stable count printing.
- Normalized Confidence enums to HIGH/MEDIUM/LOW across seed payloads.
- evidence.json: added tsLabel, evidenceType, entities[] for EvidenceReview completeness.
- opportunities.json: ensured aiRationale + override object exist for every opportunity.
- mappings.json: added commonEntity for entity filtering.
- connectors.json: added recommendedRank for recommended ordering.
- events.json: expanded to 10+ events for richer Screen 3 run log.
- Added decision variety: one opportunity set to REJECTED for demo coverage.

## Notes
- The primary EPIC document remains unchanged to preserve original intent and readability.
- This changelog exists only to help developers see deltas when updating their local pack.
