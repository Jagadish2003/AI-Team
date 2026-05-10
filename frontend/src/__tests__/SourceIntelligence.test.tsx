/**
 * T41-4 v1.3 — Source Intelligence tests
 *
 * New tests added for three fixes:
 *   Issue 1: Connector ID as join key — source rows keyed by connectorId not name
 *   Issue 2: Resolved vs reviewed state distinction
 *   Issue 3: Backend tests — fieldId-based matching
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route, Navigate } from 'react-router-dom';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import type { MappingRow, PermissionRequirement } from '../types/normalization';

// ── Fixtures ──────────────────────────────────────────────────────────────────

const MAPPED_SN = {
  id: 'map_001', sourceSystem: 'ServiceNow', sourceType: 'CMDB',
  sourceField: 'cmdb_ci.name', commonEntity: 'Application',
  commonField: 'Application.name', status: 'MAPPED' as const,
  confidence: 'HIGH' as const, sampleValues: ['Jira', 'SAP'],
};
const MAPPED_JIRA = {
  id: 'map_002', sourceSystem: 'Jira', sourceType: 'Tickets',
  sourceField: 'issue.summary', commonEntity: 'Workflow',
  commonField: 'Workflow.summary', status: 'MAPPED' as const,
  confidence: 'MEDIUM' as const, sampleValues: [],
};
const AMBIGUOUS_SN = {
  id: 'map_003', sourceSystem: 'ServiceNow', sourceType: 'CMDB',
  sourceField: 'cmdb_ci.owner', commonEntity: 'Application',
  commonField: 'Application.owner', status: 'AMBIGUOUS' as const,
  confidence: 'MEDIUM' as const, sampleValues: ['Platform Eng', 'IT Ops'],
};
const AMBIGUOUS_JIRA = {
  id: 'map_004', sourceSystem: 'Jira', sourceType: 'Tickets',
  sourceField: 'issue.assignee', commonEntity: 'Workflow',
  commonField: 'Workflow.owner', status: 'AMBIGUOUS' as const,
  confidence: 'MEDIUM' as const, sampleValues: ['alice', 'bob'],
};
const UNMAPPED_SLACK = {
  id: 'map_005', sourceSystem: 'Slack', sourceType: 'Messages',
  sourceField: 'thread.topic', commonEntity: 'Workflow',
  commonField: 'Workflow.topic', status: 'UNMAPPED' as const,
  confidence: 'LOW' as const, sampleValues: ['handoff delay'],
};

// Slack connected but zero signals — Issue 1 test case
const CONNECTORS = [
  { id: 'servicenow', name: 'ServiceNow', status: 'connected', tier: 'recommended' },
  { id: 'jira',       name: 'Jira',       status: 'connected', tier: 'standard'   },
  { id: 'slack',      name: 'Slack',      status: 'connected', tier: 'standard'   },
];

const PERMISSIONS_OK = [
  { id: 'p1', label: 'Read CMDB', sourceSystem: 'ServiceNow', required: true, satisfied: true },
];
const PERMISSIONS_WARN = [
  { id: 'p2', label: 'Read incidents', sourceSystem: 'Jira', required: true, satisfied: false },
];
const PERMISSIONS_SLACK_OK = [
  { id: 'p3', label: 'Read channels', sourceSystem: 'Slack', required: true, satisfied: true },
];
const PERMISSIONS_SLACK_WARN = [
  { id: 'p4', label: 'Read channels', sourceSystem: 'Slack', required: true, satisfied: false },
];

// ── Mocks ─────────────────────────────────────────────────────────────────────

let mockRows: MappingRow[] = [MAPPED_SN, MAPPED_JIRA, AMBIGUOUS_SN];
let mockRowsLoading = false;
let mockCounts = { MAPPED: 2, UNMAPPED: 0, AMBIGUOUS: 1 };
let mockPermissions: PermissionRequirement[] = PERMISSIONS_OK;
let mockPermissionsLoading = false;
let mockPermissionsError: string | null = null;

vi.mock('../context/NormalizationContext', () => ({
  useNormalizationContext: () => ({
    get rows() { return mockRows; },
    get rowsLoading() { return mockRowsLoading; },
    get counts() { return mockCounts; },
    confidence: { level: 'MEDIUM', why: '', missingSignals: [], nextAction: '' },
    get permissions() { return mockPermissions; },
    get permissionsLoading() { return mockPermissionsLoading; },
    get permissionsError() { return mockPermissionsError; },
    relevantPermissions: [],
    refetchPermissions: vi.fn(),
    activeTab: 'MAPPED', setActiveTab: vi.fn(),
    search: '', setSearch: vi.fn(),
    sourceFilter: 'All Sources', setSourceFilter: vi.fn(),
    entityFilter: 'All Entities', setEntityFilter: vi.fn(),
    sortMode: 'Confidence High→Low', setSortMode: vi.fn(),
    selectedRowId: 'map_001', setSelectedRowId: vi.fn(),
    sources: ['All Sources', 'Jira', 'ServiceNow'],
    entities: ['All Entities', 'Application', 'Workflow'],
    filteredRows: [MAPPED_SN],
    selectedRow: MAPPED_SN,
  }),
}));

vi.mock('../context/ConnectorContext', () => ({
  useConnectorContext: () => ({ all: CONNECTORS }),
}));

vi.mock('../context/RunContext', () => ({
  useRunContext: () => ({ runId: 'run_001' }),
}));

vi.mock('../components/common/TopNav', () => ({ default: () => <nav /> }));
vi.mock('../components/normalization/MappingTable', () => ({ default: () => <div data-testid="mapping-table" /> }));
vi.mock('../components/normalization/FieldDetailsPanel', () => ({ default: () => <div data-testid="field-details" /> }));

import SourceIntelligencePage from '../pages/SourceIntelligencePage';

function renderPage(path = '/source-intelligence') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/source-intelligence" element={<SourceIntelligencePage />} />
        <Route path="/normalization" element={<Navigate to="/source-intelligence" replace />} />
      </Routes>
    </MemoryRouter>,
  );
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('SourceIntelligencePage v1.3 — T41-4', () => {

  beforeEach(() => {
    vi.clearAllMocks();
    mockRows = [MAPPED_SN, MAPPED_JIRA, AMBIGUOUS_SN];
    mockRowsLoading = false;
    mockCounts = { MAPPED: 2, UNMAPPED: 0, AMBIGUOUS: 1 };
    mockPermissions = PERMISSIONS_OK;
    mockPermissionsLoading = false;
    mockPermissionsError = null;
  });

  // ── Basic rendering ──────────────────────────────────────────────────────

  it('SI1: page title is Source Intelligence', () => {
    renderPage();
    expect(screen.getByTestId('page-title').textContent).toBe('Source Intelligence');
  });

  it('SI1: /normalization redirects to /source-intelligence', () => {
    renderPage('/normalization');
    expect(screen.getByTestId('page-title').textContent).toBe('Source Intelligence');
  });

  it('SI1: loading state keeps Source Intelligence heading and subtext visible', () => {
    mockRowsLoading = true;
    renderPage();
    expect(screen.getByTestId('page-title').textContent).toBe('Source Intelligence');
    expect(screen.getByText(/How AgentIQ understood your connected sources/)).toBeTruthy();
    expect(screen.getByText('Loading Source Intelligence')).toBeTruthy();
    expect(screen.getByText(/Reading source mappings/)).toBeTruthy();
  });

  it('SI2: three stat cards render with correct labels', () => {
    renderPage();
    expect(screen.getAllByTestId('stat-card').length).toBe(3);
    expect(screen.getByText('Connected sources')).toBeTruthy();
    expect(screen.getByText('Fields mapped with HIGH confidence')).toBeTruthy();
    expect(screen.getByText('Fields needing your review')).toBeTruthy();
  });

  // ── Issue 1: connector ID as join key ────────────────────────────────────

  it('I1: source rows keyed by connectorId in data-testid', () => {
    renderPage();
    // Rows use connectorId (e.g. 'servicenow') not display name ('ServiceNow')
    expect(screen.getByTestId('source-row-servicenow')).toBeTruthy();
    expect(screen.getByTestId('source-row-jira')).toBeTruthy();
    expect(screen.getByTestId('source-row-slack')).toBeTruthy();
  });

  it('I1: Slack row appears despite zero signals (connected-but-empty)', () => {
    renderPage();
    const slackRow = screen.getByTestId('source-row-slack');
    expect(slackRow.textContent).toContain('Status unknown');
  });

  it('SI2: Permission-limited sub-state shown for zero-signal permission warning', () => {
    mockRows = [MAPPED_SN, MAPPED_JIRA];
    mockCounts = { MAPPED: 2, UNMAPPED: 0, AMBIGUOUS: 0 };
    mockPermissions = PERMISSIONS_SLACK_WARN;
    renderPage();
    expect(screen.getByTestId('zero-signal-slack').textContent).toContain('Permission-limited');
  });

  it('SI3: No signals detected shown when zero-signal source is confirmed', () => {
    mockRows = [MAPPED_SN, MAPPED_JIRA];
    mockCounts = { MAPPED: 2, UNMAPPED: 0, AMBIGUOUS: 0 };
    mockPermissions = PERMISSIONS_SLACK_OK;
    renderPage();
    expect(screen.getByTestId('zero-signal-slack').textContent).toContain('No signals detected');
  });

  it('SI4: Checking shown while permissions are loading', () => {
    mockRows = [MAPPED_SN, MAPPED_JIRA];
    mockCounts = { MAPPED: 2, UNMAPPED: 0, AMBIGUOUS: 0 };
    mockPermissionsLoading = true;
    renderPage();
    expect(screen.getByTestId('zero-signal-slack').textContent).toContain('Checking');
  });

  it('SI4: Not yet fully analyzed shown when a zero-signal source has unmapped fields', () => {
    mockRows = [MAPPED_SN, MAPPED_JIRA, UNMAPPED_SLACK];
    mockCounts = { MAPPED: 2, UNMAPPED: 1, AMBIGUOUS: 0 };
    mockPermissions = PERMISSIONS_SLACK_OK;
    renderPage();
    expect(screen.getByTestId('zero-signal-slack').textContent).toContain('Not yet fully analyzed');
  });

  it('SI5: no zero-reason element rendered for sources with mapped signals', () => {
    renderPage();
    expect(screen.queryByTestId('zero-signal-servicenow')).toBeNull();
  });

  it('I1: ServiceNow row shows Permissions confirmed (joined via sourceKey)', () => {
    mockPermissions = PERMISSIONS_OK; // satisfied=true for ServiceNow
    renderPage();
    const snRow = screen.getByTestId('source-row-servicenow');
    expect(snRow.textContent).toContain('Confirmed');
  });

  it('I1: Jira row shows Check permissions when required perm unsatisfied', () => {
    mockPermissions = PERMISSIONS_WARN; // satisfied=false for Jira
    renderPage();
    const jiraRow = screen.getByTestId('source-row-jira');
    expect(jiraRow.textContent).toContain('Check permissions');
  });

  it('I1: loading state propagates to all source rows', () => {
    mockPermissionsLoading = true;
    renderPage();
    const snRow = screen.getByTestId('source-row-servicenow');
    expect(snRow.textContent).toContain('Loading');
  });

  it('I1: unknown state when permissions API errors', () => {
    mockPermissionsError = 'API failed';
    renderPage();
    const snRow = screen.getByTestId('source-row-servicenow');
    expect(snRow.textContent).toContain('Not assessed');
  });

  // ── Issue 2: resolved vs reviewed ────────────────────────────────────────

  it('I2: resolved state shown when all cards confirmed (no dismissals)', () => {
    renderPage();
    // Confirm the one ambiguous card
    fireEvent.click(screen.getByTestId('ambiguous-option-map_003-Application'));
    fireEvent.click(screen.getByTestId('ambiguous-confirm-map_003'));
    expect(screen.getByTestId('state-resolved')).toBeTruthy();
    expect(screen.queryByTestId('state-reviewed')).toBeNull();
  });

  it('I2: reviewed state shown when any card dismissed', () => {
    renderPage();
    fireEvent.click(screen.getByTestId('ambiguous-dismiss-map_003'));
    expect(screen.getByTestId('state-reviewed')).toBeTruthy();
    expect(screen.queryByTestId('state-resolved')).toBeNull();
  });

  it('I2: reviewed state shown when mix of confirmed and dismissed', () => {
    // Two ambiguous cards
    mockRows = [MAPPED_SN, MAPPED_JIRA, AMBIGUOUS_SN, AMBIGUOUS_JIRA];
    mockCounts = { MAPPED: 2, UNMAPPED: 0, AMBIGUOUS: 2 };
    renderPage();
    // Confirm one, dismiss the other
    fireEvent.click(screen.getByTestId('ambiguous-option-map_003-Application'));
    fireEvent.click(screen.getByTestId('ambiguous-confirm-map_003'));
    fireEvent.click(screen.getByTestId('ambiguous-dismiss-map_004'));
    expect(screen.getByTestId('state-reviewed')).toBeTruthy();
    expect(screen.queryByTestId('state-resolved')).toBeNull();
  });

  it('I2: reviewed state text mentions dismissed count', () => {
    renderPage();
    fireEvent.click(screen.getByTestId('ambiguous-dismiss-map_003'));
    const reviewed = screen.getByTestId('state-reviewed');
    expect(reviewed.textContent).toContain('dismissed');
  });

  it('I2: neither state shown while cards still pending', () => {
    renderPage();
    expect(screen.queryByTestId('state-resolved')).toBeNull();
    expect(screen.queryByTestId('state-reviewed')).toBeNull();
  });

  // ── Developer panel ───────────────────────────────────────────────────────

  it('SI8: detail panel hidden by default', () => {
    renderPage();
    expect(screen.queryByTestId('detail-panel')).toBeNull();
  });

  it('SI8: toggle shows detail panel', () => {
    renderPage();
    fireEvent.click(screen.getByTestId('toggle-detail'));
    expect(screen.getByTestId('detail-panel')).toBeTruthy();
  });
});
