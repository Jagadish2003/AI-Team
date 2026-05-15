/**
 * StackBuilderRouter — SB-13 Task 13 Sprint 7
 *
 * Top-level orchestrator for the Guided Discovery Stack Builder.
 * Owns the single useSetupState() call and distributes state
 * to all four screens. Handles session persistence and launch.
 *
 * Responsibilities:
 *   1. Call useSetupState() once — pass result as setupState prop to screens
 *   2. Restore session via useStackBuilderPersistence on mount
 *   3. Auto-save state changes via useStackBuilderPersistence (debounced)
 *   4. Render correct screen per state.currentStep (1–4)
 *   5. Handle onLaunch — translate setup state to ComputeRequest, POST to
 *      /api/runs/{runId}/compute, navigate to results screen
 *
 * Screen routing:
 *   step 1 → DiscoveryFocusScreen
 *   step 2 → YourSystemsScreen
 *   step 3 → SourceWeightingScreen
 *   step 4 → DiscoveryPlanScreen
 *
 * Multi-pack selection:
 *   Pack is selected from state.industryId via pack hints from Task 12.
 *   resolvePackId(state) — see inline function below.
 *   Falls back to 'service_cloud' if no industry or no pack hints.
 *
 * Session persistence:
 *   useStackBuilderPersistence(orgId, state, setState)
 *   Restores saved session on mount. Auto-saves on state change (1s debounce).
 *   Clears session after successful launch.
 *
 * Props:
 *   orgId     — the org identifier for session persistence
 *   onComplete — called with runId after successful launch
 *   apiBase   — base URL for API calls (defaults to '')
 *   token     — JWT dev token for authentication
 */

import React, { useCallback, useEffect, useRef } from 'react';
import TopNav from '../components/common/TopNav';
import { useSetupState } from '../components/stack_builder';
import DiscoveryFocusPage   from '../pages/DiscoveryFocusPage';
import YourSystemsPage      from '../pages/YourSystemsPage';
import SourceWeightingPage  from '../pages/SourceWeightingPage';
import DiscoveryPlanPage    from '../pages/DiscoveryPlanPage';

// ── Pack resolution ───────────────────────────────────────────────────────────
// Translates setup state into the pack ID the runner expects.
// Industry pack hints (from industry_registry.py) drive selection.
// First hint for the industry is used — the registry orders by relevance.
// Falls back to 'service_cloud' for unknown or null industryId.

const INDUSTRY_PACK_HINTS: Record<string, string[]> = {
  financial_services:      ['ncino', 'service_cloud'],
  public_sector:           ['strs_benefits', 'service_cloud'],
  logistics_supply_chain:  ['service_cloud'],
  retail_commerce:         ['service_cloud'],
  healthcare:              ['service_cloud'],
  energy_utilities:        ['service_cloud'],
  manufacturing:           ['service_cloud'],
  technology:              ['service_cloud'],
};

function resolvePackId(state: ReturnType<typeof useSetupState>['state']): string {
  if (!state.industryId) return 'service_cloud';
  const hints = INDUSTRY_PACK_HINTS[state.industryId];
  if (!hints || hints.length === 0) return 'service_cloud';
  return hints[0];
}

// ── System ID normalisation ───────────────────────────────────────────────────
// The runner's 'systems' param maps to connector IDs understood by ingestors.
// Salesforce cloud IDs (salesforce_pss etc.) resolve to 'salesforce'.
// All other IDs are passed through as-is.

const SALESFORCE_CLOUD_IDS = new Set([
  'salesforce_pss', 'salesforce_sc', 'salesforce_ncino',
  'salesforce_fsc', 'salesforce_rc', 'salesforce_hc',
]);

function normaliseSystems(selectedIds: string[]): string[] {
  const normalised = selectedIds.map(id =>
    SALESFORCE_CLOUD_IDS.has(id) ? 'salesforce' : id
  );
  // Deduplicate — multiple Salesforce clouds should not produce duplicate 'salesforce'
  return [...new Set(normalised)];
}

// ── Session persistence hook ──────────────────────────────────────────────────

function useStackBuilderPersistence(
  orgId: string,
  setupState: ReturnType<typeof useSetupState>,
  apiBase: string,
  token: string
) {
  const { state } = setupState;
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const headers = { 
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${token}` 
  };

  // Restore on mount
  useEffect(() => {
    if (!orgId) return;
    fetch(`${apiBase}/api/stack-builder/setup-state/${encodeURIComponent(orgId)}`, {
      credentials: 'omit',
      headers,
    })
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data?.state && setupState.state.currentStep === 1) {
          // Only restore if user hasn't started navigating (still on step 1 at default)
          // Parent can implement more sophisticated conflict resolution if needed
          setupState.restoreState?.(data.state);
        }
      })
      .catch(() => {
        // Silent failure — no saved session is not an error
      });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId]);

  // Auto-save on state change (debounced 1s)
  useEffect(() => {
    if (!orgId) return;
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      fetch(
        `${apiBase}/api/stack-builder/setup-state/${encodeURIComponent(orgId)}`,
        {
          method: 'POST',
          credentials: 'omit',
          headers,
          body: JSON.stringify({
            state,
            saved_at: new Date().toISOString(),
          }),
        },
      ).catch(() => {
        // Silent failure — session save failure is non-blocking
      });
    }, 1000);
    return () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
    };
  }, [state, orgId, apiBase, token]);

  // Clear session (call after successful launch)
  const clearSession = useCallback(() => {
    if (!orgId) return;
    fetch(
      `${apiBase}/api/stack-builder/setup-state/${encodeURIComponent(orgId)}`,
      { 
        method: 'DELETE', 
        credentials: 'omit',
        headers 
      },
    ).catch(() => {});
  }, [orgId, apiBase, token]);

  return { clearSession };
}

// ── Router ────────────────────────────────────────────────────────────────────

interface Props {
  orgId: string;
  onComplete: (runId: string) => void;
  apiBase?: string;
  token?: string; // Add token prop here
}

export default function StackBuilderPage({
  orgId,
  onComplete,
  apiBase = '',
  token = 'dev-token-change-me', // Default value for development
}: Props) {
  const setupState = useSetupState();
  const { state } = setupState;
  
  // Pass the token to the persistence hook
  const { clearSession } = useStackBuilderPersistence(orgId, setupState, apiBase, token);

  const authHeaders = {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${token}`
  };

  // ── Launch handler ──────────────────────────────────────────────────────────
  const handleLaunch = useCallback(async () => {
    const packId = resolvePackId(state);
    const systems = normaliseSystems(state.selectedSystemIds);

    // 1. Start a run via the launch endpoint (Task 12 addition)
    let runId: string;
    try {
      const launchResp = await fetch(`${apiBase}/api/stack-builder/launch`, {
        method: 'POST',
        credentials: 'omit',
        headers: authHeaders,
        body: JSON.stringify({
          org_id: orgId,
          focus_id: state.focusId,
          industry_id: state.industryId,
          template_id: state.templateId,
          selected_system_ids: state.selectedSystemIds,
          pack_id: packId,
          weightings: state.weightings,
        }),
      });
      if (!launchResp.ok) {
        throw new Error(`Launch failed: ${launchResp.status}`);
      }
      const launchData = await launchResp.json();
      runId = launchData.runId;
    } catch (err) {
      console.error('[StackBuilderRouter] Launch failed:', err);
      return;
    }

    // 2. Trigger the compute run
    try {
      await fetch(`${apiBase}/api/runs/${runId}/compute`, {
        method: 'POST',
        credentials: 'omit',
        headers: authHeaders,
        body: JSON.stringify({
          mode: 'offline',
          systems,
          pack: packId,
        }),
      });
    } catch (err) {
      console.error('[StackBuilderRouter] Compute trigger failed:', err);
      return;
    }

    // 3. Clear saved session — successful launch starts fresh next time
    clearSession();

    // 4. Hand off to parent
    onComplete(runId);
  }, [state, orgId, apiBase, clearSession, onComplete, token]);

  // ── Screen routing ──────────────────────────────────────────────────────────
  return (
    <div className="min-h-screen text-text bg-bg">
      <TopNav />
      <div className="w-full">
        {state.currentStep === 1 && (
          <DiscoveryFocusPage setupState={setupState} />
        )}
        {state.currentStep === 2 && (
          <YourSystemsPage setupState={setupState} />
        )}
        {state.currentStep === 3 && (
          <SourceWeightingPage setupState={setupState} />
        )}
        {state.currentStep === 4 && (
          <DiscoveryPlanPage
            setupState={setupState}
            onLaunch={handleLaunch}
          />
        )}
      </div>
    </div>
  );
}