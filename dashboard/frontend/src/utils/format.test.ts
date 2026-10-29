import { describe, expect, it } from 'vitest';

import { formatCost, formatTokens } from './format';

describe('formatCost', () => {
  it('shows zero as zero, sub-cent amounts as <$0.01, and large totals without cents', () => {
    expect(formatCost(0)).toBe('$0.00');
    expect(formatCost(0.004)).toBe('<$0.01');
    expect(formatCost(3.456)).toBe('$3.46');
    expect(formatCost(1234.5)).toBe(`$${(1234.5).toLocaleString(undefined, { maximumFractionDigits: 0 })}`);
  });
});

describe('formatTokens', () => {
  it('scales to K, M and B', () => {
    expect(formatTokens(999)).toBe('999');
    expect(formatTokens(1_500)).toBe('2K');
    expect(formatTokens(2_500_000)).toBe('2.5M');
    expect(formatTokens(3_000_000_000)).toBe('3.0B');
  });
});
