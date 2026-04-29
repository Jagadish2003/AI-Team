export const RUN_ID_PATTERN = /^(?:run_[A-Za-z0-9_]+|RUN_\d{3,})$/;

export function cleanRunId(value: string | null | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
}

export function isCanonicalRunId(value: string | null | undefined): value is string {
  return RUN_ID_PATTERN.test(value ?? "");
}
