/**
 * Shared acceptance-criteria checklist rule (#7608).
 *
 * Every new Kanban card must state how its own completion will be
 * verified, as a Markdown checklist line. This mirrors
 * ACCEPTANCE_CRITERION_RE in scripts/utils/validation.py exactly: at
 * least one "- [ ] <text>" or "- [x] <text>" line, with real text after
 * the checkbox. A heading alone (e.g. "## Acceptance Criteria") does not
 * count. Keep this regex in sync with the Python one if either changes.
 *
 * This is a client-side convenience so TaskForm can block submit and show
 * an inline error before round-tripping to the server. It is NOT the
 * enforcement boundary -- DatabaseManager.create_card() (backend_manager.py)
 * is, and the server rejects the same way regardless of what the client
 * checks.
 */
const ACCEPTANCE_CRITERION_RE = /^[ \t]*-[ \t]*\[[ xX]\][ \t]+\S/m;

/** True when `notes` contains at least one concrete acceptance-criterion line. */
export function hasAcceptanceCriteria(notes: string | null | undefined): boolean {
  if (!notes || !notes.trim()) {
    return false;
  }
  return ACCEPTANCE_CRITERION_RE.test(notes);
}

/** Prefilled starting point for a new card's acceptance-criteria field. */
export const ACCEPTANCE_CRITERIA_TEMPLATE = '- [ ] ';

/** Client-side copy of the same rule the server enforces; shown inline. */
export const ACCEPTANCE_CRITERIA_HINT =
  "Acceptance criteria required: add at least one '- [ ] <how completion is verified>' " +
  "checklist line. A heading alone (e.g. '## Acceptance Criteria') does not count.";

/**
 * One checklist line parsed from notes (#7821). `lineIndex` is the position
 * in notes.split("\n") -- the same index the server's
 * POST /api/tasks/{id}/checklist toggle uses -- and `text` is the item text
 * with any trailing "\r" (CRLF notes) removed. Mirrors
 * scripts/utils/checklist.py CHECKLIST_LINE_RE.
 */
const CHECKLIST_LINE_RE = /^([ \t]*-[ \t]*\[)([ xX])(\][ \t]+)(\S.*)$/;

export type NotesLine =
  | { kind: 'text'; lineIndex: number; raw: string }
  | { kind: 'checklist'; lineIndex: number; raw: string; checked: boolean; text: string; indent: string };

/** Split notes into display lines, tagging checklist items. */
export function parseNotesLines(notes: string | null | undefined): NotesLine[] {
  if (!notes) {
    return [];
  }
  return notes.split('\n').map((raw, lineIndex): NotesLine => {
    const body = raw.endsWith('\r') ? raw.slice(0, -1) : raw;
    const match = CHECKLIST_LINE_RE.exec(body);
    if (!match) {
      return { kind: 'text', lineIndex, raw };
    }
    return {
      kind: 'checklist',
      lineIndex,
      raw,
      checked: match[2] !== ' ',
      text: match[4],
      // Leading whitespace before "-", so nested items keep their level.
      indent: match[1].slice(0, match[1].indexOf('-')),
    };
  });
}
