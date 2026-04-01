/**
 * AgentIQ — Task 5 (G-static) + Task 7 (RunContext + runId Persistence)
 * Shared API client for non-run endpoints (S1/S2/S5) and run-scoped endpoints.
 *
 * Rules:
 * - No hardcoded localhost for non-dev builds.
 * - GET requests should not send Content-Type.
 * - Support run-scoped API calls with runId parameter.
 */

const ENV_BASE_URL = import.meta.env.VITE_API_BASE_URL as string | undefined;

const BASE_URL =
  ENV_BASE_URL ??
  (import.meta.env.DEV
    ? "http://localhost:8000"
    : (() => {
        throw new Error(
          "VITE_API_BASE_URL is not set. Copy .env.development.example to .env.development (or set env in hosting)."
        );
      })());

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

function authHeader(): Record<string, string> {
  const token = (import.meta.env.VITE_DEV_JWT as string | undefined) ?? "dev-token-change-me";
  return { Authorization: `Bearer ${token}` };
}

async function parseBody(res: Response): Promise<unknown> {
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) return res.json().catch(() => ({}));
  return res.text().catch(() => "");
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { ...authHeader() },
  });
  const body = await parseBody(res);
  if (!res.ok) throw new ApiError(`GET ${path} failed`, res.status, body);
  return body as T;
}

export async function apiPost<T>(path: string, payload: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeader() },
    body: JSON.stringify(payload),
  });
  const body = await parseBody(res);
  if (!res.ok) throw new ApiError(`POST ${path} failed`, res.status, body);
  return body as T;
}

/* ========== TASK 7 - NEW CODE START ========== */

/**
 * Task 7: Start a new discovery run
 * Creates a new run via POST /api/runs/start and returns run object with id
 * This runId should be stored in RunContext for subsequent run-scoped requests
 * 
 * @template T - Response type (typically { id: string, createdAt: string, status: string, ... })
 * @param payload - Optional run configuration object (name, parameters, etc.)
 * @returns Promise resolving to new run object containing runId
 * @throws ApiError if run creation fails (network, server error, etc.)
 * 
 * @example
 * const newRun = await apiStartRun({ name: "Discovery Run 1" });
 * console.log(newRun.id); // "run-uuid-12345"
 * // Store this ID in RunContext via setRunId(newRun.id)
 */
export async function apiStartRun<T>(payload?: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}/api/runs/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeader() },
    body: JSON.stringify(payload ?? {}),
  });
  const body = await parseBody(res);
  if (!res.ok) throw new ApiError("POST /api/runs/start failed", res.status, body);
  return body as T;
}

/**
 * Task 7: Fetch run details by runId
 * Retrieves run metadata and current status from /api/runs/{runId}
 * Called when RunContext runId changes or on page refresh to restore run state
 * 
 * @template T - Response type (typically { id, name, status, createdAt, updatedAt, ... })
 * @param runId - Unique run identifier from RunContext (must not be null/empty)
 * @returns Promise resolving to run details object
 * @throws ApiError if run not found (404) or other fetch errors
 * @throws Error if runId is empty/null
 * 
 * @example
 * const run = await apiGetRun("run-uuid-12345");
 * console.log(run.status); // "running" | "completed" | "failed" | "paused"
 */
export async function apiGetRun<T>(runId: string): Promise<T> {
  if (!runId) throw new Error("runId is required for apiGetRun");
  const res = await fetch(`${BASE_URL}/api/runs/${runId}`, {
    headers: { ...authHeader() },
  });
  const body = await parseBody(res);
  if (!res.ok) throw new ApiError(`GET /api/runs/${runId} failed`, res.status, body);
  return body as T;
}

/**
 * Task 7: Fetch all events for a specific run
 * Retrieves complete event history from /api/runs/{runId}/events
 * Called to populate DiscoveryRunPage events list when runId is available
 * 
 * @template T - Response type (typically RunEvent[] or { events: RunEvent[], total: number })
 * @param runId - Unique run identifier from RunContext (must not be null/empty)
 * @returns Promise resolving to array of run events with timestamps and types
 * @throws ApiError if fetch fails
 * @throws Error if runId is empty/null
 * 
 * @example
 * const events = await apiGetRunEvents("run-uuid-12345");
 * console.log(events.length); // number of events
 * events.forEach(event => console.log(event.eventType, event.timestamp));
 */
export async function apiGetRunEvents<T>(runId: string): Promise<T> {
  if (!runId) throw new Error("runId is required for apiGetRunEvents");
  const res = await fetch(`${BASE_URL}/api/runs/${runId}/events`, {
    headers: { ...authHeader() },
  });
  const body = await parseBody(res);
  if (!res.ok) throw new ApiError(`GET /api/runs/${runId}/events failed`, res.status, body);
  return body as T;
}

/**
 * Task 7: Fetch paginated events for a specific run (optional helper for large event lists)
 * Retrieves subset of events with limit/offset query parameters
 * Useful when runs have many events to avoid loading all at once
 * 
 * @template T - Response type (typically { events: RunEvent[], total: number, hasMore: boolean })
 * @param runId - Unique run identifier from RunContext (must not be null/empty)
 * @param limit - Number of events to fetch per page (default: 50)
 * @param offset - Number of events to skip (pagination offset, default: 0)
 * @returns Promise resolving to paginated events response object
 * @throws ApiError if fetch fails
 * @throws Error if runId is empty/null
 * 
 * @example
 * const page1 = await apiGetRunEventsPaginated("run-uuid-12345", 20, 0);
 * console.log(page1.events.length); // up to 20 events
 * const page2 = await apiGetRunEventsPaginated("run-uuid-12345", 20, 20);
 */
export async function apiGetRunEventsPaginated<T>(
  runId: string,
  limit: number = 50,
  offset: number = 0
): Promise<T> {
  if (!runId) throw new Error("runId is required for apiGetRunEventsPaginated");
  const queryParams = new URLSearchParams({
    limit: limit.toString(),
    offset: offset.toString(),
  });
  const res = await fetch(`${BASE_URL}/api/runs/${runId}/events?${queryParams}`, {
    headers: { ...authHeader() },
  });
  const body = await parseBody(res);
  if (!res.ok)
    throw new ApiError(`GET /api/runs/${runId}/events failed`, res.status, body);
  return body as T;
}

/**
 * Task 7: Generic run-scoped GET request helper
 * Flexible method for any GET request to run-specific endpoints
 * Prevents code duplication for future run-scoped fetch needs
 * 
 * @template T - Response type (flexible based on endpoint)
 * @param runId - Unique run identifier from RunContext (must not be null/empty)
 * @param path - API subpath relative to /api/runs/{runId} (e.g., "/status", "/results", "/config")
 * @returns Promise resolving to typed response data
 * @throws ApiError if request fails
 * @throws Error if runId is empty/null
 * 
 * @example
 * const status = await apiGetRunScoped("run-uuid-12345", "/status");
 * const results = await apiGetRunScoped("run-uuid-12345", "/results");
 */
export async function apiGetRunScoped<T>(runId: string, path: string): Promise<T> {
  if (!runId) throw new Error("runId is required for apiGetRunScoped");
  const res = await fetch(`${BASE_URL}/api/runs/${runId}${path}`, {
    headers: { ...authHeader() },
  });
  const body = await parseBody(res);
  if (!res.ok)
    throw new ApiError(`GET /api/runs/${runId}${path} failed`, res.status, body);
  return body as T;
}

/**
 * Task 7: Generic run-scoped POST request helper
 * Flexible method for any POST request to run-specific endpoints
 * Prevents code duplication for actions like pause, resume, cancel, etc.
 * 
 * @template T - Response type (flexible based on endpoint)
 * @param runId - Unique run identifier from RunContext (must not be null/empty)
 * @param path - API subpath relative to /api/runs/{runId} (e.g., "/pause", "/resume", "/cancel")
 * @param payload - Request body object (optional, defaults to empty object)
 * @returns Promise resolving to typed response data
 * @throws ApiError if request fails
 * @throws Error if runId is empty/null
 * 
 * @example
 * const result = await apiPostRunScoped("run-uuid-12345", "/pause", { reason: "user-requested" });
 * const resumed = await apiPostRunScoped("run-uuid-12345", "/resume");
 */
export async function apiPostRunScoped<T>(
  runId: string,
  path: string,
  payload?: unknown
): Promise<T> {
  if (!runId) throw new Error("runId is required for apiPostRunScoped");
  const res = await fetch(`${BASE_URL}/api/runs/${runId}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeader() },
    body: JSON.stringify(payload ?? {}),
  });
  const body = await parseBody(res);
  if (!res.ok)
    throw new ApiError(`POST /api/runs/${runId}${path} failed`, res.status, body);
  return body as T;
}

/* ========== TASK 7 - NEW CODE END ========== */