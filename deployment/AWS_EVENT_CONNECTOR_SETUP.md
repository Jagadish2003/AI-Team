# AWS Event Connector — setup & live-data requirements (MSP-B1)

What the native AWS Event Connector needs before a discovery run ingests real AWS
events, and exactly which pieces require **live customer data** rather than
configuration we can ship.

Companion artifacts: `aws_readonly_iam_policy.json` (the minimal read-only IAM
policy, importable as-is) and `AWS_READONLY_IAM_POLICY.md` (permission-by-capability
mapping + security-review checklist).

---

## 1. What runs today with no AWS account

Offline is the default (`INGEST_MODE` unset or not `live`). The connector reads
`backend/discovery/ingest/fixtures/aws_native_events_sample.json` — a deterministic
two-account demo estate whose recurrence volumes deliberately clear the MSP-B7
noise floors, so an offline run reaches the Cloud Operations detectors. No
credentials, no network, no configuration.

Nothing below is needed for that path. Everything below is needed for a **live**
run.

---

## 2. LIVE DATA NEEDED — the checklist

| # | What we need from the customer | Where it goes | Blocking? |
|---|---|---|---|
| 1 | **Hub access key id + secret** for the management ("hub") identity | Integration Hub → AWS Events card → Connect. Vaulted per org under connector id `aws_events`. Never in `.env`. | Yes |
| 2 | **Partition** — `aws` (commercial) or `aws-us-gov` (GovCloud) | Selected on the Connect form; stored non-secret on the connector record | Yes |
| 3 | **One read-only IAM role per managed account**, trusting the hub identity | Customer-side. Policy: `deployment/aws_readonly_iam_policy.json` | Yes |
| 4 | **Role ARN** for each managed account | Integration Hub → AWS Events → Add account (pin a scope) | Yes |
| 5 | **STS ExternalId**, if the role's trust policy requires one | Same pin form. Persisted as config so it reaches every AssumeRole call | Only if the trust policy requires it |
| 6 | **Regions** to poll per account | Same pin form | Recommended (GovCloud: required) |
| 7 | *Alternative to 3–5:* **direct per-account access keys** | Same pin form. Vaulted under `aws_events:account:{account_id}` | Only when an account cannot offer a cross-account role |

A scope only ingests once an Owner **pins** it. Reachable-but-unpinned accounts are
never polled — the connected estate never grows on its own.

### Validation happens before anything is saved

Both the Connect form and each account pin run a live probe first
(`sts:GetCallerIdentity`, then `sts:AssumeRole` for a role-based account). A
credential AWS rejects is **never vaulted** and the connector is **not** marked
connected — it reports the provider-specific reason instead (bad key, expired
token, un-assumable role ARN, wrong partition). A connector that reads *Connected*
has been proven against AWS.

---

## 3. What the connector reads (V1 scope)

Exactly three event classes, per the MSP-B1 scope defence:

| Surface | API call | What it yields |
|---|---|---|
| CloudWatch | `cloudwatch:DescribeAlarmHistory` (`HistoryItemType=StateUpdate`) | Alarm **state changes** only |
| EventBridge | `events:ListRules` + `events:DescribeRule` | The **bounded rule set** and every change to it — see §4 |
| CloudTrail | `cloudtrail:LookupEvents` | **Management (audit)** events only |

Explicitly **not** ingested: CloudWatch metrics, CloudWatch Logs, CloudTrail data
events, CloudTrail Insight events, GuardDuty, Security Hub. Those are
monitoring-tool territory; widening is a new story, not a configuration change.

The connector is **outbound-only** — polling, no push infrastructure of any kind —
so it works unchanged under `NETWORK_PROFILE=no_public_inbound`.

---

## 4. EventBridge — what this surface can and cannot see

Worth stating plainly, because the name invites a wrong expectation.

The minimal read-only grant (`events:Describe*/List*`) reads **rule
configuration**. The EventBridge bus itself exposes **no read API for past
events** — there is no "list the events that flowed through this bus" call. So
what this surface honestly contributes is the bounded rule set as observed
operational state, plus every subsequent rule change (a rule appearing, being
modified, or being disabled is a real operational event). An unchanged rule set
re-reads nothing.

**LIVE DATA / EXTRA GRANT NEEDED to go further.** A true EventBridge *event
stream* requires a customer-side estate change — either:

* route the bounded rules to a durable target the connector can then read
  (a CloudWatch Logs log group, S3, or Firehose), or
* create an EventBridge Archive and replay it.

Both go beyond the read-only policy we ship and are deliberately out of V1 scope.

---

## 5. GovCloud (`aws-us-gov`)

Partition-aware from day one. GovCloud resolves `us-gov-*` endpoints and
`arn:aws-us-gov:…` ARNs, and has no global STS endpoint — so a GovCloud connection
with no explicit region defaults to `us-gov-west-1` rather than falling back to the
commercial endpoint. A region that contradicts the stated partition is rejected at
configuration time.

**LIVE DATA NEEDED:** a real GovCloud account is required to verify this
end-to-end. That is the MSP-B9 live-verification item; FIPS endpoint variants (a
common GovCloud compliance requirement) are handled as part of the same
follow-through. Everything up to that point is verified at the configuration level.

---

## 6. Failure behaviour (what the customer will see)

Per-account, and loud — never a silent skip:

* a revoked role or expired credential marks **that account** `auth_failed` while
  every other account continues ingesting;
* throttling backs off, retries, and is counted — it never thins the data quietly;
* each account's outcome appears both in run health and on its scope in the
  Integration Hub card, using the same vocabulary in both places.

A backlog larger than one poll is drained across successive polls rather than
truncated: CloudWatch is read oldest-first, and CloudTrail (which the API returns
newest-first) walks backwards through the remaining window. Total volume per run is
bounded by the MSP-B7 event budget, which defers loudly and reports what it
deferred.

---

## 7. Configuration reference

Everyday path is the Integration Hub; the env var is a per-deployment override that
takes precedence over it.

```bash
# Optional operator override. NON-SECRET only — inline AWS keys are rejected.
# Either a plain array (applies to every org) or an object keyed by org id
# (with a "default"/"*" fallback).
AWS_EVENT_ACCOUNTS='[
  {"account_id":"111122223333",
   "role_arn":"arn:aws:iam::111122223333:role/AgentIQReadOnly",
   "external_id":"<from the customer>",
   "regions":["us-east-1"]}
]'
```

CLI/standalone only, and **refused in production** (`ENVIRONMENT=production` or
`REQUIRE_CONNECTOR_SECRETS=1`), where the hub key must come from the vault:

```bash
AWS_EVENTS_HUB_ACCESS_KEY_ID=...
AWS_EVENTS_HUB_SECRET_ACCESS_KEY=...
AWS_EVENTS_HUB_SESSION_TOKEN=...
```

## 8. Turning it on for a run

A discovery run polls AWS when **both** hold:

1. `aws_events` is among the run's connected systems — automatic for a live run
   once the connector is connected and at least one account is reachable; and
2. a **`cloud_ops` pack** is selected, so the events are actually consumed.

Its records join the MSP-B8 bridge and the MSP-B2 Azure connector in one Cloud
Operations assembly, where identical event signatures fold together — a natively
ingested event and its bridged twin never double-count.
