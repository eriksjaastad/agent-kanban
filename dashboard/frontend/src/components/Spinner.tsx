import './Spinner.css';

interface SpinnerProps {
  size?: 'small' | 'medium' | 'large';
  className?: string;
}

export function Spinner({ size = 'medium', className = '' }: SpinnerProps) {
  return (
    <div className={`spinner spinner-${size} ${className}`} role="status" aria-label="Loading">
      <div className="spinner-circle"></div>
    </div>
  );
}
