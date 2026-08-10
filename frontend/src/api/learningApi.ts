import { apiGet, apiPost } from '../lib/apiClient';
import type {
  LearningAdjustmentHistoryEntry,
  LearningAdjustmentResetResponse,
  LearningAdjustmentStateResponse,
  LearningSignalSetResponse,
} from '../types/learning';

export function fetchLearningSignals(): Promise<LearningSignalSetResponse> {
  return apiGet<LearningSignalSetResponse>('/api/learning/signals');
}

export function fetchLearningAdjustmentState(): Promise<LearningAdjustmentStateResponse> {
  return apiGet<LearningAdjustmentStateResponse>('/api/learning/adjustment');
}

export function fetchLearningAdjustmentHistory(
  limit = 1000,
): Promise<LearningAdjustmentHistoryEntry[]> {
  return apiGet<LearningAdjustmentHistoryEntry[]>(
    `/api/learning/adjustment/history?limit=${limit}`,
  );
}

export function resetLearningAdjustment(
  reason: string,
): Promise<LearningAdjustmentResetResponse> {
  return apiPost<LearningAdjustmentResetResponse>('/api/learning/adjustment/reset', {
    reason,
  });
}
