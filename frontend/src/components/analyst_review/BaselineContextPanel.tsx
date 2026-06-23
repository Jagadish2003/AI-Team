import React from "react";
import {
  AlertTriangle,
  ArrowRight,
  CircleCheck,
  CircleDot,
  History,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { OppEnrichment } from "../../api/enrichmentApi";

interface Props {
  enrichment: OppEnrichment | null;
}

type BaselineState =
  | "insufficient"
  | "stable"
  | "rising"
  | "falling"
  | "first_deviation"
  | "anomaly_rising"
  | "anomaly_falling";

interface StateStyle {
  Icon: LucideIcon;
  icon: string;
  primaryBadge: string;
  arrow: string;
}

interface Chip {
  label: string;
  tone?: keyof typeof CHIP_TONES;
}

interface BaselineConfig {
  title: string;
  whyTitle: string;
  why: string;
  badges: Chip[];
  detailChips: Chip[];
}

interface BaselineMetricContext {
  latestValue: number | null;
  baseline: number | null | undefined;
  change: number | null;
  windowDays: number;
}

const MIN_BASELINE_RUNS = 3;
const DEFAULT_WINDOW_DAYS = 90;

const CHIP_TONES = {
  blue: "border-blue-500/35 bg-blue-500/10 text-blue-300",
  amber: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  emerald: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  teal: "border-teal-500/40 bg-teal-500/10 text-teal-300",
  violet: "border-violet-500/40 bg-violet-500/10 text-violet-300",
  red: "border-red-500/40 bg-red-500/10 text-red-300",
  neutral: "border-border bg-bg/30 text-text",
} as const;

const STATE_STYLES: Record<BaselineState, StateStyle> = {
  insufficient: {
    Icon: History,
    icon: "border-blue-500/35 bg-blue-500/10 text-blue-300",
    primaryBadge: CHIP_TONES.blue,
    arrow: "text-blue-400",
  },
  stable: {
    Icon: CircleCheck,
    icon: "border-blue-500/35 bg-blue-500/10 text-blue-300",
    primaryBadge: CHIP_TONES.blue,
    arrow: "text-blue-400",
  },
  rising: {
    Icon: TrendingUp,
    icon: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
    primaryBadge: CHIP_TONES.emerald,
    arrow: "text-emerald-400",
  },
  falling: {
    Icon: TrendingDown,
    icon: "border-amber-500/40 bg-amber-500/10 text-amber-300",
    primaryBadge: CHIP_TONES.amber,
    arrow: "text-amber-400",
  },
  first_deviation: {
    Icon: CircleDot,
    icon: "border-violet-500/40 bg-violet-500/10 text-violet-300",
    primaryBadge: CHIP_TONES.violet,
    arrow: "text-violet-400",
  },
  anomaly_rising: {
    Icon: AlertTriangle,
    icon: "border-red-500/40 bg-red-500/10 text-red-300",
    primaryBadge: CHIP_TONES.red,
    arrow: "text-red-400",
  },
  anomaly_falling: {
    Icon: AlertTriangle,
    icon: "border-red-500/40 bg-red-500/10 text-red-300",
    primaryBadge: CHIP_TONES.red,
    arrow: "text-red-400",
  },
};

function formatValue(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "N/A";
  if (Number.isInteger(value)) return String(value);
  if (Math.abs(value) < 1) return value.toFixed(2);
  return value.toFixed(2).replace(/\.?0+$/, "");
}

function formatPercent(value: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "N/A";
  const rounded = Math.round(value);
  return `${rounded > 0 ? "+" : ""}${rounded}%`;
}

function baselineComparisonPhrase(
  change: number | null,
  windowDays: number,
): string | null {
  if (typeof change !== "number" || !Number.isFinite(change)) return null;
  const rounded = Math.round(change);
  if (rounded === 0) return `at the ${windowDays}-day baseline`;
  const direction = rounded > 0 ? "above" : "below";
  return `${Math.abs(rounded)}% ${direction} the ${windowDays}-day baseline`;
}

function labelize(value: string | null | undefined): string {
  if (!value) return "Current Discovery Pack";
  const known: Record<string, string> = {
    ncino: "nCino",
    service_cloud: "Service Cloud",
    sqlserver_opsignal: "SQL Server Opsignal",
    strs_benefits: "STRS Benefits",
  };
  const normalised = value.toLowerCase();
  if (known[normalised]) return known[normalised];
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function lastNumber(values: number[] | undefined): number | null {
  if (!values || values.length === 0) return null;
  const value = values[values.length - 1];
  return Number.isFinite(value) ? value : null;
}

function latestSignalValue(enrichment: OppEnrichment, recentValues: number[]): number | null {
  // Use the same latest point shown in "Recent runs" so the visible numbers reconcile.
  return lastNumber(recentValues) ?? enrichment.current_value ?? null;
}

function changePct(current: number | null, baseline: number | null | undefined): number | null {
  if (
    typeof current !== "number" ||
    !Number.isFinite(current) ||
    typeof baseline !== "number" ||
    !Number.isFinite(baseline) ||
    baseline === 0
  ) {
    return null;
  }
  return ((current - baseline) / baseline) * 100;
}

function pluralRun(count: number | null | undefined): string | null {
  if (typeof count !== "number" || !Number.isFinite(count)) return null;
  return `Based on ${count} run${count === 1 ? "" : "s"}`;
}

function classify(enrichment: OppEnrichment): BaselineState {
  const direction = enrichment.trend_direction?.toLowerCase();
  const insufficient =
    direction === "insufficient_data" ||
    (typeof enrichment.run_count === "number" && enrichment.run_count < MIN_BASELINE_RUNS);

  if (insufficient) return "insufficient";
  if (enrichment.first_deviation) return "first_deviation";
  if (enrichment.is_anomalous) {
    return direction === "falling" ? "anomaly_falling" : "anomaly_rising";
  }
  if (direction === "rising") return "rising";
  if (direction === "falling") return "falling";
  return "stable";
}

function subtitleFor(
  state: BaselineState,
  enrichment: OppEnrichment,
  metrics: BaselineMetricContext,
): string {
  const { latestValue, baseline, change, windowDays } = metrics;
  const pct = Math.abs(Math.round(change ?? 0));
  const baselineComparison = baselineComparisonPhrase(change, windowDays);

  if (state === "insufficient") {
    return "Baseline context will appear after 3 or more discovery runs.";
  }
  if (state === "first_deviation") {
    return "First deviation from a previously stable baseline";
  }
  if (state === "anomaly_falling") {
    if (!baselineComparison && enrichment.baseline_context) return enrichment.baseline_context;
    return `Down ${pct}% from your ${windowDays}-day baseline of ${formatValue(baseline)}`;
  }
  if (state === "anomaly_rising") {
    if (!baselineComparison && enrichment.baseline_context) return enrichment.baseline_context;
    return `Up ${pct}% from your ${windowDays}-day baseline of ${formatValue(baseline)}`;
  }
  if (state === "rising") {
    if (baselineComparison) {
      return `Latest run is ${baselineComparison}. Overall trend is rising across recent runs.`;
    }
  }
  if (state === "falling") {
    if (baselineComparison) {
      return `Latest run is ${baselineComparison}. Overall trend is falling across recent runs.`;
    }
  }
  if (enrichment.baseline_context) {
    return enrichment.baseline_context;
  }
  if (latestValue !== null && baseline !== null && latestValue === baseline) {
    return `Stable - within normal range of your ${windowDays}-day baseline`;
  }
  return `Stable - within normal range of your ${windowDays}-day baseline`;
}

function configFor(
  state: BaselineState,
  enrichment: OppEnrichment,
  metrics: BaselineMetricContext,
): BaselineConfig {
  const currentRuns = enrichment.run_count ?? 0;
  const runBadge = pluralRun(enrichment.run_count);
  const { latestValue, baseline, change, windowDays } = metrics;
  const baselineLabel = `${windowDays}-day avg`;

  switch (state) {
    case "insufficient":
      return {
        title: "Not Enough History Yet",
        whyTitle: "Why Not Shown Yet?",
        why:
          "The system needs at least three completed runs from the same workspace and the same Discovery Pack before it can calculate a meaningful baseline.",
        badges: [
          { label: "Insufficient data", tone: "blue" },
          { label: `${currentRuns} of ${MIN_BASELINE_RUNS} runs`, tone: "neutral" },
        ] as Chip[],
        detailChips: [
          { label: `Current runs: ${currentRuns}`, tone: "neutral" },
          { label: `Required: ${MIN_BASELINE_RUNS}`, tone: "neutral" },
          { label: `Discovery Pack: ${labelize(enrichment.pack_id)}`, tone: "neutral" },
        ] as Chip[],
      };
    case "first_deviation":
      return {
        title: "First Deviation",
        whyTitle: "Why First Deviation?",
        why:
          "Earlier runs were perfectly stable, so the baseline had no variation. This run changed for the first time, which makes it important even though an anomaly score cannot be calculated yet.",
        badges: [
          { label: "First deviation from stable baseline", tone: "blue" },
          ...(runBadge ? [{ label: runBadge, tone: "neutral" as const }] : []),
        ],
        detailChips: [
          { label: `Previous baseline: ${formatValue(baseline)}`, tone: "neutral" },
          { label: `Latest: ${formatValue(latestValue)}`, tone: "neutral" },
          { label: `Std dev: ${formatValue(enrichment.baseline_stddev)}`, tone: "neutral" },
        ] as Chip[],
      };
    case "anomaly_falling":
    case "anomaly_rising":
      return {
        title: "Anomaly Detected",
        whyTitle: "Why Anomaly?",
        why:
          state === "anomaly_falling"
            ? "The current value dropped outside the expected baseline range. The change is statistically unusual compared with recent historical variation."
            : "The current value is far above the expected baseline range. It is more than two standard deviations away from the baseline, so it is highlighted for review.",
        badges: [
          { label: "Anomaly detected", tone: "red" },
          {
            label: state === "anomaly_falling" ? "Falling" : "Rising",
            tone: state === "anomaly_falling" ? "amber" : "emerald",
          },
          ...(runBadge ? [{ label: runBadge, tone: "neutral" as const }] : []),
        ],
        detailChips: [
          { label: `Latest: ${formatValue(latestValue)}`, tone: "neutral" },
          { label: `${baselineLabel}: ${formatValue(baseline)}`, tone: "neutral" },
          { label: `Latest vs avg: ${formatPercent(change)}`, tone: "neutral" },
        ] as Chip[],
      };
    case "rising":
      return {
        title: "Trending Up",
        whyTitle: "Why This Trend?",
        why:
          "AgentIQ calculates trend from the overall slope across recent runs. The latest run is compared separately against the baseline average.",
        badges: [
          { label: "Rising", tone: "emerald" },
          ...(runBadge ? [{ label: runBadge, tone: "neutral" as const }] : []),
        ],
        detailChips: [
          { label: `Latest: ${formatValue(latestValue)}`, tone: "neutral" },
          { label: `${baselineLabel}: ${formatValue(baseline)}`, tone: "neutral" },
          { label: `Latest vs avg: ${formatPercent(change)}`, tone: "neutral" },
        ] as Chip[],
      };
    case "falling":
      return {
        title: "Trending Down",
        whyTitle: "Why This Trend?",
        why:
          "AgentIQ calculates trend from the overall slope across recent runs. The latest run is compared separately against the baseline average.",
        badges: [
          { label: "Falling", tone: "amber" },
          ...(runBadge ? [{ label: runBadge, tone: "neutral" as const }] : []),
        ],
        detailChips: [
          { label: `Latest: ${formatValue(latestValue)}`, tone: "neutral" },
          { label: `${baselineLabel}: ${formatValue(baseline)}`, tone: "neutral" },
          { label: `Latest vs avg: ${formatPercent(change)}`, tone: "neutral" },
        ] as Chip[],
      };
    default:
      return {
        title: "Stable",
        whyTitle: "Why Stable?",
        why:
          "This signal stayed consistent across recent discovery runs. The current value is close to the baseline average, and the trend stayed inside the normal stability band.",
        badges: [
          { label: "Stable", tone: "blue" },
          ...(runBadge ? [{ label: runBadge, tone: "neutral" as const }] : []),
        ],
        detailChips: [
          { label: `Latest: ${formatValue(latestValue)}`, tone: "neutral" },
          { label: `${baselineLabel}: ${formatValue(baseline)}`, tone: "neutral" },
          { label: `Latest vs avg: ${formatPercent(change)}`, tone: "neutral" },
        ] as Chip[],
      };
  }
}

function ChipPill({ chip, primaryClass }: { chip: Chip; primaryClass?: string }) {
  const toneClass = chip.tone ? CHIP_TONES[chip.tone] : primaryClass ?? CHIP_TONES.neutral;
  return (
    <span
      className={`inline-flex min-h-7 max-w-full items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold leading-[1.35] ${toneClass}`}
    >
      <span className="block max-w-full truncate leading-[1.35]">{chip.label}</span>
    </span>
  );
}

function RecentValues({
  values,
  arrowClass,
}: {
  values: number[];
  arrowClass: string;
}) {
  if (!values.length) return null;

  return (
    <div className="pl-1">
      <div className="flex min-w-0 flex-wrap items-center gap-2 text-[11px] text-muted">
        <span className="mr-1 font-semibold">Recent runs</span>
        {values.map((value, index) => (
          <React.Fragment key={`${value}-${index}`}>
            {index > 0 && (
              <ArrowRight size={14} className={`shrink-0 ${arrowClass}`} aria-hidden="true" />
            )}
            <span className="inline-flex h-6 min-w-[2.25rem] items-center justify-center rounded-full border border-blue-500/20 bg-blue-500/10 px-2 font-semibold text-text">
              {formatValue(value)}
            </span>
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

export default function BaselineContextPanel({ enrichment }: Props) {
  if (!enrichment) return null;

  const hasTemporalContext =
    enrichment.baseline_context ||
    enrichment.trend_direction ||
    typeof enrichment.run_count === "number" ||
    typeof enrichment.current_value === "number" ||
    (enrichment.recent_values?.length ?? 0) > 0;

  if (!hasTemporalContext) return null;

  const recentValues = enrichment.recent_values ?? [];
  const latestValue = latestSignalValue(enrichment, recentValues);
  const windowDays = enrichment.baseline_window_days ?? DEFAULT_WINDOW_DAYS;
  const change = changePct(latestValue, enrichment.baseline_mean);
  const metrics = {
    latestValue,
    baseline: enrichment.baseline_mean,
    change,
    windowDays,
  };
  const state = classify(enrichment);
  const style = STATE_STYLES[state];
  const { Icon } = style;
  const config = configFor(state, enrichment, metrics);
  const subtitle = subtitleFor(state, enrichment, metrics);

  return (
    <div>
      <div className="mb-2 text-xs font-semibold text-text">Baseline Context</div>

      <div className="space-y-3">
        <div className="rounded-lg border border-blue-500/25 bg-bg/25 p-4">
          <div className="flex min-w-0 items-start gap-3">
            <span
              className={`inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full border ${style.icon}`}
            >
              <Icon size={19} strokeWidth={2.4} aria-hidden="true" />
            </span>

            <div className="min-w-0 flex-1">
              <div className="text-sm font-bold leading-snug text-text">
                {config.title}
              </div>
              <div className="mt-1 text-sm leading-relaxed text-text">
                {subtitle}
              </div>
              <div className="mt-3 flex min-w-0 flex-wrap gap-2">
                {config.badges.map((badge) => (
                  <ChipPill
                    key={badge.label}
                    chip={badge}
                    primaryClass={style.primaryBadge}
                  />
                ))}
              </div>
            </div>
          </div>
        </div>

        <div className="rounded-lg border border-blue-500/25 bg-bg/20 p-4">
          <div className="text-sm font-bold leading-snug text-text">
            {config.whyTitle}
          </div>
          <p className="mt-2 text-xs leading-relaxed text-muted">{config.why}</p>
          <div className="mt-3 flex min-w-0 flex-wrap gap-2">
            {config.detailChips.map((chip) => (
              <ChipPill key={chip.label} chip={chip} />
            ))}
          </div>
        </div>

        <RecentValues values={recentValues} arrowClass={style.arrow} />
      </div>
    </div>
  );
}
