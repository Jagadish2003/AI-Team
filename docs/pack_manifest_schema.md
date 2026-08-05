# Pack manifest schema (2.0-C3 T1 / AT-836)

The declarative definition of an authored pack. Read this before adding a field to
the manifest, adding a detector primitive, or writing code that installs, packages,
or validates a partner pack.

Implementation: `backend/discovery/packs/sdk/manifest.py` (schema + validator) and
`backend/discovery/packs/sdk/primitives.py` (primitive vocabulary + parameter
contracts). Worked example:
`backend/discovery/packs/sdk/examples/example_partner_pack.json`. Tests:
`backend/tests/unit/test_pack_manifest_schema.py`.

---

## 1. What a manifest is, and what it deliberately is not

A manifest is a **closed JSON document** describing a pack: who wrote it, what
platform it needs, what its detectors are, how they are scored, what language they
speak, and what a template pre-populates. It is the whole pack.

It is not, and must never become, a way to ship behaviour the platform does not
already have. 2.0-C3's governing constraint:

> No partner-supplied executable code runs inside a customer deployment.

Everything in this schema follows from that sentence:

| Rule | Why it is not merely tidiness |
|------|-------------------------------|
| Every field is enumerated; an unknown key is a hard error | A schema that ignores what it does not understand is a schema an author can smuggle through. Silence is the vulnerability. |
| Detectors name a **primitive**, never a module path | The first-party registry lists importable module paths. That field does not exist here, and a dotted path in an identifier field is refused by name. |
| Every parameter is typed and **bounded** | `max_depth: 40` is an unbounded graph walk wearing configuration's clothes. Bounds are part of the security posture. |
| Concepts come from the platform's normalised vocabulary | A pack that reads something the platform never declared cannot be gated, versioned, or ported. |
| Code-shaped keys and values are refused anywhere, at any depth | The constraint is document-wide, not a per-field concern. |

The manifest is data. Nothing in the SDK imports, loads, compiles, or executes any
part of it — a structural test pins that the package never grows a dynamic import.

---

## 2. Document shape

```jsonc
{
  "manifestVersion": "agentiq-pack-manifest-v1",   // required, exact
  "primitiveLibraryVersion": "1.0.0",              // optional; may not exceed the platform's
  "pack":              { ... },                    // required
  "compatibility":     { ... },                    // required
  "certification":     { ... },                    // optional (a request, never a claim)
  "detectors":         [ ... ],                    // required, at least one
  "scorerCalibration": { ... },                    // optional
  "terminology":       { ... },                    // optional
  "templateDefaults":  { ... }                     // optional
}
```

### 2.1 `pack` — identity

| Field | Required | Notes |
|-------|----------|-------|
| `packId` | yes | `lower_snake_case`, 3–64 chars. **May not be a first-party pack id** — an authored pack cannot shadow `cloud_ops`, `service_cloud`, … |
| `packName` | yes | Human-readable. |
| `packVersion` | yes | Dotted numeric. Bump it whenever detector or calibration behaviour changes (the R16-B1 §4 discipline applies to authored packs too). |
| `domain` | no | Defaults to `packId`. |
| `description` | yes | One paragraph; what the pack finds. |
| `author` | yes | `{ name, contact, url? }` — `name` and `contact` required. |

### 2.2 `compatibility` — the 2.0-C1 declaration

Exactly the block `pack_compatibility.py` gates against, so an authored pack is
refused at activation for the same reasons and with the same wording as a
first-party one.

| Field | Required | Notes |
|-------|----------|-------|
| `minPlatformVersion` | **yes** | Inclusive floor. Required for an authored pack: a partner pack that does not state its floor cannot be gated honestly. |
| `maxPlatformVersion` | no | Inclusive ceiling; `null` ⇒ open-ended. |
| `requiredConcepts` | yes | Gating. Every id must be in the platform's normalised-concept vocabulary (`platform_capabilities.NORMALISED_CONCEPTS`). |
| `optionalConcepts` | no | Advisory — the pack degrades honestly without them. |

Two rules that fail at authoring time rather than in front of a customer:

* the declared floor must be **at or above** the `since` of every required concept
  (the same self-contradiction check the first-party registry is held to);
* the range must be non-empty.

### 2.3 `certification` — a request, not a claim

An author may state the level they will **request**. They may not state the level
they hold, and they may not carry a signature:

```jsonc
"certification": {
  "requestedLevel": "partner",              // certified | partner | community
  "contact": "certification@partner.test",
  "notes": "Submitted for Partner review against the 2.0 platform."
}
```

`level`, `signature`, `certifyingEntity`, `reviewDate`,
`reviewedAgainstPlatformVersion`, and `scope` are **refused by name**. Those are
issued by CloudFulcrum after review (2.0-C2 AT-831/AT-832). If an author could
write `"level": "certified"` into their own manifest, the signature would stop
being the trust root and the badge would mean nothing — which is exactly the hole
2.0-C2 exists to close. A projected manifest therefore always registers as
`community`; the requested level travels separately, as a request.

### 2.4 `detectors` — composed primitives

```jsonc
{
  "detectorId": "repeated_manual_resolution",   // required, lower_snake_case, unique
  "title": "Repeated manual resolution of the same incident shape",  // required
  "primitive": "recurrence",                    // required, from the primitive library
  "concepts": ["resolution_signature"],         // required; must be DECLARED above
  "parameters": { "min_occurrences": 4, "window_days": 30 },
  "labels": { "summary": "...", "whyItMatters": "...", "recommendation": "..." },
  "enabledByDefault": true
}
```

A detector may only read a concept the manifest declared in
`requiredConcepts`/`optionalConcepts`. Otherwise the compatibility gate would be a
lie: the pack would depend on something no activation check can see.

The four-part finding contract — evidence, confidence, corroboration status,
source trace — is **inherited from the primitive**, not re-implemented per pack.
That is the point of composing: an author cannot forget it, and cannot opt out.

### 2.5 `scorerCalibration`

```jsonc
{
  "impactWeights": {           // must sum to 1.0 (±0.001) across the four dimensions
    "effort_concentration": 0.4, "breadth": 0.25,
    "recurrence_stability": 0.2, "automation_shape": 0.15
  },
  "confidence": { "singleSourceCap": "MEDIUM", "corroboratedMax": "HIGH",
                  "conversationSourceCap": "MEDIUM" },
  "dimensions": { "automation_shape": { "trivial_ttr_minutes": 30 } }   // numbers/booleans only
}
```

A pack calibrates the platform's scoring engine; it does not ship one. Two
standing ceilings a manifest may **lower but never raise**: `singleSourceCap` and
`conversationSourceCap` cap at `MEDIUM`. A partial weight set is refused rather
than silently rescaled — a ranking that quietly renormalises is a ranking nobody
can explain.

### 2.6 `terminology` and `templateDefaults`

`terminology` is `{ glossary, languageMap, llmContext }` — labels and language as
data. `templateDefaults` is `{ industry, systems, recommendedSystems,
workflowFocus, roles }` — the R18-C1 template pre-population, as configuration.

Connector ids in `templateDefaults` are validated for **shape** only here. The
R191-R1 anchor-on-shipped cross-check (are those connectors real and shipped?)
belongs at install time, where the deployment's connector catalog is readable;
this module stays dependency-free of `app`. That is a declared gap, not an
oversight.

---

## 3. Detector primitive library

`primitives.py` declares the closed set, each with a typed parameter contract, its
concept arity, and its evidence/corroboration semantics.

| Primitive | Shape it detects | Concepts |
|-----------|------------------|----------|
| `recurrence` | The same normalised fact recurring above a count within a window | 1 |
| `threshold_vs_baseline` | A measure departing from its own observed baseline | 1 |
| `ageing` | Items sitting in a state past a threshold | 1 |
| `oscillation` | Repeated back-and-forth transitions (ping-pong, flapping) | 1 |
| `concentration_traversal` | Work concentrating on a shared entity, depth-bounded | 1+ |
| `co_occurrence_window` | Two concepts co-occurring inside a correlation window | exactly 2 |

This module declares the **vocabulary and contracts**; binding each id to platform
detector machinery is the separate primitive-library task. One vocabulary read
from both ends — the same discipline `certification_criteria.py` applies to the
review checklist.

`manifest_schema_reference()` renders the whole schema *and* the primitive catalog
as JSON from the same constants validation uses, so the reference an author reads
and the rules an installer enforces cannot drift.

---

## 4. Validation

`validate_manifest(document)` never raises: the verdict is the return value, and it
reports **every** problem, each with a JSON path and a machine-readable code — an
author fixing one error per round trip is an author who gives up.
`parse_manifest(document)` is the raising variant; `load_manifest(path)` reads a
file (an unreadable or non-JSON file is reported as a manifest error, not a stack
trace).

Error codes: `unknown_field`, `missing_field`, `invalid_type`, `invalid_value`,
`duplicate_value`, `reserved_pack_id`, `reserved_field`, `unknown_primitive`,
`unknown_parameter`, `missing_parameter`, `parameter_out_of_range`,
`unknown_concept`, `undeclared_concept`, `concept_requires_newer_platform`,
`executable_code_forbidden`, `confidence_ceiling_exceeded`,
`impact_weights_invalid`.

### The code-shape sweep

Runs over the raw document first, before any field is examined:

* **forbidden keys** anywhere, at any depth — `code`, `command`, `module`,
  `script`, `entrypoint`, `eval`, `exec`, `hook`, `plugin`, `python`, `shell`, …;
* **code-shaped values** — import statements, lambdas, `eval(`/`exec(`/`compile(`/
  `__import__(`, subprocess or shell invocations, dunder attributes, shebangs, and
  executable file references (`.py`, `.sh`, `.exe`, `.dll`, …);
* **dotted module paths** in identifier fields (`primitive`, `detectorId`,
  `packId`, `domain`, `industry`).

The import pattern is deliberately anchored on the `import` keyword rather than a
bare `from`, because ordinary pack prose says things like "findings *from*
ServiceNow queues". A code sweep that cries wolf on documentation is a code sweep
authors learn to route around.

---

## 5. Projection into the platform

`manifest_to_pack_config(manifest)` projects a validated manifest into the
`PACK_REGISTRY` config shape, so the existing pack lifecycle — compatibility gate,
safe disable, rollback, certification, version stamping — reads an authored pack
through the paths it already has, rather than acquiring a parallel one.

Two properties of the projection are load-bearing:

* `detectors` is **empty and always will be**. That field holds importable module
  paths and an authored pack has none; its detectors live under
  `manifestDetectors` as declarations the platform's own primitives execute.
* `certification` is the **community default**, whatever the manifest requested.

`manifest_fingerprint(manifest)` is SHA-256 over the manifest's canonical JSON
(the same canonicalisation the certification signature uses), so packaging can
bind a bundle signature to exactly this manifest and installation can prove the
document it validated is the document it registers.

---

## 6. Scope of this task

AT-836 owns the **schema**: the document, its vocabulary, its validator, the
fingerprint, and the projection. The authoring toolkit (scaffold, fixture harness,
lint pass), packaging and installation, sandbox validation, and partner
documentation are separate 2.0-C3 tasks. They consume this module rather than
re-deriving its rules — if a later task re-implements a validation rule instead of
calling `validate_manifest`, that is the drift this schema exists to prevent.
