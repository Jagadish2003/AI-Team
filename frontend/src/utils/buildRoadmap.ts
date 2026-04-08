import { OpportunityCandidate, PermissionItem } from '../types/analystReview';
import { PilotRoadmapModel, RoadmapDependency, RoadmapStage, Readiness } from '../types/pilotRoadmap';

function uniqByLabel(items: PermissionItem[]): PermissionItem[] {
  const map = new Map<string, PermissionItem>();
  for (const p of items) {
    const existing = map.get(p.label);
    if (!existing) map.set(p.label, { ...p });
    else {
      map.set(p.label, {
        label: p.label,
        required: existing.required || p.required,
        satisfied: existing.satisfied && p.satisfied
      });
    }
  }
  return Array.from(map.values());
}

export function readinessFromPermission(p: PermissionItem): Readiness {
  if (p.satisfied) return 'READY';
  return p.required ? 'MISSING' : 'PENDING';
}

export function stageReadiness(ps: PermissionItem[]): Readiness {
  let pending = 0;
  for (const p of ps) {
    const r = readinessFromPermission(p);
    if (r === 'MISSING') return 'MISSING';
    if (r === 'PENDING') pending++;
  }
  return pending > 0 ? 'PENDING' : 'READY';
}

export function overallReadiness(ps: PermissionItem[]): 'High' | 'Moderate' | 'Low' {
  let missing = 0, pending = 0;
  for (const p of ps) {
    const r = readinessFromPermission(p);
    if (r === 'MISSING') missing++;
    if (r === 'PENDING') pending++;
  }
  if (missing > 0) return 'Low';
  if (pending > 0) return 'Moderate';
  return 'High';
}

export function buildPilotRoadmap(all: OpportunityCandidate[]): PilotRoadmapModel {
  const approved = all.filter(o => o.decision === 'APPROVED');

  const unreviewed = all
    .filter(o => o.decision === 'UNREVIEWED')
    .slice()
    .sort((a, b) => ((b.impact - b.effort) - (a.impact - a.effort)) || (b.impact - a.impact));

  const unreviewedQW = unreviewed.filter(o => o.tier === 'Quick Win').slice(0, 3);
  const unreviewedStrategic = unreviewed.filter(o => o.tier === 'Strategic').slice(0, 2);
  const unreviewedComplex = unreviewed.filter(o => o.tier === 'Complex').slice(0, 1);

  const selected = [
    ...approved,
    ...unreviewedQW,
    ...unreviewedStrategic,
    ...unreviewedComplex
  ];

  const stage30 = selected.filter(o => o.tier === 'Quick Win').slice(0, 3);
  const stage60 = selected.filter(o => o.tier === 'Strategic').slice(0, 3);
  const stage90 = selected.filter(o => o.tier === 'Complex').slice(0, 2);

  const mkDeps = (id: RoadmapStage['id']): RoadmapDependency[] => {
    const base: RoadmapDependency[] = [
      { id: 'dep_owner', label: 'Business owner + success metrics confirmed', status: 'READY' },
      { id: 'dep_access', label: 'Data access approvals completed', status: 'PENDING' },
      { id: 'dep_security', label: 'Security review (read-only connectors)', status: 'PENDING' },
      { id: 'dep_runbook', label: 'Runbook / SOP evidence set identified', status: 'PENDING' },
    ];
    if (id === 'NEXT_30') return base.slice(0, 3);
    if (id === 'NEXT_60') return base;
    return [
      ...base,
      { id: 'dep_audit', label: 'Audit & compliance sign-off', status: 'PENDING' },
      { id: 'dep_prod', label: 'Production readiness gate', status: 'MISSING' }
    ];
  };

  const mkStage = (id: RoadmapStage['id'], title: string, opps: OpportunityCandidate[], summary: string): RoadmapStage => ({
    id,
    title,
    summary,
    opportunities: opps,
    requiredPermissions: uniqByLabel(opps.flatMap(o => o.permissions ?? [])),
    dependencies: mkDeps(id)
  });

  const stages: RoadmapStage[] = [
    mkStage('NEXT_30', 'Next 30 Days', stage30, 'Prove value fast with low-effort quick wins; establish access + governance.'),
    mkStage('NEXT_60', 'Next 60 Days', stage60, 'Expand evidence coverage and scale adoption across one shared process.'),
    mkStage('NEXT_90', 'Next 90 Days', stage90, 'Production-hardening and long-term automation with audit-ready controls.'),
  ];

  const allPerms = uniqByLabel(stages.flatMap(s => s.requiredPermissions));

  return {
    selectedOpportunityCount: selected.length,
    requiredPermissionsCount: allPerms.filter(p => p.required).length,
    dependencyCount: stages.reduce((n, s) => n + s.dependencies.length, 0),
    overallReadiness: overallReadiness(allPerms),
    stages
  };
}
