import React from 'react';
import { CheckCircle2, Loader2 } from 'lucide-react';
import type { DiscoveryRun } from '../../types/discoveryRun';

function humanise(value: string): string {
  if (value === 'ncino') return 'nCino lending';
  return value
    .split(/[_-]+/)
    .filter(Boolean)
    .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(' ');
}

export default function TemplateRunNotice({
  run,
  computing,
}: {
  run: DiscoveryRun;
  computing: boolean;
}) {
  if (!run.templateId || !run.templateProvenance?.applied) return null;

  const provenance = run.templateProvenance;
  const systemCount = run.selectedSystemIds?.length ?? 0;
  const editedFields = provenance.edited_fields ?? [];
  const templateLabel = humanise(run.templateId);
  const runSummary = `${computing ? 'Discovery is running with' : 'This run used'} ${systemCount} configured system${systemCount === 1 ? '' : 's'}${run.packId ? ` and the ${humanise(run.packId)} pack` : ''}${run.focusId ? `, focused on ${humanise(run.focusId)}` : ''}.`;

  return (
    <section
      aria-label="Template run guidance"
      className="mb-5 rounded-xl border border-emerald-400/30 bg-emerald-500/10 px-4 py-3"
    >
      <div className="flex items-start gap-3">
        {computing ? (
          <Loader2 size={18} className="mt-0.5 animate-spin flex-shrink-0 text-emerald-300" aria-hidden="true" />
        ) : (
          <CheckCircle2 size={18} className="mt-0.5 flex-shrink-0 text-emerald-300" aria-hidden="true" />
        )}
        <div>
          <h2 className="text-sm font-semibold text-text">
            {computing ? `Using the ${templateLabel} template` : `${templateLabel} template run`}
          </h2>
          <p className="mt-1 text-sm leading-relaxed text-muted">
            {runSummary}
          </p>
          <p className="mt-1 text-xs leading-relaxed text-muted">
            {provenance.untouched
              ? 'The registered template defaults were used without changes.'
              : editedFields.length > 0
                ? `Template changes preserved on this run: ${editedFields.map(humanise).join(', ')}.`
                : 'The run record preserves the template configuration used at launch.'}
          </p>
        </div>
      </div>
    </section>
  );
}
