import React, { createContext, useContext, useState } from "react";
import data from "../data/mockConnectors.json";
import { Connector } from "../types/connector";
import { calculateConfidence } from "../utils/confidence";
import { getNextBest } from "../utils/nextBest";

interface ConnectorContextType {
  connectors: Connector[];
  selected?: Connector;
  confidence: string;
  nextBest?: Connector;
  selectConnector: (id: string) => void;
  connectConnector: (id: string) => void;
}

const ConnectorContext = createContext<ConnectorContextType | null>(null);

export const ConnectorProvider: React.FC<{ children: React.ReactNode }> = ({
  children
}) => {

  const initialConnectors: Connector[] = [
    ...data.recommended,
    ...data.connectors
  ];

  const [connectors, setConnectors] = useState<Connector[]>(initialConnectors);
  const [selectedId, setSelectedId] = useState<string>();

  const selectConnector = (id: string) => {
    setSelectedId(id);
  };

  const connectConnector = (id: string) => {
    setConnectors(prev =>
      prev.map(c =>
        c.id === id
          ? { ...c, status: "connected", lastSynced: "just now" }
          : c
      )
    );
  };

  const selected = connectors.find(c => c.id === selectedId);
  const confidence = calculateConfidence(connectors);
  const nextBest = getNextBest(connectors);

  return (
    <ConnectorContext.Provider
      value={{
        connectors,
        selected,
        confidence,
        nextBest,
        selectConnector,
        connectConnector
      }}
    >
      {children}
    </ConnectorContext.Provider>
  );
};

export const useConnectors = () => {
  const ctx = useContext(ConnectorContext);
  if (!ctx) throw new Error("ConnectorContext missing");
  return ctx;
};