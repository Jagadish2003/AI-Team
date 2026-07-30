# Pack version archive — 2.0-C1 T3 (AT-828) rollback

This directory holds the **config artifact of each prior pack version that remains
available to run**. It is what makes rollback honest: a run pinned to `1.1.0` loads
`1.1.0`'s real config from here, rather than being *stamped* `1.1.0` while executing
the current version's behaviour.

Each file is the verbatim historical config for one version — recovered from git,
never hand-written. Its internal `packVersion` field must match its filename.

```
<pack_id>_pack_config.v<version>.json
```

| File | Version | Recovered from |
|------|---------|----------------|
| `cloud_ops_pack_config.v1.1.0.json` | 1.1.0 | `ae4d6f3e` (MSP-B6 T4, AT-739) |
| `security_ops_pack_config.v1.1.0.json` | 1.1.0 | `11e4aaf0` (MSP-B12 T2) |

The **current** version's config is not archived here — it lives at its normal path
(`discovery/packs/<pack_id>_pack_config.json`) and the registry's `config_path`
points at it. This directory holds *prior* versions only.

## The discipline: archive on bump

**When you bump a config-driven pack's `packVersion`, archive the OUTGOING config
here and add a `versionHistory` entry for it in `pack_config.py`** — in the same PR
as the bump. That is the only moment the outgoing artifact is trivially available;
afterwards it has to be dug out of git.

A version with no archived artifact is simply **not rollbackable**: `PUT
/api/packs/{id}/version` refuses it and names the versions that *are* available.
That refusal is correct behaviour, not a gap — it is the platform declining to
pretend it can serve behaviour it no longer has.

## What is deliberately NOT archived

`cloud_ops` and `security_ops` both had a `1.0.0`, but those were **scaffolds with
zero detectors** (MSP-B6 T1 / MSP-B12 T1 shipped the config schema and terminology
before the detectors landed). Rolling back to a version that produces no findings at
all would be a footgun dressed up as a feature, so `1.0.0` is not offered as a
rollback target even though its artifact is recoverable.

## Code-only packs

`service_cloud`, `ncino`, `strs_benefits`, `sqlserver_opsignal`,
`github_engineering`, and `enterprise_ops` keep their behaviour in code, not in an
external config artifact. There is nothing to archive, so they declare no
`versionHistory` and rollback is refused for them with that reason named. Making
them rollbackable would require externalising their calibration first — the same
step MSP-B6 T1 took for `cloud_ops`.
