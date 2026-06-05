import React from "react";
import {
  AlertTriangle,
  CircleCheck,
  History,
  Info,
  TrendingUp,
  TrendingDown,
} from "lucide-react";
import type { OppEnrichment } from "../../api/enrichmentApi";

interface Props {
  enrichment: OppEnrichment | null;
}

export default function BaselineContextPanel({ enrichment }: Props) {
  if (!enrichment) return null;

  const {
    baseline_context,
    trend_direction,
    is_anomalous,
    first_deviation,
    run_count,
  } = enrichment;

  // Hide panel when no temporal context at all
  if (!baseline_context && !trend_direction) return null;

  // Insufficient data — fewer than 3 runs
  if (run_count !== null && run_count < 3) {
    return (
      <div>
        <div className="mb-2 text-xs font-semibold text-text">Baseline Context</div>
        <div className="rounded-lg border border-border bg-bg/30 p-3">
          <div className="flex items-start gap-3">
            <span className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-blue-500/25 bg-blue-500/10 text-blue-500">
              <History size={15} aria-hidden="true" />
            </span>
            <p className="pt-1 text-xs text-text leading-relaxed">
              Baseline context will appear after 3 or more discovery runs.
            </p>
          </div>
        </div>
      </div>
    );
  }

  const direction = trend_direction?.toLowerCase();

  const trendStyle =
    direction === "rising"
      ? {
          icon: TrendingUp,
          shell: "border-amber-500/25 bg-amber-500/12 text-amber-500",
        }
      : direction === "falling"
        ? {
            icon: TrendingDown,
            shell: "border-teal-500/25 bg-teal-500/12 text-teal-500",
          }
        : {
            icon: CircleCheck,
            shell: "border-blue-500/25 bg-blue-500/12 text-blue-500",
          };

  const TrendIcon = trendStyle.icon;

  return (
    <div>
      <div className="mb-2 text-xs font-semibold text-text">Baseline Context</div>

      <div className="rounded-lg border border-border bg-bg/30 p-3 space-y-2">
        {/* Trend row */}
        <div className="flex items-start gap-3">
          <span
            className={`inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border ${trendStyle.shell}`}
          >
            <TrendIcon size={16} aria-hidden="true" />
          </span>
          <span className="pt-1 text-xs text-text leading-relaxed">
            {baseline_context}
          </span>
        </div>

        {/* Badges row */}
        <div className="flex flex-wrap gap-2 pl-11">
          {is_anomalous && (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-500/15 px-2.5 py-0.5 text-[11px] font-medium text-amber-500 border border-amber-500/30">
              <AlertTriangle size={12} aria-hidden="true" />
              Anomaly detected
            </span>
          )}
          {first_deviation && (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-blue-500/15 px-2.5 py-0.5 text-[11px] font-medium text-blue-500 border border-blue-500/30">
              <Info size={12} aria-hidden="true" />
              First deviation from stable baseline
            </span>
          )}
        </div>

        {/* Run count footnote */}
        {run_count !== null && (
          <div className="flex items-center gap-1.5 pl-11 text-xs text-text leading-relaxed">
            <History size={11} className="text-blue-500/80" aria-hidden="true" />
            <span>
              Based on {run_count} run{run_count === 1 ? "" : "s"}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
