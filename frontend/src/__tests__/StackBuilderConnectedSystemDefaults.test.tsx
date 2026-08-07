/**
 * Stack Builder Screen 2 — connected systems are selected by default.
 *
 * A system the workspace has connected is one it has already decided to give
 * AgentIQ, so arriving at Screen 2 with every one of them unticked made the common
 * case a manual re-selection of work already done.
 *
 * The rules these tests pin (see useSetupState.applyConnectedSystemDefaults):
 *   - connected systems start selected, each with a default weighting so Screen 3
 *     and the step-2 "has a primary" gate behave as for a hand-made selection;
 *   - `needs_auth` / `not_configured` systems are NOT selected — they cannot
 *     contribute data to a run, so pre-selecting them would put an unreadable
 *     source in the plan;
 *   - the seed is ONE-TIME: a system the user deselects stays deselected when the
 *     catalog refreshes in the background (their choice wins);
 *   - it is ADDITIVE: a template's systems and a manual selection both survive.
 *
 * Run:
 *   npx vitest run src/__tests__/StackBuilderConnectedSystemDefaults.test.tsx
 */
import '@testing-library/jest-dom/vitest';
import { renderHook, act } from '@testing-library/react';
import { describe, it, expect } from 'vitest';

import { useSetupState } from '../components/stack_builder';
import {
  getConnectedCatalogSystemIds,
  type WorkspaceCatalogResponse,
} from '../types/workspace_catalog';

const CATALOG: WorkspaceCatalogResponse = {
  primary_platforms: [
    { system_id: 'salesforce', name: 'Salesforce', status: 'connected', products: [] },
  ],
  operational_systems: [
    { system_id: 'servicenow', name: 'ServiceNow', status: 'connected', products: [] },
    { system_id: 'jira', name: 'Jira', status: 'needs_auth', products: [] },
  ],
  comms_knowledge: [
    { system_id: 'slack', name: 'Slack', status: 'connected', products: [] },
    { system_id: 'confluence', name: 'Confluence', status: 'not_configured', products: [] },
  ],
  data_engineering: [],
  missing_categories: [],
};

describe('getConnectedCatalogSystemIds', () => {
  it('returns connected systems only', () => {
    expect(getConnectedCatalogSystemIds(CATALOG)).toEqual([
      'salesforce',
      'servicenow',
      'slack',
    ]);
  });

  it('excludes needs_auth — an unreadable source must not enter the plan', () => {
    const ids = getConnectedCatalogSystemIds(CATALOG);
    expect(ids).not.toContain('jira');
    expect(ids).not.toContain('confluence');
  });
});

describe('applyConnectedSystemDefaults', () => {
  it('selects every connected system, with a default weighting each', () => {
    const { result } = renderHook(() => useSetupState(null));

    act(() => {
      result.current.applyConnectedSystemDefaults(getConnectedCatalogSystemIds(CATALOG));
    });

    expect(result.current.state.selectedSystemIds).toEqual([
      'salesforce',
      'servicenow',
      'slack',
    ]);
    // A weighting per selected system, none of them pre-confirmed — Screen 3 still
    // asks the user to confirm, exactly as for a manually selected system.
    for (const id of ['salesforce', 'servicenow', 'slack']) {
      expect(result.current.state.weightings[id]).toBeDefined();
      expect(result.current.state.weightings[id].confirmed).toBe(false);
    }
    expect(result.current.state.connectedDefaultsApplied).toBe(true);
  });

  it('does NOT re-select a system the user deselected (the seed is one-time)', () => {
    const { result } = renderHook(() => useSetupState(null));
    const connected = getConnectedCatalogSystemIds(CATALOG);

    act(() => result.current.applyConnectedSystemDefaults(connected));
    act(() => result.current.toggleSystem('servicenow'));
    expect(result.current.state.selectedSystemIds).not.toContain('servicenow');

    // The catalog refreshes in the background and the seed is attempted again.
    act(() => result.current.applyConnectedSystemDefaults(connected));

    expect(result.current.state.selectedSystemIds).not.toContain('servicenow');
    expect(result.current.state.weightings.servicenow).toBeUndefined();
  });

  it('marks itself applied even for an empty catalog, so a later refresh cannot re-tick', () => {
    const { result } = renderHook(() => useSetupState(null));

    act(() => result.current.applyConnectedSystemDefaults([]));
    expect(result.current.state.connectedDefaultsApplied).toBe(true);

    act(() => result.current.applyConnectedSystemDefaults(['salesforce']));
    expect(result.current.state.selectedSystemIds).toEqual([]);
  });

  it('is additive — a manual selection made first is kept', () => {
    const { result } = renderHook(() => useSetupState(null));

    act(() => result.current.toggleSystem('github'));
    act(() => result.current.applyConnectedSystemDefaults(getConnectedCatalogSystemIds(CATALOG)));

    expect(result.current.state.selectedSystemIds).toEqual([
      'github',
      'salesforce',
      'servicenow',
      'slack',
    ]);
  });

  it('never duplicates a system already selected', () => {
    const { result } = renderHook(() => useSetupState(null));

    act(() => result.current.toggleSystem('salesforce'));
    act(() => result.current.applyConnectedSystemDefaults(['salesforce', 'slack']));

    expect(result.current.state.selectedSystemIds).toEqual(['salesforce', 'slack']);
  });

  it('a restored session with no systems gets the seed; one WITH systems keeps its own', () => {
    // Restoring an empty selection means no system choice has been made yet, so the
    // connected-systems default still applies.
    const empty = renderHook(() => useSetupState(null));
    act(() => empty.result.current.applyConnectedSystemDefaults(['salesforce']));
    act(() => empty.result.current.restoreState({ selectedSystemIds: [], currentStep: 1 }));
    expect(empty.result.current.state.connectedDefaultsApplied).toBe(false);

    // A restored session that DOES carry systems is the user's own prior choice —
    // re-seeding would re-add a system they removed in an earlier session.
    const saved = renderHook(() => useSetupState(null));
    act(() =>
      saved.result.current.restoreState({
        selectedSystemIds: ['jira'],
        connectedDefaultsApplied: true,
        currentStep: 1,
      }),
    );
    act(() => saved.result.current.applyConnectedSystemDefaults(['salesforce', 'slack']));
    expect(saved.result.current.state.selectedSystemIds).toEqual(['jira']);
  });
});
