import { describe, expect, it } from 'vitest';

import { ACCEPTANCE_CRITERIA_TEMPLATE, hasAcceptanceCriteria, parseNotesLines } from './checklist';

describe('hasAcceptanceCriteria', () => {
  it('accepts an unchecked item', () => {
    expect(hasAcceptanceCriteria('- [ ] pytest passes')).toBe(true);
  });

  it('accepts a checked item', () => {
    expect(hasAcceptanceCriteria('- [x] already verified manually')).toBe(true);
  });

  it('accepts an uppercase checked item', () => {
    expect(hasAcceptanceCriteria('- [X] already verified manually')).toBe(true);
  });

  it('accepts an item among other notes', () => {
    const notes = 'Some context first.\n\n- [ ] pytest passes\n\nMore context after.';
    expect(hasAcceptanceCriteria(notes)).toBe(true);
  });

  it('rejects null', () => {
    expect(hasAcceptanceCriteria(null)).toBe(false);
  });

  it('rejects undefined', () => {
    expect(hasAcceptanceCriteria(undefined)).toBe(false);
  });

  it('rejects an empty string', () => {
    expect(hasAcceptanceCriteria('')).toBe(false);
  });

  it('rejects whitespace only', () => {
    expect(hasAcceptanceCriteria('   \n  \n')).toBe(false);
  });

  it('rejects a heading alone', () => {
    expect(hasAcceptanceCriteria('## Acceptance Criteria')).toBe(false);
  });

  it('rejects a checkbox with no text', () => {
    expect(hasAcceptanceCriteria('- [ ] ')).toBe(false);
  });

  it('rejects the unfilled template by itself', () => {
    expect(hasAcceptanceCriteria(ACCEPTANCE_CRITERIA_TEMPLATE)).toBe(false);
  });

  it('rejects a plain bullet without a checkbox', () => {
    expect(hasAcceptanceCriteria('- Just a bullet, not a checkbox')).toBe(false);
  });
});

describe('parseNotesLines (#7821)', () => {
  it('returns nothing for empty notes', () => {
    expect(parseNotesLines(null)).toEqual([]);
    expect(parseNotesLines('')).toEqual([]);
  });

  it('indexes lines by position in split("\\n") and tags checklist items', () => {
    const lines = parseNotesLines('intro\n- [ ] first\n  - [X] second\n## head\n-[ ]nospace');
    expect(lines.map((l) => [l.kind, l.lineIndex])).toEqual([
      ['text', 0], ['checklist', 1], ['checklist', 2], ['text', 3], ['text', 4],
    ]);
    expect(lines[1]).toMatchObject({ checked: false, text: 'first', indent: '' });
    expect(lines[2]).toMatchObject({ checked: true, text: 'second', indent: '  ' });
  });

  it('drops a trailing CR from the item text but keeps raw intact', () => {
    const [line] = parseNotesLines('- [ ] one\r\n');
    expect(line).toMatchObject({ kind: 'checklist', text: 'one', raw: '- [ ] one\r' });
  });
});
