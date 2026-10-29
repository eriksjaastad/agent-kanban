"""
Shadow Pricing — compute what Claude Max usage would cost at API rates.

Reads token usage from ~/.claude/stats-cache.json and applies model pricing
from dashboard/model_registry.json to calculate shadow cost.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("shadow-pricing")

_REGISTRY_PATH = Path(__file__).parent / "model_registry.json"
_STATS_CACHE_PATH = Path.home() / ".claude" / "stats-cache.json"

# Cache registry in memory (changes rarely)
_registry_cache: dict | None = None


def _load_registry() -> dict:
    """Load model registry, keyed by model_id for fast lookup."""
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache

    data = json.loads(_REGISTRY_PATH.read_text())
    registry = {}
    for model in data.get("models", []):
        mid = model["model_id"]
        pricing = model.get("pricing_per_1M", {})
        registry[mid] = {
            "display_name": model.get("display_name", mid),
            "provider": model.get("provider", "unknown"),
            "input_usd": pricing.get("input_usd", 0),
            "cached_input_usd": pricing.get("cached_input_usd", 0),
            "output_usd": pricing.get("output_usd", 0),
        }
    _registry_cache = registry
    return registry


def _compute_model_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_create_tokens: int,
    registry: dict,
) -> float:
    """Compute shadow cost in USD for a single model's usage."""
    pricing = registry.get(model_id)
    if not pricing:
        return 0.0

    input_rate = pricing["input_usd"]
    output_rate = pricing["output_usd"]
    cached_rate = pricing["cached_input_usd"]
    # Cache creation billed at 1.25x input rate for Anthropic models
    cache_write_rate = input_rate * 1.25 if pricing["provider"] == "anthropic" else input_rate

    cost = (
        (input_tokens * input_rate)
        + (output_tokens * output_rate)
        + (cache_read_tokens * cached_rate)
        + (cache_create_tokens * cache_write_rate)
    ) / 1_000_000

    return cost


def get_shadow_pricing_data() -> dict:
    """Build complete shadow pricing response from stats-cache + registry."""
    stats = json.loads(_STATS_CACHE_PATH.read_text())
    registry = _load_registry()

    model_usage = stats.get("modelUsage", {})
    per_model = []
    total_shadow_cost = 0.0
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_create = 0

    for model_id, usage in model_usage.items():
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        cache_read = usage.get("cacheReadInputTokens", 0)
        cache_create = usage.get("cacheCreationInputTokens", 0)

        cost = _compute_model_cost(
            model_id, input_tokens, output_tokens, cache_read, cache_create, registry
        )

        pricing = registry.get(model_id)
        per_model.append({
            "model_id": model_id,
            "display_name": pricing["display_name"] if pricing else model_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_create_tokens": cache_create,
            "shadow_cost": round(cost, 2),
        })

        total_shadow_cost += cost
        total_input += input_tokens
        total_output += output_tokens
        total_cache_read += cache_read
        total_cache_create += cache_create

    # Sort by cost descending
    per_model.sort(key=lambda m: m["shadow_cost"], reverse=True)

    subscription_cost = 200.0  # Claude Max monthly
    value_multiplier = round(total_shadow_cost / subscription_cost, 1) if subscription_cost > 0 else 0

    return {
        "total_shadow_cost": round(total_shadow_cost, 2),
        "subscription_cost_monthly": subscription_cost,
        "value_multiplier": value_multiplier,
        "total_tokens": total_input + total_output + total_cache_read + total_cache_create,
        "per_model": per_model,
        "first_session_date": stats.get("firstSessionDate", ""),
        "last_computed_date": stats.get("lastComputedDate", ""),
    }
