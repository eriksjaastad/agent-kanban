import type { ProjectCheckout } from '../types';
import './CheckoutBadge.css';

/** "Checked out · Mac Mini": the project is developed only on that machine (#8339). */
export function CheckoutBadge({ projectId, checkout }: { projectId: string; checkout: ProjectCheckout }) {
  return (
    <span
      className="checkout-badge"
      title={`${projectId} is checked out to the ${checkout.machine_label} by ${checkout.holder}; develop it only there`}
    >
      Checked out · {checkout.machine_label}
    </span>
  );
}
