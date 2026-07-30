/**
 * Pre-launch connector token-expiry guard (Stack Builder).
 *
 * Before a discovery run starts, connectors whose OAuth token has expired must
 * block the launch (the run would silently return no data from them) and the user
 * is told which ones to reconnect. These tests pin the decision logic:
 *   - which run-used connectors are worth an expiry check (catalog-engaged only), and
 *   - which live token statuses count as "expired" (needs_auth / refresh_failed).
 */
import { describe, expect, it } from 'vitest';
import {
  connectorsToCheckForExpiry,
  expiredConnectors,
  connectorDisplayName,
} from '../pages/StackBuilderPage';
import type {
  WorkspaceCatalogResponse,
  CatalogSystemItem,
  CatalogSystemStatus,
} from '../types/workspace_catalog';
import type { TokenStatus } from '../services/staticApi';

function sys(system_id: string, status: CatalogSystemStatus): CatalogSystemItem {
  return { system_id, name: system_id, status, products: [] };
}

function catalog(items: CatalogSystemItem[]): WorkspaceCatalogResponse {
  return {
    primary_platforms: items,
    operational_systems: [],
    comms_knowledge: [],
    data_engineering: [],
    missing_categories: [],
  };
}

describe('connectorsToCheckForExpiry', () => {
  it('checks only connectors the workspace has engaged (connected or needs_auth)', () => {
    const cat = catalog([
      sys('salesforce', 'connected'),
      sys('jira', 'needs_auth'),
      sys('servicenow', 'not_configured'),
    ]);
    const result = connectorsToCheckForExpiry(
      ['salesforce', 'jira', 'servicenow', 'github'],
      cat,
    );
    // servicenow (not_configured) and github (absent) are ignored.
    expect(result).toEqual(['salesforce', 'jira']);
  });

  it('returns nothing when there is no catalog', () => {
    expect(connectorsToCheckForExpiry(['salesforce'], null)).toEqual([]);
  });
});

describe('expiredConnectors', () => {
  it('flags needs_auth and refresh_failed, ignores healthy/refreshable/unknown', () => {
    const statuses: Array<{ id: string; status: TokenStatus | null }> = [
      { id: 'salesforce', status: 'refresh_failed' },
      { id: 'jira', status: 'connected' },
      { id: 'servicenow', status: 'needs_auth' },
      { id: 'slack', status: 'needs_refresh' }, // refreshable → NOT expired
      { id: 'github', status: null }, // status unknown → not blocked
    ];
    expect(expiredConnectors(statuses)).toEqual(['salesforce', 'servicenow']);
  });

  it('returns empty when every connector is healthy', () => {
    expect(
      expiredConnectors([
        { id: 'a', status: 'connected' },
        { id: 'b', status: 'needs_refresh' },
      ]),
    ).toEqual([]);
  });
});

describe('connectorDisplayName', () => {
  it('uses friendly names for known connectors', () => {
    expect(connectorDisplayName('salesforce')).toBe('Salesforce');
    expect(connectorDisplayName('servicenow')).toBe('ServiceNow');
    expect(connectorDisplayName('teams')).toBe('Microsoft Teams');
  });

  it('title-cases an unknown connector id as a fallback', () => {
    expect(connectorDisplayName('oracle_db')).toBe('Oracle Db');
  });
});
