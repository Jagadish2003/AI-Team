/**
 * MSP-B13 (AT-747, T5) — the SINGLE source of the per-scope run-health vocabulary
 * for the Integration Hub cloud-connector cards.
 *
 * T5-AC1 requires per-scope connector failures to be shown using the SAME
 * run-health terminology the backend reports. This module is that shared source:
 * every status word, its human label, its visual tone, and its icon live here once,
 * and both the presentation card (`MultiScopeConnectorCard`) and the wiring manager
 * (`MultiScopeConnectorManager`) import from it — so the card and any run-health
 * surface can never drift into two different vocabularies.
 *
 * The status set is deliberately aligned 1:1 with the backend connector-health
 * vocabularies:
 *   - AWS   `AWSAccountHealth.status`  → ok | partial | auth_failed | failed
 *   - Azure `subscription_status`      → ok | error(→failed) | auth(→auth_failed)
 *   - scope-health endpoint            → pending (pinned, not yet polled) | the above
 *   - unknown                          → no health reported yet
 *
 * T5-AC2 (healthy vs failed visually distinguishable) is encoded in `tone`
 * (`healthy` | `warn` | `error` | `neutral`), which drives the colour classes and a
 * stable `data-tone` marker the card exposes for tests / styling.
 */
import {
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Clock,
  HelpCircle,
  type LucideIcon,
} from 'lucide-react';
import { ScopeHealthStatus } from '../../types/multiScopeConnector';

/** The visual severity tone for a health status (drives colour + the data-tone). */
export type ScopeHealthTone = 'healthy' | 'warn' | 'error' | 'neutral';

export interface ScopeHealthPresentation {
  /** Human-readable label shown on the badge — the shared run-health term. */
  label: string;
  /** Severity tone (healthy vs failed visual distinction, T5-AC2). */
  tone: ScopeHealthTone;
  /** Tailwind classes for the badge. */
  cls: string;
  /** Icon reinforcing the tone (colour-independent distinction for a11y). */
  Icon: LucideIcon;
}

/** Every recognised per-scope health status (the shared run-health vocabulary). */
export const SCOPE_HEALTH_STATUSES: readonly ScopeHealthStatus[] = [
  'ok',
  'partial',
  'auth_failed',
  'failed',
  'pending',
  'unknown',
] as const;

const _KNOWN = new Set<string>(SCOPE_HEALTH_STATUSES);

/**
 * Coerce a raw backend status string to a known `ScopeHealthStatus`, falling back
 * to `unknown` for anything unrecognised — so a new backend status never renders as
 * a raw, unstyled string.
 */
export function toScopeHealthStatus(status: string | null | undefined): ScopeHealthStatus {
  return status && _KNOWN.has(status) ? (status as ScopeHealthStatus) : 'unknown';
}

/** True when a status represents a failed/attention scope (not ok, not pending). */
export function isFailedStatus(status: ScopeHealthStatus): boolean {
  return SCOPE_HEALTH_PRESENTATION[status]?.tone === 'error';
}

/** The one presentation map both the card and run-health surfaces render from. */
export const SCOPE_HEALTH_PRESENTATION: Record<ScopeHealthStatus, ScopeHealthPresentation> = {
  ok: {
    label: 'Healthy',
    tone: 'healthy',
    cls: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
    Icon: CheckCircle2,
  },
  partial: {
    label: 'Partial',
    tone: 'warn',
    cls: 'border-amber-500/30 bg-amber-500/10 text-amber-200',
    Icon: AlertTriangle,
  },
  auth_failed: {
    label: 'Auth failed',
    tone: 'error',
    cls: 'border-red-500/30 bg-red-500/10 text-red-300',
    Icon: XCircle,
  },
  failed: {
    label: 'Failed',
    tone: 'error',
    cls: 'border-red-500/30 bg-red-500/10 text-red-300',
    Icon: XCircle,
  },
  pending: {
    label: 'Pending',
    tone: 'neutral',
    cls: 'border-sky-500/30 bg-sky-500/10 text-sky-300',
    Icon: Clock,
  },
  unknown: {
    label: 'Unknown',
    tone: 'neutral',
    cls: 'border-border bg-slate-500/10 text-muted',
    Icon: HelpCircle,
  },
};

/** Presentation for a status, always defined (falls back to `unknown`). */
export function scopeHealthPresentation(status: ScopeHealthStatus): ScopeHealthPresentation {
  return SCOPE_HEALTH_PRESENTATION[status] ?? SCOPE_HEALTH_PRESENTATION.unknown;
}
