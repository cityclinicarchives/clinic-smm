from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings


DEFAULT_TEXT_PRICES_USD_PER_1M: dict[str, tuple[float, float]] = {
    # input, output. These are editable estimates; env variables override them.
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.4": (2.5, 15.0),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.3": (1.25, 10.0),
    "gpt-5.3-mini": (0.25, 2.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1-nano": (0.1, 0.4),
}

DEFAULT_IMAGE_PRICES_USD_PER_IMAGE: dict[str, float] = {
    # Conservative low-quality square estimate. Override via COST_IMAGE_1024_USD if needed.
    "gpt-image-1": 0.011,
    "gpt-image-1.5": 0.009,
    "gpt-image-1-mini": 0.005,
    "gpt-image-2": 0.005,
}


def _getattr_or_key(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _money(value: float | int | None) -> float:
    try:
        return round(float(value or 0.0), 6)
    except Exception:
        return 0.0


def text_prices_for_model(model: str) -> tuple[float, float, str]:
    if settings.cost_text_input_usd_per_1m > 0 or settings.cost_text_output_usd_per_1m > 0:
        return (
            float(settings.cost_text_input_usd_per_1m),
            float(settings.cost_text_output_usd_per_1m),
            "env",
        )
    key = (model or "").strip()
    if key in DEFAULT_TEXT_PRICES_USD_PER_1M:
        i, o = DEFAULT_TEXT_PRICES_USD_PER_1M[key]
        return i, o, "default_table"
    # Try prefix match for versioned model names like gpt-5.5-2026-04-xx.
    for prefix, prices in DEFAULT_TEXT_PRICES_USD_PER_1M.items():
        if key.startswith(prefix):
            return prices[0], prices[1], "default_table_prefix"
    return 0.0, 0.0, "unknown_model"


def image_price_for_model(model: str) -> tuple[float, str]:
    if settings.cost_image_1024_usd > 0:
        return float(settings.cost_image_1024_usd), "env"
    key = (model or "").strip()
    if key in DEFAULT_IMAGE_PRICES_USD_PER_IMAGE:
        return DEFAULT_IMAGE_PRICES_USD_PER_IMAGE[key], "default_table"
    for prefix, price in DEFAULT_IMAGE_PRICES_USD_PER_IMAGE.items():
        if key.startswith(prefix):
            return price, "default_table_prefix"
    return 0.0, "unknown_model"


def cost_from_response_usage(operation: str, model: str, response: Any, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    usage = _getattr_or_key(response, "usage")
    input_tokens = int(_getattr_or_key(usage, "input_tokens", _getattr_or_key(usage, "prompt_tokens", 0)) or 0)
    output_tokens = int(_getattr_or_key(usage, "output_tokens", _getattr_or_key(usage, "completion_tokens", 0)) or 0)
    total_tokens = int(_getattr_or_key(usage, "total_tokens", input_tokens + output_tokens) or (input_tokens + output_tokens))
    input_price, output_price, price_source = text_prices_for_model(model)
    estimated = (input_tokens / 1_000_000.0 * input_price) + (output_tokens / 1_000_000.0 * output_price)
    return {
        "kind": "text",
        "operation": operation,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_usd_per_1m": input_price,
        "output_usd_per_1m": output_price,
        "image_count": 0,
        "estimated_cost_usd": _money(estimated),
        "price_source": price_source,
        "metadata": metadata or {},
    }


def cost_for_image_generation(operation: str, model: str, image_count: int = 1, size: str = "1024x1024", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    unit_price, price_source = image_price_for_model(model)
    image_count = max(0, int(image_count or 0))
    return {
        "kind": "image",
        "operation": operation,
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "image_count": image_count,
        "image_size": size,
        "unit_usd_per_image": unit_price,
        "estimated_cost_usd": _money(unit_price * image_count),
        "price_source": price_source,
        "metadata": metadata or {},
    }


def free_operation(operation: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "kind": "local",
        "operation": operation,
        "model": "local",
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "image_count": 0,
        "estimated_cost_usd": 0.0,
        "price_source": "local_no_api_cost",
        "metadata": metadata or {},
    }


def aggregate_costs(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    items = [x for x in (items or []) if isinstance(x, dict)]
    return {
        "currency": "USD",
        "estimated_total_usd": _money(sum(float(x.get("estimated_cost_usd") or 0.0) for x in items)),
        "input_tokens": sum(int(x.get("input_tokens") or 0) for x in items),
        "output_tokens": sum(int(x.get("output_tokens") or 0) for x in items),
        "total_tokens": sum(int(x.get("total_tokens") or 0) for x in items),
        "image_count": sum(int(x.get("image_count") or 0) for x in items),
        "items": items,
        "note": "Расчетная стоимость. Точные списания зависят от актуальных тарифов OpenAI, размера/качества изображений, кэширования и настроек аккаунта.",
    }


def save_cost_event(operation: str, asset_id: int | None, summary: dict[str, Any]) -> None:
    if not settings.cost_tracking_enabled:
        return
    path = Path("storage/costs/cost-log.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "asset_id": asset_id,
        "summary": summary,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def format_cost_summary(summary: dict[str, Any] | None) -> str:
    if not isinstance(summary, dict):
        return ""
    total = float(summary.get("estimated_total_usd") or 0.0)
    input_tokens = int(summary.get("input_tokens") or 0)
    output_tokens = int(summary.get("output_tokens") or 0)
    images = int(summary.get("image_count") or 0)
    lines = ["", "💰 <b>Расчетная стоимость:</b>"]
    if input_tokens or output_tokens:
        lines.append(f"Tokens input/output: <code>{input_tokens}</code> / <code>{output_tokens}</code>")
    if images:
        lines.append(f"Image generations: <code>{images}</code>")
    lines.append(f"Estimated cost: <code>${total:.4f}</code>")
    # Warn when model has no price table.
    unknown = [x for x in summary.get("items", []) if x.get("price_source") == "unknown_model"]
    if unknown:
        lines.append("⚠️ Для части моделей цена не задана, расчет может быть неполным.")
    return "\n".join(lines)
