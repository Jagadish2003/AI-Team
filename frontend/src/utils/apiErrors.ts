type ApiErrorLike = {
  status?: number;
  body?: unknown;
  message?: string;
};

function detailText(body: unknown): string {
  if (!body) return "";
  if (typeof body === "string") return body;
  if (typeof body === "object" && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    return typeof detail === "string" ? detail : JSON.stringify(detail ?? "");
  }
  return JSON.stringify(body);
}

export function isRunNotFoundError(error: unknown): boolean {
  const e = error as ApiErrorLike;
  if (e?.status !== 404) return false;
  const detail = detailText(e.body);
  return /run not found/i.test(detail) || /Run '.+' not found/i.test(detail);
}

export function runScopedErrorMessage(error: unknown, fallback: string): string {
  const e = error as ApiErrorLike;
  const detail = detailText(e?.body);

  if (isRunNotFoundError(error)) {
    return "That run ID does not exist anymore. Start or select a valid discovery run.";
  }

  if (e?.status === 404) {
    return "This run's results are still being prepared. Wait for Discovery Run to finish, then retry.";
  }

  return detail || e?.message || fallback;
}

