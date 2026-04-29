import { Connector } from '../types/connector';
import { isDiscoveryReadyConnector } from './sourceReadiness';

export function getNextBestRecommended(recommended: Connector[]): string | null {
  const next = recommended
    .slice()
    .sort((a, b) => (a.recommendedRank ?? 999) - (b.recommendedRank ?? 999))
    .find((c) => !isDiscoveryReadyConnector(c));
  return next ? next.id : null;
}
