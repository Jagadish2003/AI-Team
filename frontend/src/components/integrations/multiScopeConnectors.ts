/**
 * MSP-B13 (AT-743) — field/copy configuration for the multi-scope cloud
 * connectors onboarded through the Integration Hub: the AWS Event Connector
 * (`aws_events`, MSP-B1) and the Azure Event Connector (`azure_events`, MSP-B2).
 *
 * This is the FRONTEND source of truth for the onboarding copy. It mirrors the
 * secret-free config each connector already documents:
 *   - AWS  — hub access key + secret (vaulted under `aws_events`), each scope an
 *            account `{account_id, role_arn?, external_id?, regions[]}`
 *            (`AWS_EVENT_ACCOUNTS`, discovery/ingest/aws_auth.py).
 *   - Azure — a service principal (tenant/client id + client secret, vaulted),
 *            each scope a pinned subscription (`AZURE_EVENT_CONFIG`,
 *            discovery/ingest/azure_events.py).
 *
 * Every credential field is marked `secret: true` where it must be write-only
 * (T1-AC2): the value is sent to the vault and never read back. Non-secret
 * fields (access key id, tenant id, region list) are ordinary text.
 */
import { MultiScopeConnectorConfig } from '../../types/multiScopeConnector';

export const AWS_EVENTS_CONFIG: MultiScopeConnectorConfig = {
  connectorId: 'aws_events',
  name: 'AWS Events',
  description:
    'Read-only CloudWatch alarm-state, EventBridge rule, and CloudTrail ' +
    'management events across your AWS accounts. One connection, many accounts.',
  scopeNoun: 'account',
  scopeNounPlural: 'accounts',
  credentialHint:
    'Hub access keys are encrypted into your workspace vault and never shown ' +
    'again. Each account is reached read-only via role assumption or direct keys.',
  credentialFields: [
    {
      key: 'partition',
      label: 'Partition',
      // MSP-B1 aws_partitions: commercial `aws` and GovCloud (US) `aws-us-gov`.
      options: [
        { value: 'aws', label: 'AWS Commercial (aws)' },
        { value: 'aws-us-gov', label: 'AWS GovCloud US (aws-us-gov)' },
      ],
      defaultValue: 'aws',
      hint: 'GovCloud uses regional STS and arn:aws-us-gov ARNs.',
      required: true,
    },
    {
      key: 'access_key_id',
      label: 'Hub access key ID',
      placeholder: 'AKIA…',
      required: true,
    },
    {
      key: 'secret_access_key',
      label: 'Hub secret access key',
      placeholder: 'Enter secret access key',
      secret: true,
      required: true,
    },
    {
      key: 'session_token',
      label: 'Session token (optional)',
      placeholder: 'For temporary STS credentials',
      secret: true,
      required: false,
    },
  ],
  scopeFields: [
    {
      key: 'account_id',
      label: 'Account ID',
      placeholder: '123456789012',
      required: true,
    },
    {
      key: 'label',
      label: 'Label (optional)',
      placeholder: 'Production',
      required: false,
    },
    {
      key: 'role_arn',
      label: 'Read-only role ARN (optional)',
      placeholder: 'arn:aws:iam::123456789012:role/AgentIQReadOnly',
      hint: 'Leave blank to use direct per-account keys instead of role assumption.',
      required: false,
    },
    {
      key: 'external_id',
      label: 'External ID (optional)',
      placeholder: 'ExternalId used to gate the AssumeRole',
      required: false,
    },
    {
      key: 'regions',
      label: 'Regions',
      placeholder: 'us-east-1, us-west-2',
      hint: 'Comma-separated. GovCloud regions (us-gov-*) are supported.',
      required: true,
    },
  ],
};

export const AZURE_EVENTS_CONFIG: MultiScopeConnectorConfig = {
  connectorId: 'azure_events',
  name: 'Azure Events',
  description:
    'Read-only Azure Monitor alerts, Administrative activity-log events, and ' +
    'Service Health across your subscriptions. One connection, many subscriptions.',
  scopeNoun: 'subscription',
  scopeNounPlural: 'subscriptions',
  credentialHint:
    'The service principal is encrypted into your workspace vault and never ' +
    'shown again. Only explicitly pinned subscriptions are ever ingested.',
  credentialFields: [
    {
      key: 'environment',
      label: 'Environment',
      // MSP-B2 azure_environments: public cloud and US Government sovereign cloud.
      options: [
        { value: 'AzureCloud', label: 'Azure Public (AzureCloud)' },
        { value: 'AzureUSGovernment', label: 'Azure US Government' },
      ],
      defaultValue: 'AzureCloud',
      hint: 'Selects the login + ARM endpoints for the sovereign cloud.',
      required: true,
    },
    {
      key: 'mode',
      label: 'Access mode',
      // azure_events_config: Lighthouse (delegated) vs direct per-tenant SP.
      options: [
        { value: 'direct', label: 'Direct (per-tenant service principal)' },
        { value: 'lighthouse', label: 'Lighthouse (delegated)' },
      ],
      defaultValue: 'direct',
      required: true,
    },
    {
      key: 'tenant_id',
      label: 'Tenant ID',
      placeholder: '00000000-0000-0000-0000-000000000000',
      required: true,
    },
    {
      key: 'client_id',
      label: 'Client (application) ID',
      placeholder: '00000000-0000-0000-0000-000000000000',
      required: true,
    },
    {
      key: 'client_secret',
      label: 'Client secret',
      placeholder: 'Enter client secret',
      secret: true,
      required: true,
    },
  ],
  scopeFields: [
    {
      key: 'subscription_id',
      label: 'Subscription ID',
      placeholder: '00000000-0000-0000-0000-000000000000',
      required: true,
    },
    {
      key: 'label',
      label: 'Display name (optional)',
      placeholder: 'Production',
      required: false,
    },
  ],
};

/** All multi-scope connector configs keyed by connector id. */
export const MULTI_SCOPE_CONNECTORS: Record<string, MultiScopeConnectorConfig> = {
  aws_events: AWS_EVENTS_CONFIG,
  azure_events: AZURE_EVENTS_CONFIG,
};

/** True when a connector is onboarded through the multi-scope card. */
export function isMultiScopeConnector(connectorId: string): boolean {
  return connectorId in MULTI_SCOPE_CONNECTORS;
}

/** Config for a multi-scope connector, or null if it is not one. */
export function multiScopeConnectorConfig(
  connectorId: string,
): MultiScopeConnectorConfig | null {
  return MULTI_SCOPE_CONNECTORS[connectorId] ?? null;
}
