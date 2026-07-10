/**
 * T41-7 — Required Permissions Reframe — Frontend Tests
 *
 * Three acceptance criteria verified:
 *
 * AC1: Required Permissions section is ABSENT from Opportunity Review detail panel.
 *      OpportunityDetail.tsx no longer renders the permissions block regardless
 *      of whether opp.permissions is populated.
 *
 * AC2: Agent Blueprint screen shows Agent Permissions Required with
 *      future-tense framing: "To implement this agent, the agent user
 *      profile will need:". No checked/missing status icons.
 *
 * AC3: Integration Hub ConnectorDetailPanel shows Connection Health section
 *      with green checkmarks when connector.status === 'connected'.
 *      Section is absent when connector is not connected.
 *
 * Run:
 *   npx vitest run src/__tests__/T41_7_permissions_reframe.test.tsx
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import type { OpportunityCandidate, ReviewAuditEvent } from '../types/analystReview';
import type { Connector } from '../types/connector';

// ── Mocks ─────────────────────────────────────────────────────────────────────

vi.mock('../context/RunContext', () => ({
  useRunContext: () => ({ runId: null }),
}));

vi.mock('../context/ConnectorContext', () => ({
  useConnectorContext: () => ({
    all: [
      {
        id: 'salesforce', name: 'Salesforce', status: 'connected',
        configured: true, category: 'CRM', tier: 'recommended',
        reads: ['Cases', 'Flows', 'Approvals'],
        lastSynced: '2 hours ago', metrics: [], signalStrength: 90,
      },
    ],
  }),
}));

vi.mock('../context/AnalystReviewContext', () => ({
  useAnalystReviewContext: () => ({
    opportunities: [],
    selectedId: null,
    select: vi.fn(),
  }),
}));

vi.mock('../api/enrichmentApi', () => ({
  fetchOppEnrichment: vi.fn().mockResolvedValue(null),
}));

vi.mock('../api/blueprintApi', () => ({
  fetchBlueprint: vi.fn().mockResolvedValue(null),
}));

vi.mock('../components/common/TopNav', () => ({
  default: () => <nav data-testid="top-nav" />,
}));

vi.mock('../components/common/Toast', () => ({
  useToast: () => ({ push: vi.fn() }),
}));

// ── Fixtures ──────────────────────────────────────────────────────────────────

const OPP_WITH_PERMISSIONS: OpportunityCandidate = {
  id: 'opp_perm_001',
  title: 'Accelerate approval routing',
  category: 'Approval Automation',
  tier: 'Quick Win',
  impact: 7,
  effort: 3,
  confidence: 'HIGH',
  aiRationale: 'Approval bottleneck detected.',
  evidenceIds: ['ev_001'],
  decision: 'UNREVIEWED',
  override: { isLocked: false, rationaleOverride: '', overrideReason: '', updatedAt: null },
  // permissions populated — should NOT render in OpportunityDetail after T41-7
  permissions: [
    { label: 'Read ProcessInstance', satisfied: true, required: true },
    { label: 'Read ProcessInstanceStep', satisfied: false, required: true },
    { label: 'Read User records', satisfied: true, required: false },
  ],
  requiredPermissions: [
    'Salesforce: read ProcessInstance',
    'Salesforce: read ProcessInstanceStep',
  ],
};

const AUDIT: ReviewAuditEvent[] = [];

const CONNECTED_SALESFORCE_CONNECTOR: Connector = {
  id: 'salesforce',
  name: 'Salesforce',
  status: 'connected',
  configured: true,
  category: 'CRM',
  tier: 'recommended',
  reads: ['Cases', 'Flows', 'Approvals'],
  lastSynced: '2 hours ago',
  metrics: [],
  signalStrength: 90,
  products: ['salesforce_sc'],
};

const CONNECTED_NCINO_SALESFORCE_CONNECTOR: Connector = {
  id: 'salesforce',
  name: 'Salesforce',
  status: 'connected',
  configured: true,
  category: 'CRM · Salesforce / nCino',
  tier: 'recommended',
  reads: ['Accounts', 'Opportunities', 'Cases'],
  lastSynced: 'Just now',
  metrics: [],
  signalStrength: 94,
  products: ['salesforce_ncino'],
};

// Category mentions nCino, but the org only declared Service Cloud — used to
// prove the read scope follows the DECLARED PRODUCTS, not the catalog category.
const CONNECTED_SALESFORCE_NO_NCINO_PRODUCT: Connector = {
  id: 'salesforce',
  name: 'Salesforce',
  status: 'connected',
  configured: true,
  category: 'CRM · Salesforce / nCino',
  tier: 'recommended',
  reads: ['Accounts', 'Opportunities', 'Cases'],
  lastSynced: 'Just now',
  metrics: [],
  signalStrength: 90,
  products: ['salesforce_sc'],
};

const CONNECTED_JIRA_CONNECTOR: Connector = {
  id: 'jira_confluence',
  name: 'Jira & Confluence',
  status: 'connected',
  configured: true,
  category: 'Delivery - Knowledge',
  tier: 'recommended',
  reads: ['Issues', 'Boards', 'Runbooks / Pages'],
  lastSynced: 'Just now',
  metrics: [],
  signalStrength: 85,
};

const DISCONNECTED_CONNECTOR: Connector = {
  id: 'servicenow',
  name: 'ServiceNow',
  status: 'not_connected',
  configured: false,
  category: 'ITSM',
  tier: 'standard',
  reads: ['Incidents', 'Changes'],
  lastSynced: 'Never',
  metrics: [],
  signalStrength: 0,
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function renderWithRouter(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

// ─────────────────────────────────────────────────────────────────────────────
// AC1 — Required Permissions absent from OpportunityDetail
// ─────────────────────────────────────────────────────────────────────────────

import OpportunityDetail from '../components/analyst_review/OpportunityDetail';

describe('AC1 — Required Permissions removed from OpportunityDetail', () => {

  it('does not render "Required Data Permissions" heading when opp has permissions', () => {
    renderWithRouter(
      <OpportunityDetail
        opp={OPP_WITH_PERMISSIONS}
        audit={AUDIT}
      />
    );
    expect(screen.queryByText('Required Data Permissions')).toBeNull();
  });

  it('does not render permission labels (Granted / Missing) even when permissions are populated', () => {
    renderWithRouter(
      <OpportunityDetail
        opp={OPP_WITH_PERMISSIONS}
        audit={AUDIT}
      />
    );
    expect(screen.queryByText('✓ Granted')).toBeNull();
    expect(screen.queryByText('◇ Missing')).toBeNull();
    expect(screen.queryByText('Recommended')).toBeNull();
  });

  it('does not render individual permission labels from opp.permissions', () => {
    renderWithRouter(
      <OpportunityDetail
        opp={OPP_WITH_PERMISSIONS}
        audit={AUDIT}
      />
    );
    expect(screen.queryByText('Read ProcessInstance')).toBeNull();
    expect(screen.queryByText('Read ProcessInstanceStep')).toBeNull();
    expect(screen.queryByText('Read User records')).toBeNull();
  });

  it('still renders the opportunity title correctly (no regression)', () => {
    renderWithRouter(
      <OpportunityDetail
        opp={OPP_WITH_PERMISSIONS}
        audit={AUDIT}
      />
    );
    expect(screen.getByText('Accelerate approval routing')).toBeDefined();
  });

  it('still renders AI Analysis section (no regression)', () => {
    renderWithRouter(
      <OpportunityDetail
        opp={OPP_WITH_PERMISSIONS}
        audit={AUDIT}
      />
    );
    expect(screen.getByText('AI Analysis')).toBeDefined();
  });

  it('still renders Audit Trail section (no regression)', () => {
    renderWithRouter(
      <OpportunityDetail
        opp={OPP_WITH_PERMISSIONS}
        audit={AUDIT}
      />
    );
    expect(screen.getByText('Audit Trail')).toBeDefined();
  });

  it('suppressPermissions is deprecated — prop accepted but section always absent regardless of value', () => {
    // T41-7: suppressPermissions is kept for backward compat only.
    // It is no longer in the useEffect dependency array (was causing spurious
    // refetches). Section is always absent — prop value does not matter.
    const { rerender } = renderWithRouter(
      <OpportunityDetail
        opp={OPP_WITH_PERMISSIONS}
        audit={AUDIT}
        suppressPermissions={false}
      />
    );
    expect(screen.queryByText('Required Data Permissions')).toBeNull();

    rerender(
      <MemoryRouter>
        <OpportunityDetail
          opp={OPP_WITH_PERMISSIONS}
          audit={AUDIT}
          suppressPermissions={true}
        />
      </MemoryRouter>
    );
    expect(screen.queryByText('Required Data Permissions')).toBeNull();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// AC2 — Blueprint screen shows future-tense permissions framing
// Tests the REAL BlueprintContent component exported from BlueprintPage.tsx
// ─────────────────────────────────────────────────────────────────────────────

import { BlueprintContent } from '../pages/BlueprintPage';
import type { BlueprintResponse } from '../utils/blueprintTypes';

const BLUEPRINT_WITH_PERMISSIONS: BlueprintResponse = {
  oppId: 'opp_perm_001',
  agentName: 'Approval Automation Agent',
  agentTopic: 'Automates approval routing for discount requests.',
  agentTopicIsLlm: false,
  detectorId: 'APPROVAL_BOTTLENECK',
  suggestedActions: [],
  guardrails: ['Agent must not approve items above the configured discount threshold.'],
  agentforcePermissions: [
    'Salesforce: read ProcessInstance',
    'Salesforce: read ProcessInstanceStep',
    'Salesforce: read User',
  ],
  complexity: { label: 'Standard configuration', description: 'Single-system agent.', tier: 'Quick Win' },
  evidenceIds: ['ev_sf_001'],
};

const BLUEPRINT_NO_PERMISSIONS: BlueprintResponse = {
  ...BLUEPRINT_WITH_PERMISSIONS,
  agentforcePermissions: [],
};

describe('AC2 — BlueprintContent renders future-tense permissions framing', () => {

  it('renders forward-looking intro sentence when permissions are present', () => {
    renderWithRouter(<BlueprintContent blueprint={BLUEPRINT_WITH_PERMISSIONS} />);
    expect(
      screen.getByText('To implement this agent, the agent user profile will need:')
    ).toBeDefined();
  });

  it('renders each permission as a plain list item with no status badges', () => {
    renderWithRouter(<BlueprintContent blueprint={BLUEPRINT_WITH_PERMISSIONS} />);
    expect(screen.getByText('Salesforce: read ProcessInstance')).toBeDefined();
    expect(screen.getByText('Salesforce: read ProcessInstanceStep')).toBeDefined();
    expect(screen.getByText('Salesforce: read User')).toBeDefined();
    // No Granted/Missing status — these must be absent
    expect(screen.queryByText('✓ Granted')).toBeNull();
    expect(screen.queryByText('◇ Missing')).toBeNull();
  });

  it('renders fallback text when agentforcePermissions is empty', () => {
    renderWithRouter(<BlueprintContent blueprint={BLUEPRINT_NO_PERMISSIONS} />);
    expect(
      screen.getByText('Permissions assessment is not yet available for this opportunity.')
    ).toBeDefined();
    expect(
      screen.queryByText('To implement this agent, the agent user profile will need:')
    ).toBeNull();
  });

  it('section heading reads "Agent Permissions Required" in the real component', () => {
    renderWithRouter(<BlueprintContent blueprint={BLUEPRINT_WITH_PERMISSIONS} />);
    expect(screen.getByText('Agent Permissions Required')).toBeDefined();
  });

  it('other Blueprint sections still render alongside permissions (no regression)', () => {
    renderWithRouter(<BlueprintContent blueprint={BLUEPRINT_WITH_PERMISSIONS} />);
    expect(screen.getByText('Agent Purpose')).toBeDefined();
    expect(screen.getByText('Suggested Agent Actions')).toBeDefined();
    expect(screen.getByText('Guardrails')).toBeDefined();
    expect(screen.getByText('Implementation Complexity')).toBeDefined();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// AC3 — ConnectorDetailPanel shows Connection Health when connected
// ─────────────────────────────────────────────────────────────────────────────

import ConnectorDetailPanel from '../components/integrations/ConnectorDetailPanel';

describe('AC3 — Connection Health section in ConnectorDetailPanel', () => {

  it('renders "Configured Read Scope" heading when connector is connected', () => {
    renderWithRouter(
      <ConnectorDetailPanel
        connector={CONNECTED_SALESFORCE_CONNECTOR}
        onConfigure={vi.fn()}
      />
    );
    expect(screen.getByText('Configured Read Scope')).toBeDefined();
  });

  it('shows configured read labels for a connected Salesforce connector', () => {
    renderWithRouter(
      <ConnectorDetailPanel
        connector={CONNECTED_SALESFORCE_CONNECTOR}
        onConfigure={vi.fn()}
      />
    );
    expect(screen.getByText('Read Case records')).toBeDefined();
    expect(screen.getByText('Read Flow metadata')).toBeDefined();
    expect(screen.getByText('Read Approval history')).toBeDefined();
  });

  it('shows nCino banking scope when the org declared the nCino product, and no POC benefits object names', () => {
    // R18-C0 P1 (AC1): with the nCino product declared, the connector shows
    // banking-relevant object scope; the old 'PSS Benefits Administration'
    // object names never appear in the connector presentation.
    renderWithRouter(
      <ConnectorDetailPanel
        connector={CONNECTED_NCINO_SALESFORCE_CONNECTOR}
        onConfigure={vi.fn()}
      />
    );
    expect(screen.getByText('Read LLC_BI__Loan__c records')).toBeDefined();
    expect(screen.getByText('Read LLC_BI__Covenant2__c records')).toBeDefined();
    expect(screen.queryByText('Read IndividualApplication records')).toBeNull();
    expect(screen.queryByText('Read BenefitAssignment records')).toBeNull();
    expect(screen.queryByText('Read Case records (Disability)')).toBeNull();
  });

  it('derives read scope from declared products, not the catalog category', () => {
    // R18-C0 P1: the connector card must reflect the connected org's ACTUAL
    // selected products. A Salesforce org that declared only Service Cloud must
    // NOT show nCino scope even though the base catalog category names nCino.
    renderWithRouter(
      <ConnectorDetailPanel
        connector={CONNECTED_SALESFORCE_NO_NCINO_PRODUCT}
        onConfigure={vi.fn()}
      />
    );
    // Standard base objects + the declared Service Cloud scope are shown.
    expect(screen.getByText('Read Account records')).toBeDefined();
    expect(screen.getByText('Read Flow metadata')).toBeDefined();
    // nCino objects must NOT appear — the category string mentioning nCino is
    // no longer what drives the read scope.
    expect(screen.queryByText('Read LLC_BI__Loan__c records')).toBeNull();
  });

  it('uses generic operational-signal language for Jira and Confluence (no POC benefits wording)', () => {
    renderWithRouter(
      <ConnectorDetailPanel
        connector={CONNECTED_JIRA_CONNECTOR}
        onConfigure={vi.fn()}
      />
    );
    expect(screen.getByText('Read operational signals')).toBeDefined();
    expect(screen.queryByText('Read benefit operations signals')).toBeNull();
  });

  it('does NOT render the section when connector is not_connected', () => {
    renderWithRouter(
      <ConnectorDetailPanel
        connector={DISCONNECTED_CONNECTOR}
        onConfigure={vi.fn()}
      />
    );
    expect(screen.queryByText('Configured Read Scope')).toBeNull();
  });

  it('renders the accurate scope note (not overstating last-sync proof)', () => {
    renderWithRouter(
      <ConnectorDetailPanel
        connector={CONNECTED_SALESFORCE_CONNECTOR}
        onConfigure={vi.fn()}
      />
    );
    expect(
      screen.getByText(/Configured read scope for this connector/)
    ).toBeDefined();
  });

  it('still renders Access section alongside read scope (no regression)', () => {
    renderWithRouter(
      <ConnectorDetailPanel
        connector={CONNECTED_SALESFORCE_CONNECTOR}
        onConfigure={vi.fn()}
      />
    );
    expect(screen.getByText('Access as:')).toBeDefined();
  });

  it('still renders Re-sync button (no regression)', () => {
    renderWithRouter(
      <ConnectorDetailPanel
        connector={CONNECTED_SALESFORCE_CONNECTOR}
        onConfigure={vi.fn()}
      />
    );
    expect(screen.getByText('Re-sync')).toBeDefined();
  });

  it('renders null/no section when no connector selected (no regression)', () => {
    renderWithRouter(
      <ConnectorDetailPanel
        connector={null}
        onConfigure={vi.fn()}
      />
    );
    expect(screen.queryByText('Configured Read Scope')).toBeNull();
  });
});
