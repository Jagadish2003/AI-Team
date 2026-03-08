import { Connector } from "../types/connector";

export function calculateConfidence(connectors: Connector[]) {
  const connected = connectors.filter(c => c.status === "connected").length;

  if (connected >= 3) return "HIGH";
  if (connected >= 2) return "MEDIUM";
  return "LOW";
}