export const SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000;

/** True when `dateStr` parses and is within the last `windowMs` (default 7 days). */
export function isWithinRecentWindow(dateStr: string, nowMs = Date.now(), windowMs = SEVEN_DAYS_MS): boolean {
  const t = new Date(dateStr).getTime();
  if (Number.isNaN(t)) return false;
  const age = nowMs - t;
  return age >= 0 && age <= windowMs;
}

export function timeAgo(dateStr: string, nowMs = Date.now()): string {
  const date = new Date(dateStr);
  const seconds = Math.floor((nowMs - date.getTime()) / 1000);
  if (Number.isNaN(seconds) || seconds < 0) return 'unknown';
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}
