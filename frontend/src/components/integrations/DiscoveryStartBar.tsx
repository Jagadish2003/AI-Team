import React from "react";
import { Check, MoveRight, X } from "lucide-react";
import { Confidence } from "../../utils/confidence";
import { Connector } from "../../types/connector";
import {
  DISCOVERY_SOURCE_REQUIREMENT_MESSAGE,
  isDiscoveryReadyConnector,
} from "../../utils/sourceReadiness";

export default function DiscoveryStartBar({
  confidence,
  recommendedReadyCount,
  recommendedTotal,
  recommended,
  statusConnectors,
  canStart,
  onStart,
  // T41-8: onUpload removed. File upload is now in the Integration Hub
  // right panel (SourceConfigPanel). Prop kept for backward compatibility.
  onUpload: _onUpload,
}: {
  confidence: Confidence;
  recommendedReadyCount: number;
  recommendedTotal: number;
  recommended: Connector[];
  statusConnectors?: Connector[];
  canStart: boolean;
  onStart: () => void;
  /** @deprecated T41-8: use SourceConfigPanel in Integration Hub right panel */
  onUpload?: () => void;
}) {
  const step = confidence.toLowerCase();
  const isLow = step === "low";
  const isMedium = step === "medium";
  const isHigh = step === "high";
  const displayedConnectors = statusConnectors ?? recommended;

  const microcopy = !canStart
    ? DISCOVERY_SOURCE_REQUIREMENT_MESSAGE
    : recommendedReadyCount === 2
      ? "Connect and configure one more source to reach HIGH confidence."
      : null;

  return (
    <div className="discovery-start-bar fixed bottom-0 left-0 right-0 z-40 max-h-[36vh] overflow-y-auto border-t border-border backdrop-blur">
      <div className="w-full px-4 py-4 sm:px-6">
        <div className="grid gap-x-5 gap-y-2 xl:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] xl:items-center">
          <div className="flex min-w-0 flex-wrap items-center gap-y-1 text-sm">
            <div className="flex items-center">
              <div
                className={`h-2.5 w-2.5 rounded-full ${isLow ? "bg-accent" : "bg-muted/40"}`}
              />
              <span
                className={`ml-2 ${isLow ? "font-semibold text-text" : "text-muted"}`}
              >
                Low
              </span>
            </div>
            <div
              className={`mx-3 h-[1px] w-10 transition-colors sm:w-16 ${isMedium || isHigh ? "bg-accent/50" : "bg-border"}`}
            />
            <div className="flex items-center">
              <div
                className={`h-2.5 w-2.5 rounded-full ${isMedium ? "bg-accent" : "bg-muted/40"}`}
              />
              <span
                className={`ml-2 ${isMedium ? "font-semibold text-text" : "text-muted"}`}
              >
                Medium
              </span>
            </div>
            <div
              className={`mx-3 h-[1px] w-10 transition-colors sm:w-16 ${isHigh ? "bg-accent/50" : "bg-border"}`}
            />
            <div className="flex items-center">
              <div
                className={`h-2.5 w-2.5 rounded-full ${isHigh ? "bg-accent" : "bg-muted/40"}`}
              />
              <span
                className={`ml-2 ${isHigh ? "font-semibold text-text" : "text-muted"}`}
              >
                High
              </span>
            </div>
          </div>

          <div className="flex min-w-0 flex-wrap items-center justify-center gap-x-2 gap-y-1 justify-self-start rounded-md border border-border px-2.5 py-1 text-sm xl:justify-self-center">
            {displayedConnectors.map((connector, index) => {
              const isReady = isDiscoveryReadyConnector(connector);
              const statusLabel = isReady
                ? "Ready"
                : connector.status === "connected"
                  ? "Needs Sync"
                  : "Not Connected";
              return (
                <React.Fragment key={connector.id}>
                  {index > 0 && (
                    <span className="hidden text-muted sm:inline">|</span>
                  )}
                  <span className="flex items-center gap-1.5">
                    {isReady ? (
                      <Check
                        size={14}
                        strokeWidth={2.5}
                        className="shrink-0 text-accent"
                      />
                    ) : (
                      <X
                        size={14}
                        strokeWidth={2.5}
                        className="shrink-0 text-muted"
                      />
                    )}
                    <span className={isReady ? "text-text" : "text-muted"}>
                      {connector.name}
                    </span>
                    <span
                      className={`text-xs ${isReady ? "text-accent" : "text-muted"}`}
                    >
                      {statusLabel}
                    </span>
                  </span>
                </React.Fragment>
              );
            })}
          </div>

          <button
            onClick={onStart}
            disabled={!canStart}
            className="flex items-center gap-2 justify-self-start whitespace-nowrap rounded-md border border-accent/20 bg-accent/5 px-4 py-1.5 text-sm font-medium text-accent transition-all hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50 xl:justify-self-end"
          >
            Start Discovery Run
            <MoveRight size={17} strokeWidth={2} />
          </button>

          <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-1 text-sm text-muted xl:col-span-3">
            <div className="flex min-w-0 flex-wrap items-center gap-x-6 gap-y-1">
              <span className="whitespace-nowrap">
                Ready: <span className="text-text">{recommendedReadyCount}</span>{" "}
                of <span className="text-text">{recommendedTotal}</span>{" "}
                recommended
              </span>
              <span className="whitespace-nowrap">
                CONFIDENCE:{" "}
                <span className="font-semibold uppercase text-text">
                  {confidence}
                </span>
              </span>
            </div>
            {microcopy && (
              <div className="rounded-md bg-panel2 px-2.5 py-1 text-sm text-muted">
                {microcopy}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
