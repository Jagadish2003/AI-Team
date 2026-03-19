import { Connector } from '../types/connector';
export function getNextBestRecommended(recommended: Connector[]): string | null {
  const next = recommended.slice().sort((a,b)=>(a.recommendedRank??999)-(b.recommendedRank??999)).find(c=>c.status!=='connected');
  return next ? next.id : null;
}
