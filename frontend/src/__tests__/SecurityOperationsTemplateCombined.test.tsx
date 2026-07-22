import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import { useSetupState } from '../components/stack_builder';
import DiscoveryFocusPage from '../pages/DiscoveryFocusPage';
import YourSystemsPage from '../pages/YourSystemsPage';
import { buildStackBuilderLaunchPayload } from '../pages/StackBuilderPage';
import type { TemplateListItem } from '../types/stack_builder';
import type { WorkspaceCatalogResponse } from '../types/workspace_catalog';

const CLOUD_TEMPLATE: TemplateListItem = {
  template_id: 'managed_cloud_operations',
  label: 'Managed Cloud Operations',
  description: 'Cloud operations defaults supplied by the registry.',
  suggested_systems: ['servicenow', 'aws_event_source', 'azure_event_source', 'runbook_library'],
  suggested_roles: {
    servicenow: 'system_of_record',
    aws_event_source: 'operational_signal_source',
    azure_event_source: 'operational_signal_source',
    runbook_library: 'documentation_system',
  },
  focus_defaults: {
    focus_id: 'core_operations',
    emphasis: ['incident_recurrence', 'alert_triage', 'routing_friction'],
  },
  pack_id: 'cloud_ops',
  detector_emphasis: ['RECURRING_RESOLUTION_LOOP'],
  terminology: { notification: 'alert', documentation: 'runbook' },
  metadata: { source: 'MSP-B6', version: '1.0.0' },
};

const SECURITY_TEMPLATE: TemplateListItem = {
  template_id: 'security_operations',
  label: 'Security Operations',
  description: 'Security operations defaults supplied by the registry.',
  suggested_systems: ['servicenow', 'aws_event_source', 'azure_event_source', 'runbook_library'],
  suggested_roles: {
    servicenow: 'system_of_record',
    aws_event_source: 'operational_signal_source',
    azure_event_source: 'operational_signal_source',
    runbook_library: 'documentation_system',
  },
  focus_defaults: {
    focus_id: 'core_operations',
    emphasis: ['backlog_work_queues', 'handoffs_routing', 'compliance_risk'],
  },
  pack_id: 'security_ops',
  detector_emphasis: ['SECOPS_REMEDIATION_RECURRENCE'],
  terminology: { ticket: 'remediation task', documentation: 'playbook' },
  metadata: { source: 'MSP-B12', version: '1.0.0' },
};

function CombinedTemplateHarness() {
  const setupState = useSetupState();
  const payload = buildStackBuilderLaunchPayload(
    setupState.state,
    setupState.state.packId ?? 'service_cloud',
    'b12-ui-contract-org',
  );

  return (
    <div>
      <DiscoveryFocusPage
        setupState={setupState}
        industries={[]}
        templates={[CLOUD_TEMPLATE, SECURITY_TEMPLATE]}
        registryLoading={false}
        registryError={null}
        onRetryRegistry={vi.fn()}
        fetchSystemDefaults={vi.fn(async () => [])}
      />
      <output data-testid="setup-state">{JSON.stringify(setupState.state)}</output>
      <output data-testid="launch-payload">{JSON.stringify(payload)}</output>
      <button type="button" onClick={() => setupState.setPack('security_ops')}>
        Make Security Operations primary
      </button>
      <button
        type="button"
        onClick={() => setupState.restoreState({
          templateId: 'security_operations',
          selectedSystemIds: ['servicenow'],
        })}
      >
        Restore legacy template state
      </button>
    </div>
  );
}

const SERVICENOW_CATALOG: WorkspaceCatalogResponse = {
  primary_platforms: [],
  operational_systems: [{
    system_id: 'servicenow',
    name: 'ServiceNow',
    status: 'connected',
    products: [],
  }],
  comms_knowledge: [],
  data_engineering: [],
  missing_categories: ['primary_platforms', 'comms_knowledge', 'data_engineering'],
};

function EditableSystemsHarness() {
  const setupState = useSetupState();
  return (
    <MemoryRouter>
      <button type="button" onClick={() => setupState.applyTemplate(CLOUD_TEMPLATE)}>
        Select Cloud Operations
      </button>
      <button type="button" onClick={() => setupState.applyTemplate(SECURITY_TEMPLATE)}>
        Select Security Operations
      </button>
      <YourSystemsPage setupState={setupState} catalog={SERVICENOW_CATALOG} />
      <output data-testid="editable-state">{JSON.stringify(setupState.state)}</output>
    </MemoryRouter>
  );
}

describe('MSP-B12 Security Operations template selection', () => {
  it('keeps Cloud Operations and Security Operations in one editable launch', () => {
    render(<CombinedTemplateHarness />);

    fireEvent.click(screen.getByRole('checkbox', { name: CLOUD_TEMPLATE.label }));
    fireEvent.click(screen.getByRole('checkbox', { name: SECURITY_TEMPLATE.label }));

    const state = JSON.parse(screen.getByTestId('setup-state').textContent ?? '{}');
    const payload = JSON.parse(screen.getByTestId('launch-payload').textContent ?? '{}');

    expect(state.templateIds).toEqual([
      'managed_cloud_operations',
      'security_operations',
    ]);
    expect(state.packIds).toEqual(['cloud_ops', 'security_ops']);
    expect(state.selectedSystemIds).toEqual([
      'servicenow',
      'aws_event_source',
      'azure_event_source',
      'runbook_library',
    ]);
    expect(payload.template_ids).toEqual(state.templateIds);
    expect(payload.pack_ids).toEqual(state.packIds);

    fireEvent.click(screen.getByRole('button', { name: 'Make Security Operations primary' }));
    const reordered = JSON.parse(screen.getByTestId('setup-state').textContent ?? '{}');
    expect(reordered.packId).toBe('security_ops');
    expect(reordered.packIds).toEqual(['security_ops', 'cloud_ops']);

    // Each template remains independently editable rather than replacing the
    // other selection or locking its shared systems into the setup.
    fireEvent.click(screen.getByRole('checkbox', { name: CLOUD_TEMPLATE.label }));
    const edited = JSON.parse(screen.getByTestId('setup-state').textContent ?? '{}');
    expect(edited.templateIds).toEqual(['security_operations']);
    expect(edited.packIds).toEqual(['security_ops']);
  });

  it('shows template-only sources and preserves a user removal across composition', () => {
    render(<EditableSystemsHarness />);
    fireEvent.click(screen.getByRole('button', { name: 'Select Cloud Operations' }));

    const runbookCard = screen.getByRole('checkbox', {
      name: /Runbook or playbook library/,
    });
    expect(runbookCard).toHaveAttribute('aria-checked', 'true');
    fireEvent.click(runbookCard);
    expect(runbookCard).toHaveAttribute('aria-checked', 'false');

    fireEvent.click(screen.getByRole('button', { name: 'Select Security Operations' }));
    expect(runbookCard).toHaveAttribute('aria-checked', 'false');
    const state = JSON.parse(screen.getByTestId('editable-state').textContent ?? '{}');
    expect(state.templateIds).toEqual([
      'managed_cloud_operations',
      'security_operations',
    ]);
    expect(state.selectedSystemIds).not.toContain('runbook_library');
  });

  it('migrates an older saved single-template setup to the plural contract', () => {
    render(<CombinedTemplateHarness />);
    fireEvent.click(screen.getByRole('button', { name: 'Restore legacy template state' }));
    const state = JSON.parse(screen.getByTestId('setup-state').textContent ?? '{}');
    const payload = JSON.parse(screen.getByTestId('launch-payload').textContent ?? '{}');
    expect(state.templateIds).toEqual(['security_operations']);
    expect(payload.template_ids).toEqual(['security_operations']);
  });
});
