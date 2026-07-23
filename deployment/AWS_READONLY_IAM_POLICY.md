# AgentIQ AWS Event Connector — Read-Only IAM Policy (Partner Security Artifact)

**Scope:** MSP-B1 native AWS event connector (`aws_events`). **Audience:** the
customer/partner security team reviewing exactly what AgentIQ is granted in your
AWS accounts before you provision access.

AgentIQ ingests three **operational** event surfaces and nothing else. This
document and the machine-readable [`aws_readonly_iam_policy.json`](./aws_readonly_iam_policy.json)
are the authoritative statement of that access. The policy is **minimal** — every
action listed is a call the connector actually makes; there are no wildcard
actions and no write/delete permissions of any kind.

## Access model — one connection, many accounts

AgentIQ connects with a single **hub** identity and reads each managed account by
assuming a **read-only role** in that account via `sts:AssumeRole`. Each account
is an independent *scope*; AgentIQ never holds standing credentials in your
accounts, only short-lived STS session credentials minted per run.

```
        AgentIQ (hub identity, keys in AgentIQ's encrypted vault)
                       │  sts:AssumeRole (ExternalId-gated)
        ┌──────────────┼───────────────┐
        ▼              ▼                ▼
   Account A       Account B        Account C
 AgentIQReadOnly  AgentIQReadOnly  AgentIQReadOnly   ← per-account read-only role
   (this policy)    (this policy)    (this policy)
```

**Fallback:** for an account that cannot offer a cross-account role, AgentIQ can
instead use direct per-account read-only access keys (same three read-only
actions). These keys, like the hub keys, live only in AgentIQ's Fernet-encrypted
credential vault — never in configuration files, never logged.

## What is granted (exactly)

### 1. Per-account read-only role (`read_only_role_policy`)

Attach to the role AgentIQ assumes in each managed account.

| Action | Why | Surface |
|---|---|---|
| `cloudwatch:DescribeAlarmHistory` | Read alarm state-change history | CloudWatch alarms |
| `events:ListRules` | Enumerate the (bounded) scoped EventBridge rules | EventBridge |
| `events:DescribeRule` | Read a scoped rule's configuration | EventBridge |
| `cloudtrail:LookupEvents` | Read management-event history | CloudTrail |

No other actions. No `Get*`/`List*` beyond the above. No data-plane access (no S3
object reads, no DynamoDB, no Secrets Manager, etc.). Read-only: nothing here can
create, modify, or delete a resource.

> **EventBridge note.** The connector reads the *bounded* rule inventory via
> `Describe*/List*`. The EventBridge bus itself exposes no API to read past event
> payloads; full event-stream history (via an Archive replay) is a separate,
> explicitly-scoped follow-up and would be reviewed as its own grant.

Tighten the `Resource` ARNs to your managed accounts/regions/rule prefixes as your
policy requires; the connector does not depend on `Resource: "*"`.

### 2. Hub assume-role policy (`hub_assume_role_policy`)

Attach to the hub identity. Grants only `sts:AssumeRole`, scoped to the read-only
role ARNs you provision.

### 3. Account role trust policy (`account_role_trust_policy`)

Attach as the trust relationship on each per-account role. Allows **only** the hub
identity to assume it, gated by a shared `ExternalId` (the AWS confused-deputy
guard). Replace `HUB_ACCOUNT_ID` and `EXTERNAL_ID`.

## Provisioning checklist

1. In each managed account, create role `AgentIQReadOnlyEvents` with the
   **read-only role policy** attached and the **trust policy** allowing the hub
   account + your `ExternalId`.
2. Attach the **hub assume-role policy** to the hub identity, scoped to those role
   ARNs.
3. Provide AgentIQ the hub access key (stored in AgentIQ's vault), the per-account
   role ARNs, regions, and the `ExternalId` — via the connector's configuration
   (role ARNs/regions are non-secret; keys go to the vault only).

## Security-review sign-off (AC9)

This artifact must be reviewed and signed off by **someone other than its author**
before access is granted — that independent review is the acceptance gate.

| | Name | Date |
|---|---|---|
| Author | | |
| Reviewer (not the author) | | |
| Notes | | |
