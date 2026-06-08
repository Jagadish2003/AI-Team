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

const MIN_BASELINE_RUNS = 3;
const DEFAULT_WINDOW_DAYS = 90;

const CHIP_TONES = {
  blue: "border-blue-500/35 bg-blue-500/10 text-blue-300",
  amber: "border-amber-500/40 bg-amber-500/10 text-amber-300",
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
    icon: "border-amber-500/40 bg-amber-500/10 text-amber-300",
    primaryBadge: CHIP_TONES.amber,
    arrow: "text-amber-400",
  },
  falling: {
    Icon: TrendingDown,
    icon: "border-teal-500/40 bg-teal-500/10 text-teal-300",
    primaryBadge: CHIP_TONES.teal,
    arrow: "text-teal-400",
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
  current: number | null,
  change: number | null,
): string {
  if (state !== "insufficient" && enrichment.baseline_context) {
    return enrichment.baseline_context;
  }

  const windowDays = enrichment.baseline_window_days ?? DEFAULT_WINDOW_DAYS;
  const baseline = enrichment.baseline_mean;
  const pct = Math.abs(Math.round(change ?? 0));

  if (state === "insufficient") {
    return "Baseline context will appear after 3 or more completed runs for this same Discovery Pack in your workspace.";
  }
  if (state === "first_deviation") {
    return "First deviation from a previously stable baseline";
  }
  if (state === "anomaly_falling") {
    return `Down ${pct}% from your ${windowDays}-day baseline of ${formatValue(baseline)}`;
  }
  if (state === "anomaly_rising") {
    return `Up ${pct}% from your ${windowDays}-day baseline of ${formatValue(baseline)}`;
  }
  if (state === "rising") {
    return `Trending up - currently ${pct}% above your ${windowDays}-day baseline`;
  }
  if (state === "falling") {
    return `Trending down - currently ${pct}% below your ${windowDays}-day baseline`;
  }
  if (current !== null && baseline !== null && current === baseline) {
    return `Stable - within normal range of your ${windowDays}-day baseline`;
  }
  return `Stable - within normal range of your ${windowDays}-day baseline`;
}

function configFor(
  state: BaselineState,
  enrichment: OppEnrichment,
  current: number | null,
  change: number | null,
): BaselineConfig {
  const currentRuns = enrichment.run_count ?? 0;
  const runBadge = pluralRun(enrichment.run_count);
  const baseline = enrichment.baseline_mean;
  const anomalyScore = enrichment.anomaly_score;

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
          { label: "First deviation", tone: "violet" },
          ...(runBadge ? [{ label: runBadge, tone: "neutral" as const }] : []),
        ],
        detailChips: [
          { label: `Previous baseline: ${formatValue(baseline)}`, tone: "neutral" },
          { label: `Current: ${formatValue(current)}`, tone: "neutral" },
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
            tone: state === "anomaly_falling" ? "teal" : "amber",
          },
          ...(runBadge ? [{ label: runBadge, tone: "neutral" as const }] : []),
        ],
        detailChips: [
          { label: `Current: ${formatValue(current)}`, tone: "neutral" },
          { label: `Baseline avg: ${formatValue(baseline)}`, tone: "neutral" },
          { label: `Anomaly score: ${formatValue(anomalyScore)}`, tone: "neutral" },
        ] as Chip[],
      };
    case "rising":
      return {
        title: "Trending Up",
        whyTitle: "Why Rising?",
        why:
          "This signal increased across recent discovery runs. The calculated trend slope is above the normal stability band, so the system classifies the pattern as rising.",
        badges: [
          { label: "Rising", tone: "amber" },
          ...(runBadge ? [{ label: runBadge, tone: "neutral" as const }] : []),
        ],
        detailChips: [
          { label: `Current: ${formatValue(current)}`, tone: "neutral" },
          { label: `Baseline avg: ${formatValue(baseline)}`, tone: "neutral" },
          { label: `Change: ${formatPercent(change)}`, tone: "neutral" },
        ] as Chip[],
      };
    case "falling":
      return {
        title: "Trending Down",
        whyTitle: "Why Falling?",
        why:
          "This signal decreased across recent discovery runs. The calculated trend slope is below the normal stability band, so the system classifies the pattern as falling.",
        badges: [
          { label: "Falling", tone: "teal" },
          ...(runBadge ? [{ label: runBadge, tone: "neutral" as const }] : []),
        ],
        detailChips: [
          { label: `Current: ${formatValue(current)}`, tone: "neutral" },
          { label: `Baseline avg: ${formatValue(baseline)}`, tone: "neutral" },
          { label: `Change: ${formatPercent(change)}`, tone: "neutral" },
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
          { label: `Current: ${formatValue(current)}`, tone: "neutral" },
          { label: `Baseline avg: ${formatValue(baseline)}`, tone: "neutral" },
          { label: `Change: ${formatPercent(change)}`, tone: "neutral" },
        ] as Chip[],
      };
  }
}

function ChipPill({ chip, primaryClass }: { chip: Chip; primaryClass?: string }) {
  const toneClass = chip.tone ? CHIP_TONES[chip.tone] : primaryClass ?? CHIP_TONES.neutral;
  return (
    <span
      className={`inline-flex h-6 max-w-full items-center rounded-full border px-2.5 text-[11px] font-semibold leading-none ${toneClass}`}
    >
      <span className="truncate">{chip.label}</span>
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
    <div className="flex min-w-0 flex-wrap items-center gap-2 pl-1 text-[11px] text-muted">
      <span className="mr-1 font-semibold">Recent values</span>
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
  const current = enrichment.current_value ?? lastNumber(recentValues);
  const change = changePct(current, enrichment.baseline_mean);
  const state = classify(enrichment);
  const style = STATE_STYLES[state];
  const { Icon } = style;
  const config = configFor(state, enrichment, current, change);
  const subtitle = subtitleFor(state, enrichment, current, change);

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
