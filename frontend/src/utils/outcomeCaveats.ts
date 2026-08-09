import type { OutcomeConfounder } from '../types/outcome';

function humanize(value?: string | null): string {
  const cleaned = String(value ?? '')
    .trim()
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ');
  if (!cleaned) return '';
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

export function outcomeCaveatLabel(caveat: OutcomeConfounder): string {
  return caveat.label?.trim() || humanize(caveat.type) || 'Measurement caveat';
}

export function outcomeCaveatExplanation(caveat: OutcomeConfounder): string | null {
  const detail = caveat.detail ?? {};
  const explanation = [detail.implication, detail.description, detail.message]
    .find((value) => typeof value === 'string' && value.trim());
  if (typeof explanation === 'string') return explanation.trim();
  if (typeof detail.reason === 'string' && detail.reason.trim()) {
    return humanize(detail.reason);
  }
  return null;
}

export function outcomeCaveatSeverity(caveat: OutcomeConfounder): string {
  return humanize(caveat.severity) || 'Caveat';
}
