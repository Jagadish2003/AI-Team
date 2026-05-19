/**
 * useSetupState — SB-1 Sprint 7
 *
 * Central state management hook for the 4-screen setup flow.
 * Holds all setup state, navigation, and derived values.
 *
 * Used by:
 *   - DiscoveryFocusScreen (Screen 1)
 *   - YourSystemsScreen (Screen 2)
 *   - SourceWeightingScreen (Screen 3)
 *   - DiscoveryPlanScreen (Screen 4)
 *
 * Key design: state persists across all 4 screens.
 * Back navigation does NOT reset state.
 * Changing a selection on Screen 2 marks Screen 3 as needs_attention
 * if weighting was previously confirmed.
 */

import { useState, useCallback, useMemo } from 'react';
import type { WorkspaceCatalogResponse } from '../../types/workspace_catalog';
import { getCatalogSystemIds, getCatalogSalesforceProducts } from '../../types/workspace_catalog';
import {
  SetupState, FocusId, IndustryId, TemplateId,
  SystemWeighting, SystemRole, SystemPriority, WorkflowFocusTag,
  ProgressStep, StepStatus, ConfidenceState, ConfidenceLevel,
} from '../../types/stack_builder';

// ── Default weighting assumptions per system ID ───────────────────────────────
//
// ARCHITECTURAL NOTE (Sprint 7 — confirmed by architect review May 2026):
// These are recommendation seeds only. They are starting assumptions optimised
// for the known AgentIQ packs (nCino, PSS). They are NOT hardcoded semantic
// truths applicable across all industries or organisations.
//
// Examples of where defaults may be wrong:
//   - Workday may be system-of-record in HR-primary organisations
//   - SAP may be workflow-centric depending on module and use case
//   - Dynamics 365 may be primary in non-Salesforce estates
//   - ServiceNow may be system-of-record-like in certain operational contexts
//
// These defaults must remain editable by the user and extensible by pack.
// A logistics customer's SAP is not the same as a pension fund's SAP.
// Sprint 8 story: make defaults industry-registry-aware (pull from SB-3 IndustryConfig).
//
// Internal name: SYSTEM_DEFAULT_ASSUMPTIONS
// Do not treat as authoritative business meaning.
// Starting assumptions optimised for known packs — not cross-industry truths.

const SYSTEM_DEFAULT_ASSUMPTIONS: Record<string, Partial<SystemWeighting>> = {
  salesforce:        { role: 'system_of_record',         priority: 'primary',   workflowFocus: ['approvals', 'compliance_risk'] },
  salesforce_ncino:  { role: 'system_of_record',         priority: 'primary',   workflowFocus: ['intake_requests', 'approvals', 'compliance_risk'] },
  salesforce_sc:     { role: 'system_of_record',         priority: 'primary',   workflowFocus: ['service_casework', 'intake_requests', 'handoffs_routing'] },
  salesforce_pss:    { role: 'system_of_record',         priority: 'primary',   workflowFocus: ['intake_requests', 'approvals', 'compliance_risk'] },
  salesforce_fsc:    { role: 'system_of_record',         priority: 'primary',   workflowFocus: ['intake_requests', 'approvals'] },
  salesforce_rc:     { role: 'workflow_system',           priority: 'primary',   workflowFocus: ['approvals', 'backlog_work_queues'] },
  sap:               { role: 'system_of_record',         priority: 'primary',   workflowFocus: ['approvals', 'compliance_risk'] },
  oracle_ebs:        { role: 'system_of_record',         priority: 'primary',   workflowFocus: ['compliance_risk', 'approvals'] },
  workday:           { role: 'workflow_system',           priority: 'secondary', workflowFocus: ['intake_requests', 'compliance_risk'] },
  dynamics365:       { role: 'workflow_system',           priority: 'secondary', workflowFocus: ['intake_requests', 'handoffs_routing'] },
  jira:              { role: 'operational_signal_source', priority: 'secondary', workflowFocus: ['backlog_work_queues', 'change_release'] },
  servicenow:        { role: 'operational_signal_source', priority: 'secondary', workflowFocus: ['compliance_risk', 'backlog_work_queues'] },
  azure_devops:      { role: 'operational_signal_source', priority: 'secondary', workflowFocus: ['change_release', 'backlog_work_queues'] },
  linear:            { role: 'operational_signal_source', priority: 'secondary', workflowFocus: ['backlog_work_queues'] },
  zendesk:           { role: 'operational_signal_source', priority: 'secondary', workflowFocus: ['service_casework', 'backlog_work_queues'] },
  slack:             { role: 'operational_signal_source', priority: 'secondary', workflowFocus: ['communications', 'handoffs_routing'] },
  teams:             { role: 'operational_signal_source', priority: 'secondary', workflowFocus: ['communications'] },
  confluence:        { role: 'documentation_system',      priority: 'secondary', workflowFocus: ['documents_knowledge'] },
  sharepoint:        { role: 'documentation_system',      priority: 'secondary', workflowFocus: ['documents_knowledge', 'compliance_risk'] },
  notion:            { role: 'documentation_system',      priority: 'secondary', workflowFocus: ['documents_knowledge'] },
  github:            { role: 'engineering_change_system', priority: 'secondary', workflowFocus: ['change_release', 'backlog_work_queues'] },
  gitlab:            { role: 'engineering_change_system', priority: 'secondary', workflowFocus: ['change_release'] },
  bitbucket:         { role: 'engineering_change_system', priority: 'secondary', workflowFocus: ['change_release'] },
  azure_repos:       { role: 'engineering_change_system', priority: 'secondary', workflowFocus: ['change_release'] },
};

function defaultWeighting(systemId: string): SystemWeighting {
  const d = SYSTEM_DEFAULT_ASSUMPTIONS[systemId] || {};
  return {
    systemId,
    role: (d.role as SystemRole) || 'operational_signal_source',
    priority: (d.priority as SystemPriority) || 'secondary',
    workflowFocus: (d.workflowFocus as WorkflowFocusTag[]) || ['backlog_work_queues'],
    confirmed: false,
  };
}

// ── Setup readiness calculation ────────────────────────────────────────────────
//
// ARCHITECTURAL NOTE (Sprint 7 — confirmed by architect review May 2026):
// This function calculates CONFIGURATION READINESS, not discovery confidence.
// Internal name: calcSetupReadiness / SetupReadinessScore.
//
// What it measures:
//   - Has the user selected a primary platform?         (+40%)
//   - Have they added operational signal sources?       (+17% each, max 35%)
//   - Have they added a documentation system?           (+15%)
//   - Have they confirmed source weighting on Screen 3? (+10%)
//
// What it does NOT yet measure:
//   - Source diversity or relevance
//   - Actual authentication / connection state
//   - Pack-specific signal coverage
//   - Runtime discovery quality from previous runs
//
// UI label: "Discovery confidence" — retained as user-facing language.
// Internal model: SetupReadinessScore.
// This will evolve as runtime discovery quality signals become available (Sprint 9+).

function calcSetupReadiness(state: SetupState): ConfidenceState {
  const { selectedSystemIds, weightings, currentStep } = state;

  const hasPrimary = selectedSystemIds.some(id => {
    const w = weightings[id];
    return w && w.priority === 'primary';
  });

  const signalSources = selectedSystemIds.filter(id => {
    const w = weightings[id];
    return w && w.role === 'operational_signal_source';
  });

  const hasDoc = selectedSystemIds.some(id => {
    const w = weightings[id];
    return w && w.role === 'documentation_system';
  });

  const confirmedCount = Object.values(weightings).filter(w => w.confirmed).length;
  const totalSelected = selectedSystemIds.length;

  let fill = 0;
  if (hasPrimary) fill += 40;
  fill += Math.min(35, signalSources.length * 17);
  if (hasDoc) fill += 15;
  if (currentStep >= 3 && totalSelected > 0) {
    fill += Math.round((confirmedCount / totalSelected) * 10);
  }
  fill = Math.min(100, fill);

  const level: ConfidenceLevel = fill >= 75 ? 'strong' : fill >= 40 ? 'good' : 'basic';

  const hints: Record<ConfidenceLevel, string> = {
    basic: 'Select at least one primary business platform to improve confidence.',
    good: signalSources.length === 0
      ? 'Add an operational signal source — such as Jira or ServiceNow — to reach Strong.'
      : !hasDoc
      ? 'Add a documentation system — such as Confluence or SharePoint — to reach Strong.'
      : 'Confirm source roles in Step 3 to lock your discovery plan.',
    strong: currentStep < 4
      ? `Confidence is strong for workflow bottlenecks and cross-system corroboration. ${!hasDoc ? 'Add a documentation system to strengthen process interpretation.' : 'Confirm source roles to lock your discovery plan.'}`
      : '',
  };

  return { level, fillPercent: fill, hint: hints[level] };
}

// ── Step status derivation ────────────────────────────────────────────────────

function calcStepStatuses(state: SetupState): ProgressStep[] {
  const { currentStep, focusId, selectedSystemIds, weightings } = state;

  const step1Done = focusId !== null;

  const hasPrimary = selectedSystemIds.some(id => (weightings[id]?.priority === 'primary'));
  const step2Done = selectedSystemIds.length > 0 && hasPrimary;
  const step2NeedsAttn = currentStep > 2 && !step2Done;

  const confirmedAll = selectedSystemIds.length > 0 &&
    selectedSystemIds.every(id => weightings[id]?.confirmed);
  const step3NeedsAttn = currentStep > 3 && !confirmedAll;

  function status(step: number): StepStatus {
    if (currentStep === step) return 'active';
    if (step === 1) return step1Done ? 'completed' : 'pending';
    if (step === 2) {
      if (step2NeedsAttn) return 'needs_attention';
      return step2Done && currentStep > 2 ? 'completed' : 'pending';
    }
    if (step === 3) {
      if (step3NeedsAttn) return 'needs_attention';
      return confirmedAll && currentStep > 3 ? 'completed' : 'pending';
    }
    return currentStep > 4 ? 'completed' : 'pending';
  }

  return [
    { number: 1, label: 'Discovery focus',  status: status(1) },
    { number: 2, label: 'Your systems',     status: status(2) },
    { number: 3, label: 'Source weighting', status: status(3) },
    { number: 4, label: 'Discovery plan',   status: status(4) },
  ];
}

// ── Hook ──────────────────────────────────────────────────────────────────────

const INITIAL: SetupState = {
  focusId: null,
  industryId: null,
  templateId: null,
  selectedSystemIds: [],
  selectedSalesforceClouds: [],
  weightings: {},
  currentStep: 1,
};

export function useSetupState(catalog?: WorkspaceCatalogResponse | null) {
  const [state, setState] = useState<SetupState>(INITIAL);

  const setFocus = useCallback((id: FocusId) => {
    setState(s => ({ ...s, focusId: id }));
  }, []);

  const setIndustry = useCallback((id: IndustryId | null) => {
    setState(s => ({ ...s, industryId: id }));
  }, []);

  const setTemplate = useCallback((id: TemplateId | null, preselected: string[] = []) => {
    setState(s => {
      const merged = Array.from(new Set([...s.selectedSystemIds, ...preselected]));
      const newWeightings = { ...s.weightings };
      preselected.forEach(sid => {
        if (!newWeightings[sid]) newWeightings[sid] = defaultWeighting(sid);
      });
      return { ...s, templateId: id, selectedSystemIds: merged, weightings: newWeightings };
    });
  }, []);

  const toggleSystem = useCallback((systemId: string) => {
    setState(s => {
      const isSelected = s.selectedSystemIds.includes(systemId);
      if (isSelected) {
        const next = s.selectedSystemIds.filter(id => id !== systemId);
        const w = { ...s.weightings };
        delete w[systemId];
        return { ...s, selectedSystemIds: next, weightings: w };
      } else {
        const w = { ...s.weightings };
        if (!w[systemId]) w[systemId] = defaultWeighting(systemId);
        return { ...s, selectedSystemIds: [...s.selectedSystemIds, systemId], weightings: w };
      }
    });
  }, []);

  const toggleSalesforceCloud = useCallback((cloudId: string) => {
    setState(s => {
      const isSel = s.selectedSalesforceClouds.includes(cloudId);
      return {
        ...s,
        selectedSalesforceClouds: isSel
          ? s.selectedSalesforceClouds.filter(c => c !== cloudId)
          : [...s.selectedSalesforceClouds, cloudId],
      };
    });
  }, []);

  const setSalesforceClouds = useCallback((clouds: string[]) => {
    setState(s => ({
      ...s,
      selectedSalesforceClouds: clouds,
    }));
  }, []);

  const updateWeighting = useCallback((updated: SystemWeighting) => {
    setState(s => ({
      ...s,
      weightings: { ...s.weightings, [updated.systemId]: updated },
    }));
  }, []);

  const goTo = useCallback((step: 1 | 2 | 3 | 4) => {
    setState(s => ({ ...s, currentStep: step }));
  }, []);

  const canProceedFromStep1 = state.focusId !== null;
  // Fix Pack Sprint 7: canProceedFromStep2 must check selectedSystemIds
  // against known primary platform IDs — NOT against weighting priority.
  // Weighting priority is set on Screen 3, not Screen 2.
  // 'salesforce' base ID MUST be included — selecting Salesforce card
  // before cloud picker adds 'salesforce' (not 'salesforce_pss' etc.)
  const PRIMARY_PLATFORM_IDS = [
    'sap', 'oracle_ebs', 'workday', 'dynamics365',
    'salesforce',        // base ID — cloud picker adds salesforce_pss etc. separately
    'salesforce_pss', 'salesforce_sc', 'salesforce_ncino',
    'salesforce_fsc', 'salesforce_rc', 'salesforce_hc',
  ];
  const canProceedFromStep2 = state.selectedSystemIds.some(id =>
    PRIMARY_PLATFORM_IDS.includes(id)
  );

  const confidence = useMemo(() => calcSetupReadiness(state), [state]);
  const steps = useMemo(() => calcStepStatuses(state), [state]);

  // Engineering / change role relevance
  // ARCHITECTURAL NOTE (Sprint 7 — confirmed by architect review May 2026):
  // Current logic: tool-based only. Show Engineering/Change role when any of
  // the listed code/engineering systems are present in the selected estate.
  //
  // This is a temporary first approximation. Known limitations:
  //   - Azure DevOps is frequently used as work tracking, not engineering
  //   - Jira can represent change activity in engineering-led organisations
  //   - ServiceNow can be operational-system-of-record in some contexts
  //
  // Sprint 8 story: make showEngineeringRole focus-aware and domain-aware.
  // Logic should consider: selected systems + chosen focus + industry context.
  // ENG-SB-2 Sprint 9: derive showEngineeringRole from catalog when available.
  // catalog.data_engineering reflects what is connected in Integration Hub —
  // more accurate than checking selectedSystemIds (which includes unconnected
  // manually added systems). Falls back to selectedSystemIds when catalog absent.
  const codeEngineeringSystems = ['github', 'gitlab', 'bitbucket', 'azure_repos', 'azure_devops'];
  const showEngineeringRole = catalog
    ? (catalog.data_engineering ?? []).length > 0
    : state.selectedSystemIds.some(id => codeEngineeringSystems.includes(id));

  const restoreState = useCallback((saved: Partial<SetupState>) => {
    // ENG-SB-2 Sprint 9: filter restored selectedSystemIds against catalog.
    // A system disconnected from Integration Hub between sessions must not
    // reappear as selected. If catalog is absent, restore as-is (safe fallback).
    setState(s => {
      if (catalog && saved.selectedSystemIds) {
        const catalogIds = new Set(getCatalogSystemIds(catalog));
        const filtered = saved.selectedSystemIds.filter(id => catalogIds.has(id));
        return { ...s, ...saved, selectedSystemIds: filtered };
      }
      return { ...s, ...saved };
    });
  }, [catalog]);


  // ENG-SB-2 Sprint 9: seed selectedSystemIds and selectedSalesforceClouds
  // from workspace catalog. Called once by StackBuilderPage after catalog fetch.
  // Idempotent — only applies when state is still at initial
  // (currentStep === 1, no systems already selected). Prevents overwriting a
  // restored session or user selections.
  const initFromCatalog = useCallback((c: WorkspaceCatalogResponse) => {
    setState(s => {
      // Do not seed if user has already selected systems or moved past step 1
      if (s.selectedSystemIds.length > 0 || s.currentStep > 1) return s;

      const systemIds = getCatalogSystemIds(c);
      const sfProducts = getCatalogSalesforceProducts(c);

      // Build weightings for all pre-seeded systems
      const newWeightings: Record<string, any> = { ...s.weightings };
      systemIds.forEach(id => {
        if (!newWeightings[id]) newWeightings[id] = defaultWeighting(id);
      });

      return {
        ...s,
        selectedSystemIds:        systemIds,
        selectedSalesforceClouds: sfProducts,
        weightings:               newWeightings,
      };
    });
  }, []);

  return {
    state,
    setFocus,
    setIndustry,
    setTemplate,
    toggleSystem,
    toggleSalesforceCloud,
    setSalesforceClouds,
    updateWeighting,
    goTo,
    canProceedFromStep1,
    canProceedFromStep2,
    confidence,
    steps,
    showEngineeringRole,
    restoreState,
    initFromCatalog,
  };
}