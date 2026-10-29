import { describe, expect, it } from 'vitest';
import { SEVEN_DAYS_MS, isWithinRecentWindow, timeAgo } from '../utils/recentActivity';

describe('Recent Activity window (#7180)', () => {
  const now = Date.parse('2026-09-19T12:00:00Z');

  it('includes pushes inside the last 7 days', () => {
    expect(isWithinRecentWindow('2026-09-19T05:10:35Z', now)).toBe(true);
    expect(isWithinRecentWindow('2026-09-13T00:00:00Z', now)).toBe(true);
  });

  it('excludes pushes older than 7 days', () => {
    expect(isWithinRecentWindow('2026-09-08T09:13:58Z', now)).toBe(false);
    expect(isWithinRecentWindow('2026-08-03T05:04:46Z', now)).toBe(false);
  });

  it('excludes invalid timestamps', () => {
    expect(isWithinRecentWindow('not-a-date', now)).toBe(false);
  });

  it('formats ages without collapsing everything to 7d ago', () => {
    expect(timeAgo('2026-09-19T11:00:00Z', now)).toBe('1h ago');
    expect(timeAgo('2026-09-18T12:00:00Z', now)).toBe('1d ago');
    expect(timeAgo('2026-09-12T12:00:00Z', now)).toBe('7d ago');
    expect(SEVEN_DAYS_MS).toBe(7 * 24 * 60 * 60 * 1000);
  });
});
