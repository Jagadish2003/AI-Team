# MSP-B4 Resolution & Incident-Identity Signatures (T2)

The B4 counterpart to the B0 [`event_signature` contract](msp_operational_event_schema.md#3-event_signature-construction-rules--at-636--the-mapping-contract).
**One signature discipline across the pack:** both are deterministic,
explainable, tested, conservative, and versioned. Where B0 fingerprints *which
recurring cloud event this is*, B4 fingerprints two things about a ServiceNow
incident, using **structured fields only** — no semantic similarity, no fuzzy
text matching. Semantic matching of free-text resolutions is **MSP-B5**, not
this.

Implementation: [`backend/discovery/signals/resolution_signature.py`](../backend/discovery/signals/resolution_signature.py).
Produced onto every incident's resolution block by
[`servicenow.py`](../backend/discovery/ingest/servicenow.py) (MSP-B4 T1/T2).

## Two signatures

The recurrence detector (B6/T3) groups on the **pair** `(incident_identity_signature, resolution_signature)`; runbook matching (B5) reads `resolution_signature` to know what resolution pattern was observed without re-deriving it.

| Signature | Answers | Components (ordered) |
|-----------|---------|----------------------|
| `incident_identity_signature` | *What kind of incident is this?* | `category`, CI component, normalised short-description token set |
| `resolution_signature` | *How was it resolved?* | `category`, `close_code`, CI component, resolved-by assignment **group** |

**Format:** `"{VERSION}:{sha256_128bit_hex}"` — e.g. `1:88ea9588…`.
`RESOLUTION_SIGNATURE_VERSION` / `INCIDENT_IDENTITY_SIGNATURE_VERSION` prefix
every signature and are bumped whenever that signature's recipe or a
normalisation rule changes, so signatures from different rule versions never
silently compare equal.

`resolution_signature` is present only for **resolved** incidents (a resolution
is what it fingerprints); `incident_identity_signature` is present for every
incident, resolved or not.

- **Groups, never people.** `resolved_by_group` is the incident's assignment
  group (a queue). No individual participates in any signature.
- **Notes never participate.** The signature is over structured fields; the
  resolution note's free text is not an input (only its explicitly-referenced,
  deterministic runbook identifier is surfaced elsewhere on the block — T1).

## Normalisation (explicit and documented)

Every component is reduced by `normalize_token` before hashing:

| Concern | Rule |
|---------|------|
| **Case** | Casefolded — `"Solved (Permanently)"` → `solved (permanently)`. |
| **Whitespace** | Stripped; internal runs collapsed to one space — `"  Level 2   Support "` == `"Level 2 Support"`. |
| **Empty / missing** | `None`, `""`, whitespace-only all fold to `""`. A missing component participates as empty, never a guessed value. |
| **ServiceNow reference display values** | A raw `{"value", "display_value"}` object folds to its **stable `value`** (e.g. a group's sys_id), not the mutable display name. Callers pass pre-extracted scalars (`servicenow.py` extracts a CI reference to its `sys_id`); this is a defensive, documented fallback. |

**Short-description tokenisation** (`normalize_short_description`): casefold →
maximal alphanumeric tokens → drop a small fixed set of grammatical filler
(articles / conjunctions / prepositions / pronouns / copulas) and sub-2-char
tokens → de-duplicate → sort. Order-, case-, and punctuation-independent, but
purely structural: **no stemming, edit distance, or semantic expansion** (that
is B5). `"outage"` and `"outages"` are therefore *different* tokens.

## CI component (`class:` / `ci:` / unlocated)

The CI component prefers the **CI class** when known (broader "this class of
thing"), else the specific **CI id**, else empty ("unlocated"):

```
ci_class present  → "class:<normalised>"
else ci_id present → "ci:<normalised>"
else               → ""            (unlocated)
```

The explicit `class:` / `ci:` markers mean a CI id can never collide with a CI
class that shares its text. This is the **B3 soft-dependency** seam:

- **Without B3** (no CMDB join): incidents are unlocated or keyed on the CI id;
  recurrences still emit and group (**AC5**).
- **With B3**: the CI class sharpens the component from `ci:` to `class:` for
  broader, estate-aware grouping. Because that changes the produced signatures,
  it is a version-bump-worthy rule change.

## Conservative grouping / near-miss separation (AC2)

Because `category`, `close_code`, and the CI component all participate,
near-misses stay separate **by construction**:

- same category, **different close code** ⇒ different `resolution_signature`;
- same close code, **different CI class** ⇒ different `resolution_signature`
  *and* different `incident_identity_signature`.

Two records group only when their structured identity **and** resolution pattern
truly match.

## Guarantees

- **Deterministic** — pure functions of their inputs; no clock, no randomness.
  Identical structured input ⇒ identical signature.
- **Conservative** — any change to a participating component changes the
  signature; near-misses do not group.
- **Explainable** — `resolution_signature_components()` /
  `incident_identity_signature_components()` return the resolved components for
  any inputs, so a consumer can explain *why* two incidents share or differ in a
  signature without re-deriving the rules.
- **Tested** — [`test_msp_b4_signatures.py`](../backend/discovery/tests/test_msp_b4_signatures.py)
  covers stable grouping, near-miss separation from both sides, missing-optional
  behaviour, normalisation, and integration through the ServiceNow ingest path.
