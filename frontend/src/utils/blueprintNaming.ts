type ConnectorIdentity = {
  id: string;
  status: string;
};

export const AGENT_BLUEPRINT_LABEL = 'Agent Blueprint';
export const AGENTFORCE_BLUEPRINT_LABEL = 'Agentforce Blueprint';

/** Salesforce branding applies only to an actively connected Salesforce org. */
export function isSalesforceConnected(
  connectors: readonly ConnectorIdentity[],
): boolean {
  return connectors.some(
    (connector) =>
      connector.id === 'salesforce' && connector.status === 'connected',
  );
}

/** Resolve the customer-facing blueprint name without changing blueprint logic. */
export function getBlueprintLabel(salesforceConnected: boolean): string {
  return salesforceConnected
    ? AGENTFORCE_BLUEPRINT_LABEL
    : AGENT_BLUEPRINT_LABEL;
}
