/**
 * AgentIQ — Shared API client for non-run endpoints.
 *
 * Rules:
 * - No hardcoded localhost for non-dev builds.
 * - GET requests should not send Content-Type.
 */

const ENV_BASE_URL = import.meta.env.VITE_API_BASE_URL as string | undefined;

const BASE_URL =
  ENV_BASE_URL ??
  (import.meta.env.DEV
    ? 'http://localhost:8000'
    : (() => {
        throw new Error(
          'VITE_API_BASE_URL is not set. Copy .env.development.example to .env.development (or set env in hosting).'
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
  const token = (import.meta.env.VITE_DEV_JWT as string | undefined) ?? 'dev-token-change-me';
  return { Authorization: `Bearer ${token}` };
}

async function parseBody(res: Response): Promise<unknown> {
  const ct = res.headers.get('content-type') ?? '';
  if (ct.includes('application/json')) return res.json().catch(() => ({}));
  return res.text().catch(() => '');
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
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeader() },
    body: JSON.stringify(payload),
  });
  const body = await parseBody(res);
  if (!res.ok) throw new ApiError(`POST ${path} failed`, res.status, body);
  return body as T;
}
