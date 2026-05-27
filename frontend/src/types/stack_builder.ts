/**
 * AgentIQ Guided Discovery Stack Builder — Type Definitions
 * SB-1 Sprint 7
 *
 * All terminology confirmed by architect team May 2026:
 *   Operational Signal Source  (not Corroborating System)
 *   Work Tracking & Operations (not ALM / ITSM)
 *   Data & Engineering Sources (not Technical & Data Layer)
 *   Intake & Requests          (not Intake / Onboarding)
 *   Change & Release           (not Engineering / Change — workflow tag only)
 *
 * ARCHITECTURAL NOTE (Sprint 7 — confirmed by architect review May 2026):
 * This single file is acceptable for Sprint 7 while the surface area is
 * manageable. It must not become a dumping ground as Sprint 7/8 adds
 * industry registry types, quality rows, recommended additions, and
 * pack mapping types.
 *
 * Sprint 8 story: split into three focused files:
 *   - stack_builder_types.ts      — core domain types (this file, pruned)
 *   - stack_builder_defaults.ts   — SYSTEM_DEFAULT_ASSUMPTIONS and seeding logic
 *   - stack_builder_confidence.ts — SetupReadinessState, thresholds, quality rows
 *
 * Do not add new types to this file in Sprint 8 without the split first.
 */

// ── Discovery Focus ───────────────────────────────────────────────────────────

export type FocusId =
  | 'member_customer_service'
  | 'core_operations'
  | 'approvals_compliance'
  | 'cross_system_handoffs'
  | 'back_office_productivity'
  | 'engineering_change'
  | 'enterprise_wide';

export interface FocusCard {
  id: FocusId;
  title: string;
  subtext: string;
  icon: string;               // Tabler icon name e.g. 'ti-users'
  wide?: boolean;             // true = full-width card (Enterprise-Wide)
}

// ── Industry ──────────────────────────────────────────────────────────────────

export type IndustryId =
  | 'financial_services'
  | 'public_sector'
  | 'logistics_supply_chain'
  | 'retail_commerce'
  | 'healthcare'
  | 'energy_utilities'
  | 'manufacturing'
  | 'technology';

export interface Industry {
  id: IndustryId;
  label: string;
}

// ── Template ──────────────────────────────────────────────────────────────────

export type TemplateId =
  | 'commercial_lending'
  | 'public_retirement'
  | 'service_operations'
  | 'revenue_operations';

export interface StackTemplate {
  id: TemplateId;
  label: string;
  suggestedFocus: FocusId;
  preselectedSystems: string[];   // system IDs
}

// ── System ────────────────────────────────────────────────────────────────────

export type SystemGroup =
  | 'primary_platform'
  | 'additional_platform'
  | 'work_tracking'
  | 'comms_knowledge'
  | 'code_engineering'
  | 'data_infrastructure';

export type ConnectionStatus =
  | 'connected'       // green dot
  | 'needs_auth'      // amber dot — credentials needed
  | 'not_configured'; // grey dot

export interface SystemCard {
  id: string;
  name: string;
  category: string;           // display tag e.g. "ERP", "Issues · backlog"
  group: SystemGroup;
  connectionStatus: ConnectionStatus;
  logoInitials: string;       // fallback text e.g. "SAP", "SF"
  logoColor: string;          // Tailwind bg class e.g. "bg-slate-700"
  isSalesforce?: boolean;     // true = expands to cloud product picker
}

export interface SalesforceCloud {
  id: string;
  name: string;
}

// ── Source Weighting ──────────────────────────────────────────────────────────

export type SystemRole =
  | 'system_of_record'
  | 'workflow_system'
  | 'operational_signal_source'
  | 'documentation_system'
  | 'engineering_change_system';

export type SystemPriority = 'primary' | 'secondary' | 'optional';

export type WorkflowFocusTag =
  | 'intake_requests'
  | 'service_casework'
  | 'approvals'
  | 'backlog_work_queues'
  | 'compliance_risk'
  | 'documents_knowledge'
  | 'handoffs_routing'
  | 'communications'
  | 'change_release'
  | 'data_analytics';

export interface SystemWeighting {
  systemId: string;
  role: SystemRole;
  priority: SystemPriority;
  workflowFocus: WorkflowFocusTag[];   // max 3
  confirmed: boolean;
}

// ── Setup Readiness / Discovery Confidence ───────────────────────────────────
//
// ARCHITECTURAL NOTE (Sprint 7 — confirmed by architect review May 2026):
// Internal model name: SetupReadinessState / SetupReadinessScore.
// UI label: "Discovery confidence" — retained as user-facing language only.
//
// ConfidenceState is a configuration-quality measure, not a discovery-result
// measure. It reflects how completely the user has configured the stack builder.
// It does not yet reflect source diversity, auth state, pack coverage,
// or runtime discovery quality.
//
// Sprint 9+ story: evolve ConfidenceState to incorporate runtime signals from
// actual discovery runs. At that point, rename to SetupReadinessState internally.

export type ConfidenceLevel = 'basic' | 'good' | 'strong';

export interface ConfidenceState {
  level: ConfidenceLevel;
  fillPercent: number;          // 0–100  (SetupReadinessScore internall)
  hint: string;                 // actionable hint line shown below bar
  summary?: string;             // Screen 4 "why this setup is strong" sentence
}

// ── Discovery Quality ─────────────────────────────────────────────────────────

export type QualityLevel = 'strong' | 'moderate' | 'limited';

export interface QualityRow {
  label: string;
  level: QualityLevel;
  descriptor: string;
}

// ── Recommended Addition ──────────────────────────────────────────────────────

export interface RecommendedAddition {
  systemId: string;
  systemName: string;
  reason: string;               // industry + lens aware reason string
}

// ── Full Setup State ──────────────────────────────────────────────────────────

export interface SetupState {
  focusId: FocusId | null;
  industryId: IndustryId | null;
  templateId: TemplateId | null;
  templatePreselectedIds: string[];
  selectedSystemIds: string[];
  selectedSalesforceClouds: string[];
  weightings: Record<string, SystemWeighting>;
  currentStep: 1 | 2 | 3 | 4;
}

// ── Progress Bar Step ─────────────────────────────────────────────────────────

export type StepStatus = 'active' | 'completed' | 'needs_attention' | 'pending';

export interface ProgressStep {
  number: 1 | 2 | 3 | 4;
  label: string;
  status: StepStatus;
}
