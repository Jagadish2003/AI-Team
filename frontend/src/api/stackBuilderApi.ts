/**
 * stackBuilderApi — R18-C1 T3 (Addendum A)
 *
 * Typed wrappers around the Stack Builder *registry* endpoints. These make the
 * backend registry / template model the single source of truth for the Stack
 * Builder selection UI: the frontend asks the backend "what industries and
 * templates are available?" and renders the answer, instead of owning hardcoded
 * INDUSTRIES / TEMPLATES arrays (AC7/AC8/AC10).
 *
 * These wrappers deliberately take an explicit (apiBase, token) — mirroring the
 * raw-fetch pattern already used by StackBuilderPage for the workspace catalog,
 * setup-state, and launch calls — rather than the module-level auth in
 * lib/apiClient. That keeps the page's test-injected `apiBase` / `token` props
 * (StackBuilderPage Props) working for these calls too, so the whole Stack
 * Builder surface signs requests the same way.
 *
 * Endpoints (backend/app/routes_stack_builder.py, viewer+):
 *   GET /api/stack-builder/industries
 *   GET /api/stack-builder/industries/{id}/system-defaults
 *   GET /api/stack-builder/templates
 */

import type {
  IndustryListItem,
  SystemDefaultItem,
  TemplateListItem,
} from '../types/stack_builder';

const ORG_ID_HEADER = (import.meta.env.VITE_ORG_ID as string | undefined)?.trim();

function authHeaders(token: string): HeadersInit {
  return {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${token}`,
    ...(ORG_ID_HEADER ? { 'X-Org-Id': ORG_ID_HEADER } : {}),
  };
}

async function getJson<T>(url: string, token: string): Promise<T> {
  const res = await fetch(url, {
    credentials: 'omit',
    headers: authHeaders(token),
  });
  if (!res.ok) {
    throw new Error(`Request failed (${res.status}) for ${url}`);
  }
  return (await res.json()) as T;
}

/**
 * List every industry in the backend registry. Renders the Stack Builder
 * industry picker with no frontend hardcoding — a registry add/relabel shows up
 * with no code change (AC7).
 */
export function fetchIndustries(
  apiBase: string,
  token: string,
): Promise<IndustryListItem[]> {
  return getJson<IndustryListItem[]>(
    `${apiBase}/api/stack-builder/industries`,
    token,
  );
}

/**
 * List every template in the backend template model. Renders the template
 * picker from configuration only (AC8).
 */
export function fetchTemplates(
  apiBase: string,
  token: string,
): Promise<TemplateListItem[]> {
  return getJson<TemplateListItem[]>(
    `${apiBase}/api/stack-builder/templates`,
    token,
  );
}

/**
 * Industry-calibrated system defaults (role / priority / workflow focus) for a
 * chosen industry, resolved through the registry API path (AC9). The caller
 * applies these to the selected systems as *editable* starting defaults.
 */
export function fetchIndustrySystemDefaults(
  apiBase: string,
  token: string,
  industryId: string,
): Promise<SystemDefaultItem[]> {
  return getJson<SystemDefaultItem[]>(
    `${apiBase}/api/stack-builder/industries/${encodeURIComponent(
      industryId,
    )}/system-defaults`,
    token,
  );
}
