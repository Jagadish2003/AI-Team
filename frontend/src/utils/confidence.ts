export type Confidence = 'LOW' | 'MEDIUM' | 'HIGH';
export function computeConfidence(recommendedConnectedCount: number): Confidence {
  if (recommendedConnectedCount <= 0) return 'LOW';
  if (recommendedConnectedCount === 1) return 'MEDIUM';
  return 'HIGH';
}
