const EXPLICIT_TIMEZONE = /(?:z|[+-]\d{2}:?\d{2})$/i;

export function parseBackendDateTime(value: string): Date {
  return new Date(EXPLICIT_TIMEZONE.test(value) ? value : `${value}Z`);
}

export function formatSystemDateTime(value: string | null, fallback = "unknown"): string {
  if (!value) return fallback;
  const date = parseBackendDateTime(value);
  if (Number.isNaN(date.getTime())) return fallback;
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
  }).format(date);
}
