# AgentIQ Skills SDK — authoring a pack

Partner-facing documentation for the AgentIQ Skills SDK. If you are building a
pack, start here and read in this order:

| Document | What it answers |
|---|---|
| this page | What a pack is, what it may contain, and the loop you work in |
| [Concept vocabulary](concept_vocabulary.md) | What your detectors read — the normalised signal the platform provides |
| [Primitive reference](primitive_reference.md) | What your detectors are made of, and every parameter you may set |
| [Discipline rules](discipline_rules.md) | The requirements your pack is held to, and where each one is enforced |
| [Worked example](worked_example.md) | A complete pack, built end to end with the toolkit |

CloudFulcrum engineers changing the SDK itself want the internal notes instead:
[`pack_manifest_schema.md`](../pack_manifest_schema.md),
[`pack_primitive_library.md`](../pack_primitive_library.md),
[`pack_authoring_toolkit.md`](../pack_authoring_toolkit.md),
[`pack_packaging_installation.md`](../pack_packaging_installation.md).

---

## 1. What a pack is

A pack is **a document**. It declares who you are, what platform capabilities you
need, the detectors you want run, how findings should be ranked, and the language
findings should be written in. That is the whole artifact: one `pack.json`, plus
the fixtures that prove it does what you say.

A pack is **not** a plugin, and this is the one thing to internalise before you
start:

> **No partner-supplied executable code runs inside a customer deployment.**

There is no module path field, no expression language, no callback, no escape
hatch — and the schema refuses code-shaped keys and values anywhere in the
document, at any depth. AgentIQ runs inside banks, insurers, and federal
boundaries; a pack that could ship arbitrary code into one of those would end the
security argument that gets AgentIQ deployed there at all, and no review process
recovers that.

The practical consequence: **your detectors are compositions, not
implementations.** You pick a primitive, bind it to normalised concepts, and set
its parameters. If the shape you need is genuinely not expressible, the answer is
a new platform primitive in a future release — tell us, and we will scope it.
Please do not contort a parameter to approximate it.

## 2. What you get in return

Because the platform runs the detector, your findings inherit things you would
otherwise have to build and defend:

* **The four-part contract** on every finding — evidence, confidence,
  corroboration status, and a source trace to real records. You cannot emit a
  finding without it, and you cannot weaken it.
* **Confidence you did not have to assert.** It is derived from how many
  independent sources agree. There is no field anywhere in a manifest that sets a
  confidence level, deliberately.
* **Portability.** You bind normalised concepts, not connector payloads, so a
  detector written against `incident_workflow` works wherever the platform can
  normalise incident workflow — not only against the connector you tested with.
* **The lifecycle.** Compatibility gating, safe disable, version rollback, and
  certification apply to your pack exactly as they do to a first-party one.

## 3. The loop

Everything below runs offline against your own machine. Nothing here signs,
installs, or contacts CloudFulcrum.

```bash
# 1. Start a project
python scripts/pack_sdk.py scaffold ./my_pack --pack-id acme_service_desk \
    --name "Acme Service Desk" --author "Acme Ltd" --contact packs@acme.test

# 2. Edit pack.json and fixtures/*.json, then, repeatedly:
python scripts/pack_sdk.py validate ./my_pack   # well-formed and closed?
python scripts/pack_sdk.py test ./my_pack       # do the fixtures produce what you expect?
python scripts/pack_sdk.py lint ./my_pack       # does it hold the non-negotiables?
python scripts/pack_sdk.py check ./my_pack      # all three, exactly as installation runs them

# 3. Reference, whenever you need it
python scripts/pack_sdk.py primitives           # every primitive and parameter
python scripts/pack_sdk.py schema               # the manifest schema
python scripts/pack_sdk.py rules                # the rules and the aggregation floors

# 4. Ship
PACK_BUNDLE_SIGNING_KEY=<your base64 ed25519 seed> \
  python scripts/pack_sdk.py package ./my_pack --key-id acme-2026
python scripts/pack_sdk.py verify ./my_pack/../acme_service_desk-1.0.0.aiqpack
```

Every command exits non-zero on failure, so the loop drops straight into your
CI. `--json` on any of them gives a machine-readable report.

`check` is the important one: it is the *same function* the platform runs before
letting your pack activate in a customer deployment. "It passed on my machine"
and "it passed on install" cannot diverge, because they are one code path.

## 4. The manifest, at a glance

<!-- generated:manifest_shape — regenerate with `python scripts/pack_sdk.py docs --write`; do not edit by hand -->
Manifest version **agentiq-pack-manifest-v1**. Every key below is the complete set — the schema is closed, so an unrecognised key anywhere is an error, not an ignored extra.

| Block | Required | Fields |
|---|---|---|
| `manifestVersion` | required | — |
| `primitiveLibraryVersion` | optional | — |
| `pack` | required | `packId`, `packName`, `packVersion`, `domain`, `description`, `author` |
| `compatibility` | required | `minPlatformVersion`, `maxPlatformVersion`, `requiredConcepts`, `optionalConcepts` |
| `certification` | optional | `requestedLevel`, `contact`, `notes` |
| `detectors` | required | `detectorId`, `title`, `primitive`, `concepts`, `parameters`, `labels`, `enabledByDefault` |
| `scorerCalibration` | optional | `impactWeights`, `confidence`, `dimensions` |
| `terminology` | optional | `glossary`, `languageMap`, `llmContext` |
| `templateDefaults` | optional | `industry`, `systems`, `recommendedSystems`, `workflowFocus`, `roles` |

Keys refused anywhere in the document, at any depth, because they are how code gets smuggled into configuration:

> `code`, `command`, `detectormodule`, `entrypoint`, `eval`, `exec`, `expression`, `hook`, `import`, `module`, `modulepath`, `plugin`, `pluginpath`, `python`, `script`, `shell`, `source_code`, `sourcecode`
<!-- /generated:manifest_shape -->

The [worked example](worked_example.md) fills every one of those blocks in with
something real, and the [schema reference](../pack_manifest_schema.md) explains
why each rule is the way it is.

## 5. Certification

A manifest may **request** a certification level. It can never assert one: the
level a customer sees comes from a CloudFulcrum signature over the pack's
certification metadata, and a pack claiming Certified without a valid signature
is displayed as Community. An installed partner pack is Community until it has
been through review.

That matters commercially, not just technically — some deployments are
configured to activate Certified packs only, and will refuse anything below their
floor with a named reason. Talk to us early if you are targeting one of those.

## 6. Getting help

* Something you need is not expressible with a primitive — tell us the shape and
  the signal, not the workaround.
* A concept you need is missing from the vocabulary — the same.
* Your pack passes `check` but a customer's install refuses it — send the
  refusal, which always names the gate that stopped it.
