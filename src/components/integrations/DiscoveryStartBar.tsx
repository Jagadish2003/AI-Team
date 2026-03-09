import React from "react";
import { Check } from "lucide-react";

export default function DiscoveryStartBar({
  confidence = "high", // low | medium | high
  recommendedConnectedCount,
  recommendedTotal,
  canStart,
  onStart,
  onUpload
}: any) {

  const step = confidence?.toLowerCase();

  const isLow = step === "low";
  const isMedium = step === "medium";
  const isHigh = step === "high";

  return (
    <div className="fixed bottom-0 left-0 right-0 z-50 w-full">
      <div className="w-full border-t border-border bg-panel shadow-xl px-6 py-4">

        {/* ROW 1 */}
        <div className="flex flex-wrap items-center justify-between gap-6">

          {/* LOW MEDIUM HIGH BAR */}
          <div className="flex items-center text-sm">

            {/* LOW */}
            <div className="flex items-center">
              <div
                className={`h-3 w-3 rounded-full ${
                  isLow ? "bg-accent shadow-[0_0_8px_3px_rgba(0,255,200,0.35)]" : "bg-border"
                }`}
              />
              <span className={`ml-2 ${isLow ? "text-text font-semibold" : "text-muted"}`}>
                Low
              </span>
            </div>

            {/* LINE */}
            <div className="h-[1px] w-24 bg-border mx-3"></div>

            {/* MEDIUM */}
            <div className="flex items-center">
              <div
                className={`h-3 w-3 rounded-full ${
                  isMedium ? "bg-accent shadow-[0_0_8px_3px_rgba(0,255,200,0.35)]" : "bg-border"
                }`}
              />
              <span className={`ml-2 ${isMedium ? "text-text font-semibold" : "text-muted"}`}>
                Medium
              </span>
            </div>

            {/* LINE */}
            <div className="h-[1px] w-24 bg-border mx-3"></div>

            {/* HIGH */}
            <div className="flex items-center">
              <div
                className={`h-3 w-3 rounded-full ${
                  isHigh ? "bg-accent" : "bg-border"
                }`}
              />
              <span className={`ml-2 ${isHigh ? "text-text font-semibold" : "text-muted"}`}>
                High
              </span>
            </div>

          </div>

          {/* CONNECT TEXT */}
          <div className="text-sm text-muted">
            Connect: {recommendedConnectedCount} of {recommendedTotal} recommended
            <span className="mx-2">|</span>ReCx
          </div>

          {/* START BUTTON */}
          <button
            onClick={onStart}
            disabled={!canStart}
            className="rounded-lg bg-accent px-6 py-2 text-sm font-medium text-black whitespace-nowrap disabled:opacity-50"
          >
            Start Discovery Run →
          </button>

        </div>

        {/* ROW 2 */}
        <div className="flex flex-wrap items-center justify-between gap-4 mt-2">

          <div className="flex items-center gap-3 flex-wrap">

            <button
              onClick={onUpload}
              className="rounded-md border border-border px-4 py-1.5 text-sm text-text hover:bg-panel2 transition"
            >
              Upload Files Instead
            </button>

            <div className="flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm">
            <span className="flex items-start gap-2 text-sm text-muted">
            <Check size={16} className="mt-[2px] shrink-0 text-muted" />
            <span>Jira</span>
            </span>
              <span className="text-border mx-1">|</span>

              <span className="flex items-center gap-1 text-muted">
                <span className="h-2 w-2 rounded-sm bg-muted inline-block"></span>
                Not Connected
              </span>

              <span className="text-border mx-1">|</span>

              <span className="flex items-center gap-1">
                <span className="h-2 w-2 rounded-sm bg-muted inline-block"></span>
                Microsoft 365
              </span>

              <span className="text-border mx-1">|</span>

              <span className="flex items-center gap-1 text-muted">
                <span className="h-2 w-2 rounded-sm bg-muted inline-block"></span>
                Not Connected
              </span>
            </div>

            <div className="text-sm text-muted whitespace-nowrap">
              CONFIDENCE:{" "}
              <span className="text-text font-semibold uppercase">{confidence}</span>
            </div>

          </div>

          <div className="rounded-md bg-panel2 px-3 py-1.5 text-sm">
            Connect one more Source to reach HIGH
          </div>

        </div>

      </div>
    </div>
  );
}