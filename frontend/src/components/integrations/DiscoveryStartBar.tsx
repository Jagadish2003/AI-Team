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
  canStart: boolean;
  onStart: () => void;
  /** @deprecated T41-8: use SourceConfigPanel in Integration Hub right panel */
  onUpload?: () => void;
}) {
  const step = confidence.toLowerCase();
  const isLow = step === "low";
  const isMedium = step === "medium";
  const isHigh = step === "high";

  const microcopy = !canStart
    ? DISCOVERY_SOURCE_REQUIREMENT_MESSAGE
    : recommendedReadyCount === 2
      ? "Connect and configure one more source to reach HIGH confidence."
      : null;

  return (
    <div className="fixed bottom-0 left-0 right-0 z-40 max-h-[46vh] overflow-y-auto border-t border-border bg-panel/95 shadow-[0_-8px_24px_rgba(7,25,58,0.10)] backdrop-blur">
      <div className="w-full px-4 py-3 sm:px-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex min-w-0 flex-wrap items-center gap-y-2 text-sm">
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

            <div className="mt-5 whitespace-nowrap text-sm text-muted">
              Ready : <span className="text-text">{recommendedReadyCount}</span>{" "}
              of <span className="text-text">{recommendedTotal}</span>{" "}
              recommended
            </div>
          </div>

          <div className="min-w-0">
            <div className="flex max-w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-border px-3 py-1.5 text-sm">
              {recommended.map((connector, index) => {
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

            <div className="mt-3 whitespace-nowrap text-sm text-muted">
              CONFIDENCE:{" "}
              <span className="font-semibold uppercase text-text">
                {confidence}
              </span>
            </div>
          </div>

          <button
            onClick={onStart}
            disabled={!canStart}
            className="flex items-center gap-2 whitespace-nowrap rounded-lg bg-accent px-5 py-2 text-sm font-medium text-textwhite transition-all hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Start Discovery Run
            <MoveRight size={18} strokeWidth={2} />
          </button>
        </div>

        {microcopy && (
          <div className="mt-2 flex justify-end">
            <div className="rounded-md bg-panel2 px-3 py-1.5 text-sm text-muted">
              {microcopy}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
