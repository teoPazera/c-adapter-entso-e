"""Context selection, prompt rendering, response validation, and caching for S4."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from common import ROOT
from llm_client import JsonClient

DATA_S2 = ROOT / "data" / "s2"
CACHE_DIR = ROOT / "data" / "s4" / "llm_cache"
TZ = ZoneInfo("Europe/Stockholm")

EFFECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reasoning": {"type": "string", "minLength": 1},
        "p_active": {"type": "number", "minimum": 0, "maximum": 1},
        "start_hour": {
            "type": "object", "additionalProperties": False,
            "properties": {"mean": {"type": "number", "minimum": 0, "maximum": 24}, "sd": {"type": "number", "minimum": 0, "maximum": 12}},
            "required": ["mean", "sd"],
        },
        "end_hour": {
            "type": "object", "additionalProperties": False,
            "properties": {"mean": {"type": "number", "minimum": 0, "maximum": 24}, "sd": {"type": "number", "minimum": 0, "maximum": 12}},
            "required": ["mean", "sd"],
        },
        "size_mw": {
            "type": "object", "additionalProperties": False,
            "properties": {"p10": {"type": "number", "minimum": 0}, "p50": {"type": "number", "minimum": 0}, "p90": {"type": "number", "minimum": 0}},
            "required": ["p10", "p50", "p90"],
        },
        "direction": {"type": "string", "enum": ["reduce", "none"]},
    },
    "required": ["reasoning", "p_active", "start_hour", "end_hour", "size_mw", "direction"],
}

SYSTEM_PROMPT = """You are a context adapter for a frozen day-ahead electricity-price forecasting model in Sweden SE3. Infer only the uncertain effect of an outage document on one future availability-driver input. Return the requested JSON only. The `reasoning` field must come first and is a brief evidence-grounded audit trail from the supplied documents, not hidden chain-of-thought. Do not use realized outcomes or documents published after the forecast origin."""


def load_events() -> list[dict[str, Any]]:
    payload = json.loads((DATA_S2 / "events.json").read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else payload["events"]


def event_by_id(event_id: str) -> dict[str, Any]:
    return next(event for event in load_events() if event["event_id"] == event_id)


def delivery_t0(day: str) -> datetime:
    """D-1 12:00 local, deliberately calendar-based rather than 12h duration."""
    D = datetime.fromisoformat(day).replace(tzinfo=TZ)
    return (D - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)


def _parse_utc(value: str) -> datetime:
    date, rest = value.rstrip("Z").split("T")
    clock, frac = rest.split(".")
    return datetime.fromisoformat(f"{date}T{clock}.{(frac + '000000')[:6]}+00:00")


def available_versions(event: dict[str, Any], day: str) -> list[dict[str, Any]]:
    t0_utc = delivery_t0(day).astimezone(ZoneInfo("UTC"))
    versions = [v for v in event["versions"] if _parse_utc(v["publicationDate"]) <= t0_utc]
    return sorted(versions, key=lambda v: (_parse_utc(v["publicationDate"]), v["version"]))


def rendered_context(event: dict[str, Any], day: str) -> tuple[str, list[dict[str, Any]]]:
    versions = available_versions(event, day)
    blocks: list[str] = []
    for v in versions:
        p = DATA_S2 / "documents" / event["event_id"] / f"{v['messageId']}_v{v['version']}.txt"
        blocks.append(f"--- version {v['version']} | published {v['publicationDate']} ---\n{p.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(blocks), versions


def driver_for(event: dict[str, Any]) -> tuple[str, str]:
    if event["kind"] == "generation":
        return "avail_gen_mw", "reduce"
    return "unavail_tx_import_mw", "increase"


def render_prompt(event: dict[str, Any], day: str, context: str) -> str:
    driver, operation = driver_for(event)
    meta = {k: event.get(k) for k in ("event_id", "kind", "unit_name", "unit_eic|pair", "zone", "realised_start", "realised_end", "reduction_mw", "businesstype")}
    return f"""Forecast origin: {delivery_t0(day).isoformat()} (D-1 12:00 Europe/Stockholm).
Delivery day: {day}; output hours are local Stockholm hours 0..23.

Frozen forecast model:
- Predicts the 24 hourly SE3 day-ahead electricity prices in EUR/MWh.
- Inputs include price lags, day-ahead load/wind/solar forecasts, calendar features, and three availability drivers.
- It is already trained and will not be retrained.
- You change only the day-D path of `{driver}`. The baseline holds this driver constant at its D-1 12:00 value.
- For this event, an active effect should `{operation}` the driver by the outage MW over active hours.

Event metadata:
{json.dumps(meta, indent=2)}

Documents available at the forecast origin, in revision order:
{context if context else '(No document had been published yet.)'}

Return a distribution for this delivery day only:
- `p_active`: probability an event has a material effect on at least one hour.
- `start_hour`, `end_hour`: local delivery-day bounds in [0,24]; use sd=0 for a clear boundary.
- `size_mw`: effect magnitude in MW, with p10 <= p50 <= p90.
- `direction`: use `reduce` for generation outages; `none` if the documents imply no effect.
- Be conservative; do not invent precision beyond the supplied documents.
"""


def validate_effect(value: dict[str, Any]) -> dict[str, Any]:
    expected = set(EFFECT_SCHEMA["properties"])
    if set(value) != expected:
        raise ValueError(f"Unexpected effect keys: {sorted(value)}")
    for key in ("p_active",):
        if not isinstance(value[key], (int, float)) or not 0 <= value[key] <= 1:
            raise ValueError(f"Invalid {key}")
    for key in ("start_hour", "end_hour"):
        item = value[key]
        if set(item) != {"mean", "sd"} or not 0 <= item["mean"] <= 24 or not 0 <= item["sd"] <= 12:
            raise ValueError(f"Invalid {key}")
    sizes = value["size_mw"]
    if set(sizes) != {"p10", "p50", "p90"} or not (0 <= sizes["p10"] <= sizes["p50"] <= sizes["p90"]):
        raise ValueError("Invalid ordered size_mw quantiles")
    if value["direction"] not in {"reduce", "none"}:
        raise ValueError("Invalid direction")
    if not isinstance(value["reasoning"], str) or not value["reasoning"].strip():
        raise ValueError("reasoning must be non-empty")
    return value


def cached_effect(client: JsonClient, event: dict[str, Any], day: str, force: bool = False, cache_salt: str = "") -> tuple[dict[str, Any], dict[str, Any], str]:
    context, versions = rendered_context(event, day)
    user = render_prompt(event, day, context)
    key_input = json.dumps({"system": SYSTEM_PROMPT, "user": user, "schema": EFFECT_SCHEMA, "cache_salt": cache_salt}, sort_keys=True)
    digest = hashlib.sha256(key_input.encode()).hexdigest()
    path = CACHE_DIR / f"{digest}.json"
    if path.exists() and not force:
        cached = json.loads(path.read_text(encoding="utf-8"))
        return cached["effect"], {**cached.get("metadata", {}), "cache_hit": True}, digest
    effect, metadata = client.complete_json(SYSTEM_PROMPT, user, EFFECT_SCHEMA)
    effect = validate_effect(effect)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"event_id": event["event_id"], "day": day, "t0": delivery_t0(day).isoformat(), "available_versions": [v["version"] for v in versions], "effect": effect, "metadata": metadata, "cache_salt": cache_salt}, indent=2, default=str), encoding="utf-8")
    return effect, {**metadata, "cache_hit": False, "available_versions": [v["version"] for v in versions]}, digest
