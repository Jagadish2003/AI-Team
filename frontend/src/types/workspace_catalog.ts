/**
 * workspace_catalog.ts — ENG-SB-3 Sprint 9
 *
 * TypeScript types mirroring the WorkspaceCatalogResponse Pydantic model
 * from routes_workspace_catalog.py (ENG-IH-1 Sprint 9).
 *
 * Consumed by:
 *   StackBuilderPage — catalog fetch result
 *   YourSystemsPage  — catalog prop (ENG-SB-1)
 *   useSetupState    — catalogSystems input (ENG-SB-2)
 */

export type CatalogSystemStatus = 'connected' | 'needs_auth' | 'not_configured';

export interface CatalogSystemItem {
  system_id: string;
  name:      string;
  status:    CatalogSystemStatus;
  products:  string[];   // Salesforce cloud product IDs only
}

export interface WorkspaceCatalogResponse {
  primary_platforms:   CatalogSystemItem[];
  operational_systems: CatalogSystemItem[];
  comms_knowledge:     CatalogSystemItem[];
  data_engineering:    CatalogSystemItem[];
  // MSP-B13: Cloud Operations (AWS/Azure Events + future multi-account/subscription
  // cloud connectors). Optional so pre-MSP-B13 catalog fixtures/responses still
  // typecheck; the backend always returns it now.
  cloud_operations?:   CatalogSystemItem[];
  missing_categories:  string[];
}

/** All system items from all categories in one flat list. */
export function flattenCatalog(catalog: WorkspaceCatalogResponse): CatalogSystemItem[] {
  return [
    ...catalog.primary_platforms,
    ...catalog.operational_systems,
    ...catalog.comms_knowledge,
    ...catalog.data_engineering,
    ...(catalog.cloud_operations ?? []),
  ];
}

/** System IDs of all connected or needs_auth systems — for seeding selectedSystemIds. */
export function getCatalogSystemIds(catalog: WorkspaceCatalogResponse): string[] {
  return flattenCatalog(catalog)
    .filter(s => s.status === 'connected' || s.status === 'needs_auth')
    .map(s => s.system_id);
}

/** Salesforce products from the catalog — for seeding selectedSalesforceClouds. */
export function getCatalogSalesforceProducts(catalog: WorkspaceCatalogResponse): string[] {
  const sf = catalog.primary_platforms.find(s => s.system_id === 'salesforce');
  return sf?.products ?? [];
}
