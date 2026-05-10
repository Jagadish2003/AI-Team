import type { EvidenceReview } from '../types/partialResults';

// T41-3 scope: these patterns are intentionally tied to the Sprint 4
// Service Cloud evidence titles. Future domain packs should extend this map.
const TITLE_PATTERNS: Array<{ keywords: string[]; sentence: string }> = [
  {
    keywords: ['owner', 'reassignment', 'handoff', 'routing', 'owner change'],
    sentence:
      'This metric indicates case routing friction: agents are receiving the wrong cases and passing them on repeatedly.',
  },
  {
    keywords: ['discount approval', 'discount'],
    sentence:
      'This metric indicates an approval queue bottleneck: discount requests are waiting beyond the expected threshold with limited approver capacity.',
  },
  {
    keywords: ['refund approval', 'refund'],
    sentence:
      'This metric indicates an approval queue bottleneck: refund requests are waiting beyond the expected review window with limited approver coverage.',
  },
  {
    keywords: ['approval', 'bottleneck', 'pending', 'sla'],
    sentence:
      'This metric indicates an approval bottleneck: items are waiting significantly longer than expected with limited approver capacity.',
  },
  {
    keywords: ['knowledge', 'kb article', 'knowledge article', 'knowledge gap', 'kb reuse'],
    sentence:
      'This metric indicates a knowledge gap: cases are being resolved without a linked policy or knowledge article, creating inconsistency risk.',
  },
  {
    keywords: ['autolaunched', 'flow', 'autolaunchedflow', 'automation', 'low-complexity'],
    sentence:
      'This metric indicates repetitive automation: low-complexity flows are processing the same high-volume object and may be consolidated into one agent.',
  },
  {
    keywords: ['cross-system', 'echo', 'duplication', 'servicenow', 'incident reference', 'ticket duplication'],
    sentence:
      'This log indicates cross-system ticket duplication: the same issue is being tracked in multiple systems, creating manual coordination work.',
  },
  {
    keywords: ['permission', 'approver capacity', 'overloaded', 'queue overload'],
    sentence:
      'This metric indicates an approval queue overload: too few approvers are handling too many pending items.',
  },
  {
    keywords: ['named credential', 'integration', 'concentration', 'external system'],
    sentence:
      'This metric indicates integration concentration risk: multiple automations depend on a small set of external integrations.',
  },
];

export const SOURCE_BADGE_CLASS: Record<string, string> = {
  Salesforce: 'border-sky-500/40 bg-sky-500/10 text-sky-300',
  ServiceNow: 'border-violet-500/40 bg-violet-500/10 text-violet-300',
  Jira: 'border-amber-500/40 bg-amber-500/10 text-amber-300',
  Confluence: 'border-blue-500/40 bg-blue-500/10 text-blue-300',
  Slack: 'border-green-500/40 bg-green-500/10 text-green-300',
  Databricks: 'border-orange-500/40 bg-orange-500/10 text-orange-300',
};

export function sourceBadgeClass(source: string): string {
  return SOURCE_BADGE_CLASS[source] ?? 'border-border bg-bg/30 text-muted';
}

export function deriveInterpretation(ev: EvidenceReview): string {
  const combined = `${ev.title} ${ev.snippet ?? ''}`.toLowerCase();

  for (const { keywords, sentence } of TITLE_PATTERNS) {
    if (keywords.some((keyword) => combined.includes(keyword))) {
      return sentence;
    }
  }

  return `This ${ev.evidenceType?.toLowerCase() ?? 'finding'} from ${ev.source} was identified as a relevant signal during the discovery run.`;
}

export function countOpportunitiesReferencing(
  evidenceId: string,
  opportunities: Array<{ evidenceIds?: string[] }>,
): number {
  return opportunities.filter((o) => o.evidenceIds?.includes(evidenceId)).length;
}
