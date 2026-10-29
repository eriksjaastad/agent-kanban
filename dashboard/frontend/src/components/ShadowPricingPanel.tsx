import { useRequest } from '../hooks/useRequest';
import { formatCost, formatTokens } from '../utils/format';
import './ShadowPricingPanel.css';

interface ModelEntry {
  model_id: string;
  display_name: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_create_tokens: number;
  shadow_cost: number;
}

interface ShadowPricingData {
  total_shadow_cost: number;
  subscription_cost_monthly: number;
  value_multiplier: number;
  total_tokens: number;
  per_model: ModelEntry[];
  first_session_date: string;
  last_computed_date: string;
}

export function ShadowPricingPanel() {
  const { data, error, loading } = useRequest<ShadowPricingData>(async signal => {
    const r = await fetch('/api/shadow-pricing', { signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    return d;
  }, []);

  if (loading) {
    return (
      <div className="dashboard-section shadow-panel">
        <h2>Shadow Pricing</h2>
        <div className="shadow-loading">Loading token usage...</div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="dashboard-section shadow-panel">
        <h2>Shadow Pricing</h2>
        <div className="shadow-unavailable">Shadow pricing unavailable</div>
      </div>
    );
  }

  return (
    <div className="dashboard-section shadow-panel">
      <h2>Shadow Pricing</h2>

      {/* Summary cards */}
      <div className="shadow-summary">
        <div className="shadow-stat">
          <span className="shadow-stat-value">{formatCost(data.total_shadow_cost)}</span>
          <span className="shadow-stat-label">API Cost</span>
        </div>
        <div className="shadow-stat">
          <span className="shadow-stat-value">{formatCost(data.subscription_cost_monthly)}</span>
          <span className="shadow-stat-label">Subscription</span>
        </div>
        <div className="shadow-stat">
          <span className="shadow-stat-value highlight-good">{data.value_multiplier}x</span>
          <span className="shadow-stat-label">Value</span>
        </div>
        <div className="shadow-stat">
          <span className="shadow-stat-value">{formatTokens(data.total_tokens)}</span>
          <span className="shadow-stat-label">Tokens</span>
        </div>
      </div>

      {/* Per-model breakdown */}
      {data.per_model.length > 0 && (
        <div className="shadow-breakdown">
          <h3>By Model</h3>
          {data.per_model.map((m, i) => (
            <div key={i} className="shadow-row">
              <span className="shadow-row-name">{m.display_name}</span>
              <span className="shadow-row-tokens">{formatTokens(m.input_tokens + m.output_tokens + m.cache_read_tokens + m.cache_create_tokens)}</span>
              <span className="shadow-row-cost">{formatCost(m.shadow_cost)}</span>
            </div>
          ))}
        </div>
      )}

      {data.first_session_date && (
        <div className="shadow-period">
          Since {new Date(data.first_session_date).toLocaleDateString()}
        </div>
      )}
    </div>
  );
}
