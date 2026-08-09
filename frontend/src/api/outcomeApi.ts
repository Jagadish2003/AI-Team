import { apiGet, apiPost } from '../lib/apiClient';
import type {
  OpportunityLifecycle,
  OpportunityOutcomeView,
  OutcomePortfolioView,
} from '../types/outcome';

export interface OutcomePortfolioParams {
  comparabilityVerdict?: string[];
  projectionVerdict?: string[];
  pack?: string[];
  detector?: string[];
  confidence?: string[];
  limit?: number;
}

function appendList(params: URLSearchParams, key: string, values?: string[]) {
  (values ?? []).forEach((value) => {
    if (value) params.append(key, value);
  });
}

export function fetchOutcomePortfolio(
  filters: OutcomePortfolioParams = {},
): Promise<OutcomePortfolioView> {
  const params = new URLSearchParams();
  appendList(params, 'comparabilityVerdict', filters.comparabilityVerdict);
  appendList(params, 'projectionVerdict', filters.projectionVerdict);
  appendList(params, 'pack', filters.pack);
  appendList(params, 'detector', filters.detector);
  appendList(params, 'confidence', filters.confidence);
  if (filters.limit) params.set('limit', String(filters.limit));
  const suffix = params.toString() ? `?${params.toString()}` : '';
  return apiGet<OutcomePortfolioView>(`/api/outcomes${suffix}`);
}

export function fetchOpportunityOutcome(
  opportunityIdentity: string,
): Promise<OpportunityOutcomeView> {
  return apiGet<OpportunityOutcomeView>(
    `/api/outcomes/${encodeURIComponent(opportunityIdentity)}`,
  );
}

function lifecyclePath(opportunityIdentity: string, action?: string): string {
  const base = `/api/opportunity-lifecycle/${encodeURIComponent(opportunityIdentity)}`;
  return action ? `${base}/${action}` : base;
}

export function fetchOpportunityLifecycle(
  opportunityIdentity: string,
): Promise<OpportunityLifecycle> {
  return apiGet<OpportunityLifecycle>(lifecyclePath(opportunityIdentity));
}

export function recordOpportunityAction(
  opportunityIdentity: string,
  actionDate: string,
  note?: string,
): Promise<OpportunityLifecycle> {
  const trimmedNote = note?.trim();
  return apiPost<OpportunityLifecycle>(lifecyclePath(opportunityIdentity, 'action'), {
    actionDate,
    ...(trimmedNote ? { note: trimmedNote } : {}),
  });
}

export function dismissOpportunity(
  opportunityIdentity: string,
): Promise<OpportunityLifecycle> {
  return apiPost<OpportunityLifecycle>(lifecyclePath(opportunityIdentity, 'dismiss'), {});
}

export function reopenOpportunity(
  opportunityIdentity: string,
): Promise<OpportunityLifecycle> {
  return apiPost<OpportunityLifecycle>(lifecyclePath(opportunityIdentity, 'reopen'), {});
}
