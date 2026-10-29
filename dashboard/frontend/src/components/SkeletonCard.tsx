import './SkeletonCard.css';

interface SkeletonCardProps {
  count?: number;
}

export function SkeletonCard({ count = 1 }: SkeletonCardProps) {
  return (
    <>
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="skeleton-card" aria-label="Loading task">
          <div className="skeleton-line skeleton-line-title"></div>
          <div className="skeleton-line skeleton-line-text"></div>
          <div className="skeleton-line skeleton-line-meta"></div>
        </div>
      ))}
    </>
  );
}
