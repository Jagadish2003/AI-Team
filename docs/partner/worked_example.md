# Worked example: a service-desk pack, end to end

This walks the whole loop with a real pack — scaffold, author, validate, test,
lint, package, verify, install. Every command and every output below is from the
pack that lives in this repository at:

```
backend/discovery/packs/sdk/examples/example_service_desk/
  pack.json
  fixtures/01_busy_service_desk.json     every detector fires
  fixtures/02_below_the_floor.json       the same shapes, under every floor: silence
  fixtures/03_quiet_estate.json          an ordinary quiet month: silence
  README.md
```

**CI builds and checks this pack on every push.** Partner documentation that
drifts from reality is worse than none, so the example is a test artifact rather
than a listing: if a rule changes and the example stops passing, the build fails
before the document can start lying to you.

Run it yourself, from `backend/`:

```bash
python scripts/pack_sdk.py check discovery/packs/sdk/examples/example_service_desk
```

---

## 1. Scaffold

```console
$ python scripts/pack_sdk.py scaffold ./my_pack --pack-id acme_service_desk \
      --name "Acme Service Desk" --author "Acme Ltd" --contact packs@acme.test
Scaffolded pack 'acme_service_desk' in ./my_pack:
  ./my_pack/fixtures/01_recurring_work_fires.json
  ./my_pack/fixtures/02_aged_work_fires.json
  ./my_pack/fixtures/03_thin_signal_is_quiet.json
  ./my_pack/pack.json
  ./my_pack/README.md

Next: python scripts/pack_sdk.py check ./my_pack
```

What you get is a **working** pack: two detectors, three fixtures, and a manifest
whose platform floor is derived from the concepts it declares. It passes
`check` immediately — run it before you change anything. (A scaffold whose output
fails its own tooling teaches you that the errors are noise, and from then on you
read past the real ones.)

Note the third fixture. The scaffold writes a negative case because a detector
that fires on everything passes a positive-only suite forever.

## 2. Author the manifest

The example pack declares four detectors over four different primitives. The
interesting parts, block by block.

**Compatibility** — what the pack needs from the platform:

```json
"compatibility": {
  "minPlatformVersion": "1.9.0",
  "maxPlatformVersion": null,
  "requiredConcepts": ["incident_workflow", "resolution_signature", "operational_event"],
  "optionalConcepts": ["cmdb_dependency", "cross_system_link"]
}
```

`cmdb_dependency` is **optional** deliberately, even though the concentration
detector reads it. Without a dependency graph that detector simply finds nothing,
which is an honest degradation; declaring it required would turn a customer who
has not connected a CMDB into a refused activation.

**A detector** — recurrence over resolution signatures:

```json
{
  "detectorId": "repeated_manual_resolution",
  "title": "Repeated manual resolution of the same incident shape",
  "primitive": "recurrence",
  "concepts": ["resolution_signature"],
  "parameters": {"min_occurrences": 4, "window_days": 30, "group_by": "signature"},
  "labels": {
    "summary": "The same resolution is applied repeatedly by hand.",
    "whyItMatters": "Repeated identical resolutions are the clearest candidate for an assisting agent.",
    "recommendation": "An agent handles the recurring cases; the residual requires judgment."
  }
}
```

`min_occurrences` is 4 where the floor is 2 — the floor is the minimum the
platform will accept, not a recommendation. Pick the number your domain actually
justifies.

**The other three** use a different primitive each, because four detectors over
one primitive would teach you one thing:

| Detector | Primitive | Reads | What it says |
|---|---|---|---|
| `service_desk_queue_ageing` | `ageing` | `incident_workflow` | open incidents past 14 days, five or more of them |
| `shared_dependency_concentration` | `concentration_traversal` | incidents + CMDB + events | work across services concentrating on one dependency, two hops out |
| `alert_to_incident_pairing` | `co_occurrence_window` | `operational_event` + `incident_workflow` | alerts followed by a manually raised incident inside 120 minutes |

The concentration detector is the one to read closely. Its `max_depth` is 2 (the
cap is 3), it sets `require_corroboration: true`, and its labels are worded as
concentration — *"incidents across several services concentrate on one shared
dependency"* — with an `evidenceHint` saying so explicitly. A causal rewording of
that same sentence fails lint.

**Terminology** — the language findings are written in:

```json
"terminology": {
  "llmContext": "Service-desk operations analysis. Speak service-desk language: incidents, queues, dwell time, escalation. Reference groups and queues only, never individuals. Describe concentration, never causation. No automated incident resolution: humans remain responsible for every action."
}
```

Those last three sentences are not decoration. They are the discipline rules
restated where the model will read them.

**Certification** — a request, never a claim:

```json
"certification": {
  "requestedLevel": "partner",
  "contact": "certification@example-partner.test",
  "notes": "Submitted for Partner review against the 2.0 platform."
}
```

## 3. Validate

```console
$ python scripts/pack_sdk.py validate discovery/packs/sdk/examples/example_service_desk
Manifest is valid: example_service_desk v0.1.0 (4 detector(s))
```

Validation reports **every** error at once, each with a JSON path and a code, so a
manifest with six problems takes one pass rather than six.

## 4. Write fixtures

A case is seeded signal plus what you expect from it:

```jsonc
{
  "name": "a busy service desk: every detector fires",
  "signal": {
    "dependencyEdges": {"svc-payments": ["db-core"], "svc-billing": ["db-core"]},
    "records": [ /* normalised concept records */ ]
  },
  "expect": {
    "detectors": {
      "repeated_manual_resolution": {
        "fires": true, "findingCount": 1, "subjects": ["restart_payment_worker"],
        "minMetric": 6, "confidence": "MEDIUM", "corroboration": "single_source",
        "statementContains": "recurs 6 times"
      },
      "service_desk_queue_ageing": { "fires": true, "subjects": ["svc-payments"] }
    },
    "findingCount": 4,
    "noOtherDetectorsFire": true
  }
}
```

```console
$ python scripts/pack_sdk.py test discovery/packs/sdk/examples/example_service_desk
  [PASS] a busy service desk: every detector fires
  [PASS] real friction, but under every floor: nothing fires
  [PASS] a quiet estate: nothing to report
Fixtures: 3/3 case(s) passed
```

Four things the harness does that you did not ask it to:

* it checks the **four-part contract** on every finding in every case;
* it **fails** a case naming a detector the manifest does not declare, rather than
  silently asserting nothing;
* it reports **every** failed expectation in a case, not the first;
* a signal the platform refuses at admission (an individual field, an unknown
  concept) reads as a case failure with the reason, not a traceback.

The second negative fixture is the one worth copying. It reproduces each detector's
shape one step *below* its floor — three identical resolutions where the detector
needs four, two aged incidents where it needs five — and asserts silence. That is
the case that catches a floor accidentally loosened.

## 5. Lint and check

```console
$ python scripts/pack_sdk.py lint discovery/packs/sdk/examples/example_service_desk
Lint clean.

$ python scripts/pack_sdk.py check discovery/packs/sdk/examples/example_service_desk
example_service_desk v0.1.0: validate, fixtures, and lint all pass.
```

`check` runs validate → fixtures → lint in that order, and it is the same function
the platform runs before letting your pack activate. When it fails it names the
specific reason:

```console
$ python scripts/pack_sdk.py check ./broken_pack
Pack cannot be activated:
  lint [missing_aggregation_floor] detectors[1].parameters.min_items: min_items=1 lets a
  single record become a finding; the aggregation floor for 'ageing' is 2
```

## 6. What a finding looks like

The recurrence detector, on the busy-desk fixture:

```json
{
  "detectorId": "repeated_manual_resolution",
  "subject": "restart_payment_worker",
  "statement": "The same resolution_signature recurs 6 times in 30 days (signature: restart_payment_worker).",
  "metricValue": 6.0,
  "threshold": 4.0,
  "findingContract": {
    "evidence": {
      "concept": "resolution_signature", "grouped_by": "signature",
      "occurrences": 6, "window_days": 30, "distinct_actor_groups": 2,
      "actor_groups": ["payments-ops", "service-desk"],
      "first_seen": "2026-06-02T09:15:00+00:00", "last_seen": "2026-06-26T17:55:00+00:00"
    },
    "confidence": {
      "level": "MEDIUM", "capped": true, "eligible_for_high": false,
      "cap_reason": "Single-source observation — no independent system agrees yet."
    },
    "corroboration": {
      "status": "single_source", "sources": ["servicenow"],
      "label": "Single-source, confidence capped accordingly"
    },
    "source_trace": {
      "systems": ["servicenow"],
      "artifacts": [{"type": "resolution_signature", "id": "INC-3001", "source_system": "servicenow", "observed_at": "2026-06-02T09:15:00+00:00"}]
    }
  }
}
```

Nothing in the manifest set that confidence level. Six occurrences in one system
is MEDIUM with the cap stated; the co-occurrence detector on the same fixture
reaches HIGH because two systems agree inside its window.

## 7. Package

A bundle must be signed. The platform ships **no** publisher keys, so you sign
with your own and the customer's deployment lists it in
`PACK_BUNDLE_TRUSTED_KEYS`:

```console
$ export PACK_BUNDLE_SIGNING_KEY=<your base64 32-byte ed25519 seed>
$ python scripts/pack_sdk.py package discovery/packs/sdk/examples/example_service_desk \
      --out ./example.aiqpack --key-id acme-2026
Packaged example_service_desk v0.1.0 -> ./example.aiqpack
  files:  5
  digest: <sha256 of the signed index>
  key id: acme-2026
```

`package` runs `check` first and refuses to build an artifact the platform would
reject. Keep the signing key in the environment, never on the command line or in
a repository.

Verify what you built — the same check installation runs:

```console
$ python scripts/pack_sdk.py verify ./example.aiqpack
Bundle verifies: example_service_desk v0.1.0 (5 files, signed by 'acme-2026')
```

And what tampering looks like. Change one byte of the manifest inside the zip:

```console
$ python scripts/pack_sdk.py verify ./tampered.aiqpack
Bundle does NOT verify [bundle_content_mismatch]: pack.json does not match its signed
digest (the bundle has been altered)
```

The signature covers a per-file digest index rather than the archive bytes, so a
bundle repacked by a mirror still verifies while any change to its content does
not.

## 8. Install and activate

The customer's Owner installs the bundle:

```http
POST /api/packs/install
{"bundleBase64": "<the .aiqpack, base64>", "activate": true}
```

Five gates run in order, and a refusal names the one that stopped it:

1. **signature** — verified before anything is written to disk;
2. **validation** — your manifest, your fixtures, and lint, through the same
   `check` you ran locally;
3. **compatibility** — your declared platform range and required concepts;
4. **certification policy** — a Certified-only org refuses a Community pack,
   naming its floor;
5. **persist**.

Installing is not activating, and gates 3 and 4 are re-run on activation, because
both can move underneath an installed pack: the platform can be upgraded past your
declared ceiling, and an Owner can raise the certification floor.

Withdrawing a pack (`{"active": false}`) runs no gates and deletes nothing —
findings your pack produced stay retrievable, labelled with the pack and version
that produced them.

## 9. Where to go next

* [Concept vocabulary](concept_vocabulary.md) — everything your detectors can read
* [Primitive reference](primitive_reference.md) — everything they can be made of
* [Discipline rules](discipline_rules.md) — everything they are held to
