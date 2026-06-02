import React from "react";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";
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
      <div className="rounded-lg border border-border bg-bg/30 px-4 py-3">
        <div className="text-xs font-semibold text-text mb-1">
          Baseline Context
        </div>
        <p className="text-xs text-muted">
          Baseline context will appear after 3 or more discovery runs.
        </p>
      </div>
    );
  }

  const direction = trend_direction?.toLowerCase();

  const TrendIcon =
    direction === "rising"
      ? TrendingUp
      : direction === "falling"
        ? TrendingDown
        : Minus;

  const iconColor =
    direction === "rising"
      ? "text-amber-400"
      : direction === "falling"
        ? "text-teal-400"
        : "text-muted";

  return (
    <div className="rounded-lg border border-border bg-bg/30 px-4 py-3 space-y-2">
      <div className="text-xs font-semibold text-text">Baseline Context</div>

      {/* Trend row */}
      <div className="flex items-start gap-2">
        <TrendIcon size={14} className={`${iconColor} mt-0.5 shrink-0`} />
        <span className="text-xs text-text leading-relaxed">
          {baseline_context}
        </span>
      </div>

      {/* Badges row */}
      <div className="flex flex-wrap gap-2">
        {is_anomalous && (
          <span className="inline-flex items-center rounded-full bg-amber-500/15 px-2.5 py-0.5 text-[11px] font-medium text-amber-400 border border-amber-500/30">
            Anomaly detected
          </span>
        )}
        {first_deviation && (
          <span className="inline-flex items-center rounded-full bg-blue-500/15 px-2.5 py-0.5 text-[11px] font-medium text-blue-400 border border-blue-500/30">
            First deviation from stable baseline
          </span>
        )}
      </div>

      {/* Run count footnote */}
      {run_count !== null && (
        <p className="text-[10px] text-muted font-mono">
          Based on {run_count} run{run_count === 1 ? "" : "s"}
        </p>
      )}
    </div>
  );
}
