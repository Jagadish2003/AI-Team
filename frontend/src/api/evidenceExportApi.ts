/**
 * 2.0-B1 T4 — signed evidence export (AC4/AC6), client side.
 *
 * Item 4 names three audiences — "auditors, regulators, and board packs" — and
 * one file cannot serve them all, so there are **two downloads**:
 *
 *   - `downloadEvidenceReport*`  → a readable PDF, rendered client-side from the
 *     signed envelope. What a person reads.
 *   - `downloadEvidenceBundle*`  → the canonical signed bytes as `.json`. What an
 *     auditor verifies.
 *
 * They are deliberately separate files rather than one archive: an archive makes
 * every reader unpack something before they can read anything, and the first
 * person to open one went straight to the JSON — which is exactly the outcome the
 * readable artifact exists to prevent.
 *
 * **The bundle download must never be re-serialised.** `?download=true` returns
 * the canonical bytes the HMAC covers; a client that parsed and re-emitted the
 * JSON (different key order, different separators) would produce a file that no
 * longer verifies — indistinguishable, to an auditor, from one that was tampered
 * with. So the bundle path goes through `apiGetBlob` and saves the response body
 * untouched, while the PDF path fetches the parsed envelope instead, because it
 * only needs the values.
 *
 * Both downloads go through fetch rather than an `<a download href>`, which
 * cannot carry the Authorization header.
 *
 * Endpoints (both analyst+, org-scoped from the tenancy middleware):
 *   GET /api/runs/{runId}/opportunities/{oppId}/evidence-export  → one finding
 *   GET /api/runs/{runId}/evidence-export                        → the whole run
 */
import { apiGet, apiGetBlob } from "../lib/apiClient";
import { triggerBrowserDownload } from "../services/cloudConnectorApi";
import { downloadEvidenceReportPdf } from "../utils/evidenceReportPdf";

/** The signature envelope the API returns when `download` is not set. */
export interface EvidenceExportEnvelope {
  bundle: Record<string, unknown>;
  signature: string;
  algorithm: string;
}

function findingPath(runId: string, oppId: string, download: boolean): string {
  const query = download ? "?download=true" : "";
  return `/api/runs/${encodeURIComponent(runId)}/opportunities/${encodeURIComponent(
    oppId,
  )}/evidence-export${query}`;
}

function reportPath(runId: string, download: boolean): string {
  const query = download ? "?download=true" : "";
  return `/api/runs/${encodeURIComponent(runId)}/evidence-export${query}`;
}

/** Fetch one finding's signed envelope as JSON, without downloading a file. */
export async function fetchFindingEvidenceExport(
  runId: string,
  oppId: string,
): Promise<EvidenceExportEnvelope> {
  return apiGet<EvidenceExportEnvelope>(findingPath(runId, oppId, false));
}

/** Fetch the whole run's signed envelope as JSON, without downloading a file. */
export async function fetchReportEvidenceExport(
  runId: string,
): Promise<EvidenceExportEnvelope> {
  return apiGet<EvidenceExportEnvelope>(reportPath(runId, false));
}

// ── the readable download ───────────────────────────────────────────────────

/** Download one finding's evidence as a readable PDF. */
export async function downloadEvidenceReportForFinding(
  runId: string,
  oppId: string,
): Promise<void> {
  await downloadEvidenceReportPdf(await fetchFindingEvidenceExport(runId, oppId));
}

/** Download a whole run's evidence as a readable PDF. */
export async function downloadEvidenceReportForRun(runId: string): Promise<void> {
  await downloadEvidenceReportPdf(await fetchReportEvidenceExport(runId));
}

// ── the verifiable download ─────────────────────────────────────────────────

/**
 * Download one finding's signed bundle as `.json` — the canonical bytes,
 * saved exactly as served. The server names the file.
 */
export async function downloadFindingEvidenceBundle(
  runId: string,
  oppId: string,
): Promise<void> {
  const { blob, filename } = await apiGetBlob(findingPath(runId, oppId, true));
  triggerBrowserDownload(
    blob,
    filename ?? `agentiq-evidence-finding-${runId}-${oppId}.json`,
  );
}

/** Download a whole run's signed bundle as `.json`. */
export async function downloadReportEvidenceBundle(runId: string): Promise<void> {
  const { blob, filename } = await apiGetBlob(reportPath(runId, true));
  triggerBrowserDownload(blob, filename ?? `agentiq-evidence-report-${runId}.json`);
}
