import './RankedListControls.css';

// The API returns open listings already ranked by fit (#101679546294759424):
// `fresh` (posted within 30 days) and `stale`. A page shows the top
// TOP_LISTINGS fresh ones and offers controls to widen; nothing is deleted.
// "Show all" applies to fresh listings; older ones, when shown, render in
// their own section outside the top-N limit.
export const TOP_LISTINGS = 25;

interface Props {
  shownCount: number;
  totalCount: number;
  staleCount: number;
  showStale: boolean;
  onToggleStale: () => void;
  onShowAll: () => void;
}

export function RankedListControls({
  shownCount, totalCount, staleCount, showStale, onToggleStale, onShowAll,
}: Props) {
  if (staleCount === 0 && shownCount >= totalCount) {
    return null;
  }
  return (
    <div className="ranked-list-controls">
      {shownCount < totalCount && (
        <button type="button" className="ranked-list-btn" onClick={onShowAll}>
          Show all {totalCount}
        </button>
      )}
      {staleCount > 0 && (
        <>
          {!showStale && (
            <span className="ranked-list-note">
              {staleCount} older {staleCount === 1 ? 'listing' : 'listings'} hidden
            </span>
          )}
          <button type="button" className="ranked-list-btn" onClick={onToggleStale}>
            {showStale ? 'Hide older listings' : 'Show older listings'}
          </button>
        </>
      )}
    </div>
  );
}
