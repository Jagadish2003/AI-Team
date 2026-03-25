export type Confidence = 'LOW' | 'MEDIUM' | 'HIGH';
export function computeConfidence(recommendedConnectedCount: number): Confidence {
  if (recommendedConnectedCount <= 1) return 'LOW';
  if (recommendedConnectedCount === 2) return 'MEDIUM';
  return 'HIGH';
}
