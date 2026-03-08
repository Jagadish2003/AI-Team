import { Connector } from "../types/connector";

export function getNextBest(connectors: Connector[]) {
  return connectors
    .filter(c => c.tier === "recommended" && c.status !== "connected")
    .sort((a, b) => (a.recommendedRank || 0) - (b.recommendedRank || 0))[0];
}