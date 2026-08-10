# Example Service Desk Operations — a worked AgentIQ pack

This is the worked example from the partner documentation
(`docs/partner/worked_example.md`), kept here as a real, runnable pack project
rather than a listing in a document. CI builds it end to end on every push, so
the walkthrough cannot describe something that no longer works.

```
pack.json                       the manifest — four detectors, no code
fixtures/01_busy_service_desk.json   every detector fires
fixtures/02_below_the_floor.json     the same shapes, under every floor: silence
fixtures/03_quiet_estate.json        an ordinary quiet month: silence
```

## Run it

From `backend/`:

```bash
python scripts/pack_sdk.py check discovery/packs/sdk/examples/example_service_desk
```

`check` is validate → fixtures → lint, in the order installation runs them.

To package it you need a publisher key of your own; the platform ships none, so
nothing here is signed:

```bash
export PACK_BUNDLE_SIGNING_KEY=<base64 32-byte ed25519 seed>
python scripts/pack_sdk.py package discovery/packs/sdk/examples/example_service_desk \
    --out ./example_service_desk.aiqpack --key-id my-key --allow-untrusted
```

## What it is meant to show

* Four detectors composed from four different primitives, over normalised
  concepts — no connector names, no code.
* Two negative fixtures. A detector that fires on everything passes a
  positive-only suite forever.
* Aggregation floors set above the schema's minimum, deliberately.
* Concentration wording that never claims causation.
* A certification level that is *requested*, never asserted.
