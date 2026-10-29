#!/usr/bin/env bash
#
# portfolio-architecture-review.sh — Cross-project architecture audit
#
# Evaluates whether each project's architecture, patterns, and technical
# choices are deliberately correct — not just functional. Agent-driven,
# follows the card-factory hybrid model.
#
# Usage:
#   portfolio-architecture-review.sh                     # Audit all active projects
#   portfolio-architecture-review.sh --project <name>    # Audit a single project
#   portfolio-architecture-review.sh --dry-run           # Preview prompt without running
#
# Manual trigger only — too expensive for scheduled runs.
# See CODE_QUALITY_STRATEGY.md and card #5525.
#

set -euo pipefail

PROJECT=""
DRY_RUN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project|-p)
      PROJECT="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    --all)
      PROJECT=""
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Usage: portfolio-architecture-review.sh [--project <name>] [--dry-run]" >&2
      exit 1
      ;;
  esac
done

SCOPE="all active projects"
if [[ -n "$PROJECT" ]]; then
  SCOPE="$PROJECT"
fi

REPORT_PATH="data/architecture_review_$(date +%Y-%m).md"

PROMPT="Run a portfolio architecture review on $SCOPE.

For each project:
1. Read DIRECTION.md, DECISIONS.md, CLAUDE.md, and the top-level source structure.
2. Evaluate against this rubric:
   - **Tech stack fit:** Is the language/framework appropriate for the project's goals?
   - **Pattern consistency:** Are similar problems solved the same way within the project?
   - **Separation of concerns:** Are module boundaries clean?
   - **Decision documentation:** Do critical choices have DECISIONS.md entries?
   - **Dependency hygiene:** Are dependencies necessary and maintained?
3. For each project, output a verdict: Approved / Concerns / Needs Review.
4. Include specific findings with file paths.

Write the full report to $REPORT_PATH. Format as markdown with a summary table and per-project sections.

Do not ask questions or wait for confirmation. Run the review now."

if [[ "$DRY_RUN" == "true" ]]; then
  echo "=== DRY RUN — would send this prompt ==="
  echo ""
  echo "$PROMPT"
  exit 0
fi

exec claude --agent portfolio-architect -p "$PROMPT"
