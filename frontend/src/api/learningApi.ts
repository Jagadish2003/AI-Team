import { apiGet } from '../lib/apiClient';
import type { LearningSignalSetResponse } from '../types/learning';

export function fetchLearningSignals(): Promise<LearningSignalSetResponse> {
  return apiGet<LearningSignalSetResponse>('/api/learning/signals');
}
