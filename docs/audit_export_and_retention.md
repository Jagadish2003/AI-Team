# Audit export and retention (2.0-D4 T2)

Covers D4 AC2 (audit records cannot be updated or deleted through any application
path) and AC3 (signed export verifies; alteration fails verification; every export
is itself audited).

Read this before changing anything in `app/audit_export.py`,
`app/export_signing.py`, `app/routes_audit_export.py`, or the `audit_log` grants.

---

## 1. What the export is for

The only audit read surface before this work was `GET /api/runs/{run_id}/audit` —
Owner-gated, and scoped to a single run. That answers "what happened in this
discovery run?" and cannot answer the question an enterprise security review
actually asks:

> Show me every state-changing action in this organisation between these two dates,
> and prove the file you gave me is the file the system produced.

The export exists for that second question, which is why it differs from the
run-scoped route in three specific ways.

### 1.1 A period, not a run

`POST /api/audit/export` takes `from` and `to`. A plain date on `to` covers the
whole day (`to=2026-07-20` includes every event on the 20th), because that is what
an auditor means and a naive implementation gets it wrong by including only the
first instant of the day.

### 1.2 Org scoping enforced in the query

`WHERE org_id = %s` is in the SQL, following the pattern the 2.0-A2 and A3 stores
established. Isolation asserted *after* retrieval is not isolation: the rows have
already been read, and one bug between the read and the filter discloses another
tenant's audit trail. The organisation comes from the tenancy middleware and there
is deliberately **no `org_id` field on the request body** — a caller-supplied org on
an audit export is a cross-tenant disclosure waiting to happen.

### 1.3 A signature over the exported bytes

See §2. The signature is what makes the file evidence rather than a report.

### 1.4 Access control

Owner-only, consistent with the existing run-audit route.

### 1.5 Exporting is itself audited

Generating an export mutates nothing and is still a state-changing action from a
compliance standpoint: someone took a copy of the organisation's audit trail out of
the system. That is a **disclosure**, and a disclosure is exactly what an auditor
expects to find recorded. So the export emits `audit_export_generated`.

This is genuinely recursive, on purpose: a later export over an overlapping period
contains the record of the earlier one, which is how "who has read this trail
before?" becomes answerable.

Two ordering consequences, stated rather than left to be discovered:

* The audit row is written **before** the payload is assembled, so a disclosure can
  never go unrecorded because serialisation failed after the read.
* Therefore an export **never contains its own** generation record — only those of
  previous exports.

A refused export (bad period, or no signing key) is *also* audited, with
`outcome: failure`. An auditor should see that an export was attempted, not only
that one succeeded.

Because the route is a `POST`, it falls inside the set of routes the 2.0-D4 T1
conformance sweep enumerates — so "the export must itself be audited" is not merely
implemented, it is **enforced in CI**. A change that dropped the emission would fail
the sweep rather than quietly removing the record.

---

## 2. Signing

### 2.1 Ed25519, reused not re-chosen

`app/licensing.py` already verifies licence keys with Ed25519 via `cryptography`,
including `load_trusted_key_set()` for multiple trusted keys. The export uses the
same asymmetric scheme.

**Why not an HMAC.** With a shared secret, any signature the customer can verify is
also a signature the vendor could have produced, so the strongest available claim is
"CloudFulcrum says this file is authentic". With Ed25519 the private half never
leaves the deployment, so the auditor verifies the **deployment's** attestation
using a published public key, independently of both the transport and the vendor.
That independence is the entire point.

### 2.2 One signing module, deliberately shared

`app/export_signing.py` is a shared module, not a helper inside the audit export,
because 2.0-B1's evidence-bundle export needs the identical guarantee (B1 specifies
"altering any byte fails verification" — the same property). **A platform with two
signature schemes has a weakest one, and reviewers find it.** B1 should import this
module rather than grow a second scheme.

At the time of writing B1 has not landed; there is no evidence-export module on this
branch, so this is the first and only signing scheme.

### 2.3 Key separation — the deliberate answer

Audit exports are signed by a **different key** from licences. These are different
capabilities held by different parties:

| | Licence signing | Audit-export signing |
|---|---|---|
| Held by | CloudFulcrum (vendor) | the customer's deployment |
| Key location | AWS Secrets Manager, never in a customer install | the customer's deployment config |
| Attests | "CloudFulcrum issued this entitlement" | "this installation produced this file" |

Sharing one key would either put licence-minting power inside every customer
deployment, or make every audit export a vendor attestation the customer cannot
produce alone. Both are worse than managing two keys.

### 2.4 Configuration

| Variable | Meaning |
|----------|---------|
| `AUDIT_EXPORT_SIGNING_KEY` | the deployment's **private** Ed25519 key (PEM, PKCS#8) |
| `AUDIT_EXPORT_PUBLIC_KEY` | the matching **public** key (PEM), published to the auditor |

Both are resolved live per call, so rotation is a config change. There is **no
baked-in default and no fallback**: an unconfigured deployment cannot sign, and the
export fails with `503` rather than returning an unsigned artifact that looks
signed. A silent downgrade to "no signature" is the one failure mode a compliance
export must not have.

Generate a pair with `app.export_signing.generate_key_pair()`.

### 2.5 What is signed

The signature covers the canonical bytes of the **payload only**, never the
envelope — so verification reconstructs exactly what was signed without having to
strip its own signature back out, a step that is easy to get subtly wrong and
impossible to notice when it is wrong.

Canonical bytes are deterministic by construction: sorted keys, fixed separators,
UTF-8. Any non-determinism in serialisation would make a genuine export fail its own
verification.

The envelope also records a SHA-256 `content_sha256`. That is **not** a substitute
for the signature — anyone altering the content can recompute a digest. It is there
so a reader can tell *which* part failed: a digest mismatch means the file was
truncated or re-encoded in transit; a signature mismatch with a matching digest
means the content was deliberately rewritten.

`SIGNATURE_VERSION` is checked on verification, so a signature made under a
different signed-bytes rule can never verify under this one even if the maths
happens to work out.

### 2.6 Bounded, and loud about it

`MAX_EXPORT_ROWS` (50,000) caps one export. When the cap is hit the export is still
produced but carries `complete: false` and a `truncated` block naming the limit and
the remedy. A truncated audit trail presented as a complete one is the failure this
avoids — the same loud-degradation rule MSP-B7 applies to event volume.

---

## 3. Immutability (AC2)

### 3.1 Two halves, because documented and applied are different things

AC2 asks for a data-layer test. The honest reading is two independent checks,
because they catch different failures:

| Check | Catches |
|-------|---------|
| The store issues no `UPDATE`/`DELETE` against `audit_log` (source sweep) | a future code change |
| The deployed role genuinely lacks the privilege | a provisioning script that never ran |

`tests/unit/test_audit_log_immutability.py` implements both, following the source-
sweep precedent of `tests/unit/test_opportunity_baseline_immutability.py`.

### 3.2 What we found

The second half mattered. `database/models/audit_log.py` had documented the posture
since AT-82:

```sql
REVOKE UPDATE, DELETE ON audit_log FROM app_user;
GRANT INSERT, SELECT ON audit_log TO app_user;
```

…and it had **never been applied**. `provision.sql` contained no `REVOKE` for
`audit_log` at all, and the application role held `UPDATE`, `DELETE` **and**
`TRUNCATE`. So "audit records cannot be updated or deleted through any application
path" was true of the code and false of the database.

Migration `0038` and the `provision.sql` section now apply it.

### 3.3 The ownership limitation

In PostgreSQL a table's **owner** can re-grant itself anything, so `REVOKE` against
the owning role is advisory rather than binding. Grant-level immutability only
genuinely binds when the application role does **not** own `audit_log`.

The correct provisioning is therefore:

1. create `audit_log` as a migration/DBA role;
2. grant the application role `INSERT, SELECT` and nothing else;
3. never make the application role the owner.

This cannot be fixed from inside a migration that runs *as* the application role, so
the immutability test **reports the ownership caveat explicitly** rather than passing
and implying protection that is not there.

---

## 4. Retention

### 4.1 The tension, stated honestly

An append-only table with a retention policy is a contradiction — unless the
deletion path is deliberately **outside the application**. It is, and that is the
design, not an omission:

> The application role has `INSERT` and `SELECT` on `audit_log` and nothing else.
> Retention deletion is performed by a **separate database role** the application
> does not hold credentials for, on a schedule owned by operations.

This is how most regulated systems do it. The alternative reading — that the
platform accumulates audit rows forever and quietly becomes a storage problem — is
what this section exists to rule out.

### 4.2 The configuration

| Question | Answer |
|----------|--------|
| **How long?** | 7 years (2,555 days) by default, `AUDIT_RETENTION_DAYS`. Chosen to exceed the usual financial-services and federal record-retention floors; a deployment with a shorter contractual obligation may lower it, and one under litigation hold should raise it or suspend the job. |
| **Enforced by what?** | A scheduled database job (cron / pgAgent / managed scheduler), **not** application code. There is no in-application deletion path and no route that deletes audit rows. |
| **Run as whom?** | A dedicated `audit_retention` role holding `DELETE ON audit_log` — a privilege the application role does not have. |
| **Recorded how?** | The job writes its own summary (rows removed, cutoff date, duration) to operational logging. It deliberately does **not** write to `audit_log`: a retention job that appends to the table it prunes is a job that can never finish tidily. |

Reference job:

```sql
-- Run as the audit_retention role, NOT as the application role.
DELETE FROM audit_log
WHERE timestamp < (now() - interval '2555 days')::text;
```

The comparison is against `timestamp`'s TEXT form because the column is TEXT (the
table is SQLite-compatible) and rows are written as
`datetime.now(timezone.utc).isoformat()`, so ISO-8601 UTC strings order correctly.

### 4.3 What is deliberately NOT built

* No in-application retention job. Adding one would require giving the application
  `DELETE`, which is the privilege AC2 exists to remove.
* No `DELETE` route, at any role.
* No automatic export-before-delete. If a deployment needs the pruned window
  retained, it exports first (§1) and stores the signed file — which is exactly what
  the signature is for.

### 4.4 Interaction with the export

Retention bounds what an export can cover. An export whose period starts before the
retention cutoff returns only the rows that still exist, and reports the count it
found. It does **not** claim the period was empty. If a customer needs a window
preserved beyond retention, the signed export is the artifact to keep — it verifies
independently long after the rows are gone.
