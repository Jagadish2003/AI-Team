/**
 * StackBuilderWeightingPromise.test.tsx — R16-C1 T5
 *
 * Truthfulness check for the Stack Builder source-weighting promise.
 *
 * The Screen 3 setup copy tells users that confirming source roles and
 * priorities lets discovery "weight evidence correctly." After R16-C1 T1–T4
 * the backend engine actually honors that input, so the copy is now true and
 * must stay. These tests lock the frontend half of that promise in place:
 *
 *   1. The "weight evidence correctly" copy is present on the source-weighting
 *      step (it is no longer a credibility trap, so it must not be removed).
 *   2. The launch payload still carries the per-system weightings — the exact
 *      role and priority the customer captured and confirmed — through to the
 *      backend launch endpoint. The configuration the customer selected is the
 *      configuration that runs.
 *   3. Changing a system's role or priority changes what is sent at launch, so
 *      the run reflects the customer's selection (the observable AC2/AC3 path).
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  STEP_COPY,
  buildStackBuilderLaunchPayload,
} from '../pages/StackBuilderPage';
import type { SetupState, SystemWeighting } from '../types/stack_builder';

// ── Test fixtures ───────────────────────────────────────────────────────────

function weighting(overrides: Partial<SystemWeighting> & { systemId: string }): SystemWeighting {
  return {
    role: 'operational_signal_source',
    priority: 'secondary',
    workflowFocus: ['backlog_work_queues'],
    confirmed: true,
    ...overrides,
  };
}

function setupState(overrides: Partial<SetupState> = {}): SetupState {
  return {
    focusId: 'member_customer_service',
    industryId: 'financial_services',
    templateId: null,
    templatePreselectedIds: [],
    selectedSystemIds: ['salesforce', 'servicenow'],
    selectedSalesforceClouds: [],
    weightings: {
      salesforce: weighting({ systemId: 'salesforce', role: 'system_of_record', priority: 'primary' }),
      servicenow: weighting({ systemId: 'servicenow', role: 'workflow_system', priority: 'secondary' }),
    },
    currentStep: 4,
    ...overrides,
  };
}

// ── 1. Copy is accurate and retained ─────────────────────────────────────────

describe('R16-C1 T5 — setup copy matches reality', () => {
  it('keeps the "weight evidence correctly" promise on the source-weighting step', () => {
    expect(STEP_COPY[3].description).toContain('weight evidence correctly');
  });

  it('frames the promise around roles and priorities', () => {
    expect(STEP_COPY[3].description.toLowerCase()).toContain('roles');
    expect(STEP_COPY[3].description.toLowerCase()).toContain('priorities');
  });
});

// ── 2. Launch payload carries the weightings the customer confirmed ───────────

describe('R16-C1 T5 — launch payload includes weightings', () => {
  it('sends a weighting for every selected system', () => {
    const state = setupState();
    const payload = buildStackBuilderLaunchPayload(state, 'service_cloud', 'default');

    expect(Object.keys(payload.weightings).sort()).toEqual(['salesforce', 'servicenow']);
  });

  it('carries the exact role and priority captured per system', () => {
    const payload = buildStackBuilderLaunchPayload(setupState(), 'service_cloud', 'default');

    expect(payload.weightings.salesforce.role).toBe('system_of_record');
    expect(payload.weightings.salesforce.priority).toBe('primary');
    expect(payload.weightings.servicenow.role).toBe('workflow_system');
    expect(payload.weightings.servicenow.priority).toBe('secondary');
  });

  it('preserves the confirmed flag so the backend sees a confirmed configuration', () => {
    const payload = buildStackBuilderLaunchPayload(setupState(), 'service_cloud', 'default');

    expect(payload.weightings.salesforce.confirmed).toBe(true);
    expect(payload.weightings.servicenow.confirmed).toBe(true);
  });

  it('survives JSON serialization unchanged (the wire shape the backend receives)', () => {
    const payload = buildStackBuilderLaunchPayload(setupState(), 'service_cloud', 'default');
    const onWire = JSON.parse(JSON.stringify(payload));

    expect(onWire.weightings.salesforce.role).toBe('system_of_record');
    expect(onWire.weightings.servicenow.priority).toBe('secondary');
  });

  it('threads pack and org through unchanged alongside the weightings', () => {
    const payload = buildStackBuilderLaunchPayload(setupState(), 'ncino', 'org-123');

    expect(payload.pack_id).toBe('ncino');
    expect(payload.org_id).toBe('org-123');
    expect(payload.selected_system_ids).toEqual(['salesforce', 'servicenow']);
  });
});

// ── 3. Changing a setting changes what runs (observable AC2/AC3 path) ─────────

describe('R16-C1 T5 — the run reflects the customer selection', () => {
  it('reflects a changed role in the launch payload', () => {
    const recordState = setupState();
    const supportingState = setupState({
      weightings: {
        ...recordState.weightings,
        salesforce: weighting({ systemId: 'salesforce', role: 'operational_signal_source', priority: 'primary' }),
      },
    });

    const recordPayload = buildStackBuilderLaunchPayload(recordState, 'service_cloud', 'default');
    const supportingPayload = buildStackBuilderLaunchPayload(supportingState, 'service_cloud', 'default');

    expect(recordPayload.weightings.salesforce.role).toBe('system_of_record');
    expect(supportingPayload.weightings.salesforce.role).toBe('operational_signal_source');
    expect(recordPayload.weightings.salesforce.role)
      .not.toBe(supportingPayload.weightings.salesforce.role);
  });

  it('reflects a changed priority in the launch payload', () => {
    const primaryState = setupState();
    const optionalState = setupState({
      weightings: {
        ...primaryState.weightings,
        salesforce: weighting({ systemId: 'salesforce', role: 'system_of_record', priority: 'optional' }),
      },
    });

    expect(
      buildStackBuilderLaunchPayload(primaryState, 'service_cloud', 'default').weightings.salesforce.priority,
    ).toBe('primary');
    expect(
      buildStackBuilderLaunchPayload(optionalState, 'service_cloud', 'default').weightings.salesforce.priority,
    ).toBe('optional');
  });

  it('is deterministic — identical configuration produces an identical payload', () => {
    const a = buildStackBuilderLaunchPayload(setupState(), 'service_cloud', 'default');
    const b = buildStackBuilderLaunchPayload(setupState(), 'service_cloud', 'default');

    expect(JSON.stringify(a)).toEqual(JSON.stringify(b));
  });
});

// ── 4. A selected system missing a weighting is surfaced, not silently dropped ─

describe('R16-C1 — missing weighting is surfaced at launch', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('warns when a selected system has no weighting entry', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    // servicenow is selected but only salesforce has a weighting entry.
    const state = setupState({
      selectedSystemIds: ['salesforce', 'servicenow'],
      weightings: {
        salesforce: weighting({ systemId: 'salesforce', role: 'system_of_record', priority: 'primary' }),
      },
    });

    buildStackBuilderLaunchPayload(state, 'service_cloud', 'default');

    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0][0]).toContain('servicenow');
  });

  it('does not warn when every selected system has a weighting', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});

    buildStackBuilderLaunchPayload(setupState(), 'service_cloud', 'default');

    expect(warn).not.toHaveBeenCalled();
  });

  it('still sends the weightings it does have (warning is non-blocking)', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const state = setupState({
      selectedSystemIds: ['salesforce', 'servicenow'],
      weightings: {
        salesforce: weighting({ systemId: 'salesforce', role: 'system_of_record', priority: 'primary' }),
      },
    });

    const payload = buildStackBuilderLaunchPayload(state, 'service_cloud', 'default');

    expect(payload.selected_system_ids).toEqual(['salesforce', 'servicenow']);
    expect(Object.keys(payload.weightings)).toEqual(['salesforce']);
  });
});
