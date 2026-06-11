# Entity Overlay Authoring Guide (ENT-1)

> Audience: implementation engineers onboarding a new enterprise customer
> (e.g. City National Bank, TCU) onto AgentIQ's nCino entity extraction.
>
> Outcome: a version-controlled, customer-calibrated **entity extraction
> overlay** that makes AgentIQ extract the right people, teams, and objects
> from that customer's Salesforce/nCino org — without modifying the core pack.

---

## 1. What an overlay is

An **overlay** is a Python object (`EntityExtractionOverlay`) that declares
customer-specific extraction rules for **one connector** (`salesforce`,
`servicenow`, `jira`, …). It maps the customer's source field names and object
API names to AgentIQ entity-extraction patterns.

The generic entity extractor (T3-S12-A) is correct and unchanged. What varies
per customer is:

- **field naming** — which field holds the loan officer, relationship manager,
  credit analyst;
- **object namespace** — which `LLC_BI__*` extensions the customer has
  activated;
- **stage names** — the customer's loan-lifecycle stage labels;
- **service accounts** — the customer's integration/automation user names.

Overlays capture that variation. This is the same principle as the column alias
map in the Track 2 database connectors: the query guard is generic, the aliases
are customer-specific.

### The hard rule

> **The core pack is never modified for a customer.**
>
> If you find you need to change `ncino_overlay.py`'s common patterns to support
> a customer, that is a signal the **common** patterns need updating — not that
> the customer overlay should be skipped. The customer overlay **extends** the
> common overlay; it never replaces it.

### Key properties

| Property | Detail |
| --- | --- |
| Scope | One overlay per `(org_id, connector_id)`. nCino overlays use `connector_id='salesforce'`. |
| Isolation | One customer's rules never leak into another customer's workspace. |
| Versioned | The `version` field (semver) tracks which overlay produced which entities — critical for auditability in financial services. |
| Additive | Overlay rules are a **union** with default extraction. Fields not in the overlay still fall back to default extraction. |
| Safe default | When no overlay is registered, extraction behaves exactly as T3-S12-A. |

---

## 2. Where overlay files live

```
backend/app/entity_overlays/
├── base_overlay.py        # dataclasses (do not edit per-customer)
├── overlay_registry.py    # register_overlay(), get_overlay(), startup hook
├── ncino_overlay.py       # common nCino patterns (shared starting point)
└── {customer_id}_ncino_overlay.py   # <-- you author this, one per customer
```

The dataclasses you compose an overlay from (`base_overlay.py`):

- `EntityExtractionOverlay` — the overlay itself: `org_id`, `connector_id`,
  `version`, `person_fields`, `team_fields`, `object_rules`, `stage_map`,
  `service_account_patterns`.
- `PersonFieldRule` — `object_api_name`, `field_api_name`,
  `resolution_source` (`'id'` | `'name'`), `label`.
- `TeamFieldRule` — `object_api_name`, `field_api_name`, `resolution_source`,
  `label`.
- `ObjectRule` — `object_api_name`, `entity_type` (`'object'` | `'process'`),
  `name_field`, `record_type`.

> **`resolution_source` matters.** Use `'id'` when the field carries a stable
> source-system ID (e.g. a Salesforce `OwnerId`). ID-based persons resolve with
> `resolution_confidence = 1.0`. Use `'name'` only when the field is a plain
> display name — those resolve heuristically at `0.8`.

---

## 3. The authoring process

The overlay is the **Phase 1 deliverable**. Without it, Session 2 entity
extraction is generic. The four steps:

| Step | What | Output |
| --- | --- | --- |
| 1. Session 1 environment profile | Collect the customer's actual field API names (loan officers, relationship managers, credit analysts), actual loan-lifecycle stage names, activated `LLC_BI__*` extensions, and service-account naming patterns. | Environment profile document with the field inventory (Section 4 template). |
| 2. Author customer overlay | Write `backend/app/entity_overlays/{customer_id}_ncino_overlay.py`. Extend `NCINO_COMMON_PERSON_FIELDS` with customer-specific fields; add the customer `stage_map`; add the customer `service_account_patterns`. | `{customer_id}_ncino_overlay.py` |
| 3. Register overlay | Add a `register_overlay(...)` call to the startup sequence in `overlay_registry.register_startup_overlays()`. Restart (or rely on the next process start). | Overlay active for that `org_id` at the next run. |
| 4. Validate extraction | Run one discovery run. Review `GET /api/runs/{run_id}/entities`. Confirm loan officers, covenant analysts, and covenant objects are extracted with `resolution_confidence >= 0.8`. | Validation sign-off before Session 2. |

A standard nCino implementation takes **2–4 hours** to write an overlay for.

---

## 4. Session 1 field inventory template

Fill one row per field/object/account during the Session 1 environment profile.
Use this verbatim as a worksheet — it maps 1:1 onto the overlay dataclasses.

### 4.1 Person fields

| Object API name | Field API name | ID or name? | Human label |
| --- | --- | --- | --- |
| `LLC_BI__Loan__c` | `OwnerId` | id | Loan Owner |
| `LLC_BI__Loan__c` | `__________________` | id / name | Loan Officer |
| `LLC_BI__Covenant__c` | `__________________` | id / name | Covenant Analyst |
| `__________________` | `__________________` | id / name | Relationship Manager |
| `__________________` | `__________________` | id / name | Credit Analyst |

### 4.2 Team fields

| Object API name | Field API name | ID or name? | Human label |
| --- | --- | --- | --- |
| `__________________` | `__________________` | id / name | Credit Team |
| `__________________` | `__________________` | id / name | Underwriting Team |

### 4.3 Object rules (business objects)

| Object API name | entity_type | name_field | Record type (label) |
| --- | --- | --- | --- |
| `LLC_BI__Loan__c` | object | `Name` | Loan |
| `LLC_BI__Covenant__c` | object | `Name` | Covenant |
| `__________________` | object | `Name` | Checklist Item |
| `__________________` | object | `Name` | Spreading Record |
| `__________________` | process | `Name` | Approval |

### 4.4 Stage map (customer stage → canonical stage)

| Customer stage name | Canonical stage name |
| --- | --- |
| `__________________` | `application` |
| `__________________` | `underwriting` |
| `__________________` | `approval` |
| `__________________` | `funding` |

### 4.5 Service-account naming patterns

List regex patterns (case-insensitive) that match the customer's
integration / system / API / batch / automation users. These users are filtered
out of the entity graph so they never appear as real loan officers or analysts.
When an overlay is active, these patterns apply to both overlay-specific fields
and the default extraction paths for that connector. This is intentional, but
review the patterns carefully so a real user name is not accidentally excluded.

| Pattern (regex) | Matches example |
| --- | --- |
| `^integration[_\s]user` | `Integration User` |
| `^__________________` | `__________________` |

---

## 5. EXAMPLE ONLY overlay structure (placeholder data only)

> **EXAMPLE ONLY — not a real customer overlay.** The example below uses
> placeholder field/stage names. It demonstrates the *shape* of a customer
> overlay without exposing or implying any real customer data. Replace every
> placeholder with the values collected in Session 1.

```python
# backend/app/entity_overlays/city_national_ncino_overlay.py
# EXAMPLE ONLY — not a real customer overlay.
# Replace placeholders with Session 1 inventory values.

from app.entity_overlays.base_overlay import (
    EntityExtractionOverlay,
    ObjectRule,
    PersonFieldRule,
    TeamFieldRule,
)
from app.entity_overlays.ncino_overlay import build_ncino_overlay

# Recommended: start from the common nCino patterns and extend them.
CITY_NATIONAL_NCINO_OVERLAY: EntityExtractionOverlay = build_ncino_overlay(
    org_id="city-national",            # the customer's AgentIQ org_id
    connector_id="salesforce",         # nCino runs on Salesforce
    version="1.0.0",
    # Customer-specific person fields discovered in Session 1:
    extra_person_fields=[
        PersonFieldRule(
            object_api_name="LLC_BI__Loan__c",
            field_api_name="CNB_Relationship_Manager__c",   # placeholder
            resolution_source="id",
            label="Relationship Manager",
        ),
        PersonFieldRule(
            object_api_name="LLC_BI__Loan__c",
            field_api_name="CNB_Credit_Analyst__c",         # placeholder
            resolution_source="id",
            label="Credit Analyst",
        ),
    ],
    # Customer-specific team fields:
    extra_team_fields=[
        TeamFieldRule(
            object_api_name="LLC_BI__Loan__c",
            field_api_name="CNB_Credit_Team__c",            # placeholder
            resolution_source="id",
            label="Credit Team",
        ),
    ],
    # Customer-specific objects beyond the common Loan/Covenant rules:
    extra_object_rules=[
        ObjectRule(
            object_api_name="LLC_BI__Checklist_Item__c",    # placeholder
            entity_type="object",
            name_field="Name",
            record_type="Checklist Item",
        ),
    ],
    # Customer stage labels → canonical stages:
    stage_map={
        "CNB Intake": "application",          # placeholder
        "CNB Underwriting": "underwriting",   # placeholder
        "CNB Credit Approval": "approval",    # placeholder
    },
    # Customer service-account naming on top of the common nCino patterns:
    extra_service_account_patterns=[
        r"^cnb[_\s]integration",              # placeholder
        r"^svc[_\s]",                         # placeholder
    ],
)
```

You can also build an overlay directly with `EntityExtractionOverlay(...)` if you
do not want the common nCino seed — but extending `build_ncino_overlay()` is the
recommended path so common improvements flow to every customer.

---

## 6. Registering an overlay

Registration happens **at startup**, never at query time. Add the call to
`register_startup_overlays()` in
`backend/app/entity_overlays/overlay_registry.py`:

```python
def register_startup_overlays() -> None:
    from app.entity_overlays.city_national_ncino_overlay import (
        CITY_NATIONAL_NCINO_OVERLAY,
    )
    register_startup_overlay(CITY_NATIONAL_NCINO_OVERLAY)
    # register additional customer overlays here ...
```

`register_startup_overlays()` is already invoked from the FastAPI app lifespan,
so the overlay is active for its `org_id` by the first discovery run after the
process starts. Re-registering the same `(org_id, connector_id)` replaces the
previous overlay — that is how you deploy a new overlay **version** when a
customer's Salesforce schema changes.

For local experimentation or scripted setup you can also register at runtime:

```python
from app.entity_overlays.overlay_registry import register_overlay
register_overlay(CITY_NATIONAL_NCINO_OVERLAY)
```

---

## 7. Testing an overlay

1. **Unit-check the overlay object** — confirm it constructs without error and
   references the object API names you expect:

   ```python
   ov = CITY_NATIONAL_NCINO_OVERLAY
   assert ov.org_id == "city-national"
   assert "LLC_BI__Loan__c" in ov.referenced_object_names()
   ```

2. **Run a discovery run** for the customer's `org_id` and inspect
   `GET /api/runs/{run_id}/entities`.

3. **Confirm the validation checklist:**
   - Loan officers and covenant analysts appear as `person` entities.
   - ID-based person fields have `resolution_confidence == 1.0`.
   - Covenant / loan objects appear as `object` entities.
   - The same loan officer seen in Salesforce **and** ServiceNow/Jira resolves to
     **one** Person entity with all systems listed in `metadata.sources`
     (cross-system resolution anchored to the Salesforce ID).
   - Service accounts (integration/system/API users) do **not** appear as
     entities. The number filtered is reported in the
     `entity.extraction_completed` telemetry event
     (`filtered_service_account_count`).

4. **Confirm no regression for orgs without an overlay** — their extraction is
   unchanged.

See `backend/tests/contract/test_entity_overlays.py` for the full contract test
suite covering all of the above.

---

## 8. Cross-system Person resolution (the demo moment)

The highest-value resolution for nCino customers is cross-system Person
resolution: the same loan officer appears in Salesforce
(`LLC_BI__Loan__c.OwnerId`), ServiceNow (`assigned_to`), and Jira (`assignee`).

The overlay anchors this by declaring the Salesforce `OwnerId` as an ID-based
person field (`resolution_source='id'`, confidence 1.0). Later sightings in
ServiceNow/Jira match by name against the Salesforce display name and resolve to
the same entity — the confidence stays 1.0 (anchored to the ID), and every
system is recorded in `metadata.sources`:

```
# Result: one Person entity 'Sarah Chen' seen in 3 systems
# metadata.sources = ['salesforce', 'servicenow', 'jira']
# resolution_confidence = 1.0  (anchored to the Salesforce OwnerId)
```

That single, correctly-resolved cross-system person is what makes the nCino
demo moment possible.
