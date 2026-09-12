"""S0 probe: confirm the five raw materials exist and are usable.

Pulls one small sample each of: day-ahead price, day-ahead load forecast,
day-ahead wind/solar forecast, one outage record (ENTSO-E), and one
REMIT/UMM market message (Nord Pool). Writes everything under data/s0/
and prints a compact summary for each pull. No modelling, no full
history, no event curation -- see C_adapter_Entso_E/Claude.MD for why.

Run: uv run python src/s0_probe.py
"""

from __future__ import annotations

import json
import os
import time
import traceback

import pandas as pd
import requests
from dotenv import load_dotenv
from entsoe import EntsoePandasClient

from common import ROOT, _infer_freq, _redact_fields, _stringify_keys

DATA_DIR = ROOT / "data" / "s0"
DATA_DIR.mkdir(parents=True, exist_ok=True)

ZONE_CODE = "SE_3"  # entsoe-py mapping key -> EIC 10Y1001A1001A46L
ZONE_EIC = "10Y1001A1001A46L"
TZ = "Europe/Stockholm"

HOURLY_DAY = "2025-09-15"
QUARTER_HOUR_DAY = "2025-10-15"
OUTAGE_WINDOW_START = "2025-09-01"
OUTAGE_WINDOW_END = "2025-09-30"

UMM_BASE = "https://ummapi.nordpoolgroup.com/messages"

summary: dict[str, dict] = {}


def _day_bounds(day: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(day, tz=TZ)
    end = start + pd.Timedelta(days=1)
    return start, end


def _report(name: str, ok: bool, **fields) -> None:
    fields = _redact_fields(fields)
    entry = {"ok": ok, **fields}
    summary[name] = entry
    print(f"--- {name} ---")
    for k, v in fields.items():
        print(f"  {k}: {v}")


def pull_price(client: EntsoePandasClient) -> None:
    for day, label in [(HOURLY_DAY, "hourly"), (QUARTER_HOUR_DAY, "quarter_hourly")]:
        name = f"price_{label}"
        try:
            start, end = _day_bounds(day)
            s = client.query_day_ahead_prices(ZONE_CODE, start=start, end=end)
            freq = _infer_freq(s.index)
            out = DATA_DIR / f"{name}.csv"
            s.to_csv(out, header=["price_eur_mwh"])
            _report(
                name,
                True,
                day=day,
                n_points=len(s),
                inferred_resolution=freq,
                index_start=str(s.index.min()),
                index_end=str(s.index.max()),
                first_3=_stringify_keys(s.head(3).to_dict()),
                file=str(out.relative_to(ROOT)),
            )
        except Exception as e:
            _report(name, False, day=day, error=str(e), trace=traceback.format_exc(limit=3))
        time.sleep(0.5)


def pull_load_forecast(client: EntsoePandasClient) -> None:
    for day, label in [(HOURLY_DAY, "hourly"), (QUARTER_HOUR_DAY, "quarter_hourly")]:
        name = f"load_forecast_{label}"
        try:
            start, end = _day_bounds(day)
            df = client.query_load_forecast(ZONE_CODE, start=start, end=end, process_type="A01")
            freq = _infer_freq(df.index)
            out = DATA_DIR / f"{name}.csv"
            df.to_csv(out)
            _report(
                name,
                True,
                day=day,
                n_rows=len(df),
                inferred_resolution=freq,
                columns=list(df.columns),
                index_start=str(df.index.min()),
                index_end=str(df.index.max()),
                first_3=_stringify_keys(df.head(3).to_dict(orient="index")),
                file=str(out.relative_to(ROOT)),
            )
        except Exception as e:
            _report(name, False, day=day, error=str(e), trace=traceback.format_exc(limit=3))
        time.sleep(0.5)


def pull_wind_solar_forecast(client: EntsoePandasClient) -> None:
    for day, label in [(HOURLY_DAY, "hourly"), (QUARTER_HOUR_DAY, "quarter_hourly")]:
        name = f"wind_solar_forecast_{label}"
        try:
            start, end = _day_bounds(day)
            df = client.query_wind_and_solar_forecast(
                ZONE_CODE, start=start, end=end, psr_type=None, process_type="A01"
            )
            freq = _infer_freq(df.index)
            out = DATA_DIR / f"{name}.csv"
            df.to_csv(out)
            _report(
                name,
                True,
                day=day,
                n_rows=len(df),
                inferred_resolution=freq,
                columns=list(df.columns),
                index_start=str(df.index.min()),
                index_end=str(df.index.max()),
                first_3=_stringify_keys(df.head(3).to_dict(orient="index")),
                file=str(out.relative_to(ROOT)),
            )
        except Exception as e:
            _report(name, False, day=day, error=str(e), trace=traceback.format_exc(limit=3))
        time.sleep(0.5)


def pull_outage(client: EntsoePandasClient) -> pd.Series | None:
    name = "outage_generation_units"
    matched_row = None
    try:
        start = pd.Timestamp(OUTAGE_WINDOW_START, tz=TZ)
        end = pd.Timestamp(OUTAGE_WINDOW_END, tz=TZ)
        df = client.query_unavailability_of_generation_units(ZONE_CODE, start=start, end=end)
        if df.empty:
            _report(name, False, error="empty result for SE_3, generation units", window=(OUTAGE_WINDOW_START, OUTAGE_WINDOW_END))
        else:
            qty_col = None
            for cand in ["avail_qty", "avail_qty_A03", "quantity"]:
                if cand in df.columns:
                    qty_col = cand
                    break
            if qty_col is not None:
                sorted_df = df.sort_values(qty_col)
            else:
                sorted_df = df
            matched_row = sorted_df.iloc[0]
            out = DATA_DIR / "outage_row.json"
            row_dict = json.loads(matched_row.to_json(date_format="iso"))
            out.write_text(json.dumps(row_dict, indent=2, default=str), encoding="utf-8")
            full_out = DATA_DIR / "outage_all_rows.csv"
            df.to_csv(full_out)
            _report(
                name,
                True,
                n_rows=len(df),
                columns=list(df.columns),
                qty_col_used=qty_col,
                matched_row=row_dict,
                file=str(out.relative_to(ROOT)),
                all_rows_file=str(full_out.relative_to(ROOT)),
            )
    except Exception as e:
        _report(name, False, error=str(e), trace=traceback.format_exc(limit=3))
        # fallback attempt
        try:
            name2 = "outage_production_units_fallback"
            start = pd.Timestamp(OUTAGE_WINDOW_START, tz=TZ)
            end = pd.Timestamp(OUTAGE_WINDOW_END, tz=TZ)
            df2 = client.query_unavailability_of_production_units(ZONE_CODE, start=start, end=end)
            out2 = DATA_DIR / "outage_all_rows_production_fallback.csv"
            df2.to_csv(out2)
            _report(name2, not df2.empty, n_rows=len(df2), file=str(out2.relative_to(ROOT)))
        except Exception as e2:
            _report("outage_production_units_fallback", False, error=str(e2))
    time.sleep(0.5)
    return matched_row


def pull_umm(matched_row: pd.Series | None) -> list[dict]:
    name = "umm_messages"
    items: list[dict] = []
    try:
        params = {
            "areas": ZONE_EIC,
            "publicationStartDate": OUTAGE_WINDOW_START,
            "publicationStopDate": OUTAGE_WINDOW_END,
            "limit": 200,
        }
        r = requests.get(UMM_BASE, params=params, timeout=20)
        r.raise_for_status()
        payload = r.json()
        items = payload.get("items", [])
        _report(
            name,
            True,
            request_params={k: v for k, v in params.items()},
            reported_total=payload.get("total"),
            n_items_fetched=len(items),
        )
        out = DATA_DIR / "umm_messages_sample_raw_count.json"
        out.write_text(
            json.dumps({"reported_total": payload.get("total"), "n_fetched": len(items)}, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        _report(name, False, error=str(e), trace=traceback.format_exc(limit=3))
    time.sleep(0.3)

    # try to match the outage row to a UMM message
    matched_msg = None
    if matched_row is not None and items:
        row_name = ""
        for cand in ["production_resource_name", "production_resource_psr_name", "name"]:
            if cand in matched_row.index and matched_row.get(cand):
                row_name = str(matched_row.get(cand))
                break
        row_eic = None
        for cand in ["production_resource_id", "unit_eic", "eic"]:
            if cand in matched_row.index and matched_row.get(cand):
                row_eic = matched_row.get(cand)
                break
        for it in items:
            for pu in it.get("productionUnits", []):
                name_match = row_name and row_name.strip().lower() in str(pu.get("name", "")).strip().lower()
                eic_match = row_eic and row_eic == pu.get("eic")
                if name_match or eic_match:
                    matched_msg = it
                    break
            if matched_msg:
                break

    if matched_msg is None and items:
        # fallback: no name/EIC match found; keep the newest message with non-empty remarks as the "one sample"
        for it in items:
            if it.get("remarks"):
                matched_msg = it
                break
        if matched_msg is None:
            matched_msg = items[0]
        _report(
            "umm_match",
            False,
            row_name_searched=row_name if matched_row is not None else None,
            row_eic_searched=row_eic if matched_row is not None else None,
            note="no outage row matched a UMM message by name/EIC; saved a fallback message instead",
        )
    elif matched_msg is not None:
        matched_periods = []
        for pu in matched_msg.get("productionUnits", []):
            for tp in pu.get("timePeriods", []):
                matched_periods.append(
                    {"unit": pu.get("name"), "eic": pu.get("eic"), **tp}
                )
        _report(
            "umm_match",
            True,
            row_name_searched=row_name,
            row_eic_searched=row_eic,
            matched_message_id=matched_msg.get("messageId"),
            matched_time_periods=matched_periods,
        )

    if matched_msg is not None:
        out = DATA_DIR / "umm_message_matched.json"
        out.write_text(json.dumps(matched_msg, indent=2), encoding="utf-8")

    return items


def english_check(items: list[dict]) -> None:
    name = "english_check"
    swedish_words = {"och", "för", "på", "inte", "från", "vid", "till", "efter", "under"}
    english_words = {"and", "for", "the", "due", "to", "on", "at", "from", "after", "during"}

    n_total = len(items)
    n_empty = 0
    n_english_heuristic = 0
    n_swedish_heuristic = 0
    n_ambiguous = 0
    examples_empty = []
    examples_english = []
    examples_other = []

    for it in items:
        remarks = (it.get("remarks") or "").strip()
        reason = (it.get("unavailabilityReason") or "").strip()
        text = (remarks + " " + reason).strip()
        if not text:
            n_empty += 1
            if len(examples_empty) < 1:
                examples_empty.append({"messageId": it.get("messageId")})
            continue
        lower = text.lower()
        has_diacritic = any(ch in lower for ch in "åäö")
        tokens = set(lower.replace(".", " ").replace(",", " ").split())
        sw_hits = len(tokens & swedish_words)
        en_hits = len(tokens & english_words)
        if has_diacritic or sw_hits > en_hits:
            n_swedish_heuristic += 1
            if len(examples_other) < 3:
                examples_other.append({"messageId": it.get("messageId"), "text": text[:200]})
        else:
            n_english_heuristic += 1
            if len(examples_english) < 3:
                examples_english.append({"messageId": it.get("messageId"), "text": text[:200]})
        if sw_hits == en_hits == 0 and not has_diacritic and len(tokens) < 3:
            n_ambiguous += 1

    langdetect_available = False
    langdetect_counts: dict[str, int] = {}
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0
        langdetect_available = True
        for it in items:
            remarks = (it.get("remarks") or "").strip()
            reason = (it.get("unavailabilityReason") or "").strip()
            text = (remarks + " " + reason).strip()
            if not text:
                continue
            try:
                lang = detect(text)
            except Exception:
                lang = "unknown"
            langdetect_counts[lang] = langdetect_counts.get(lang, 0) + 1
    except ImportError:
        pass

    result = {
        "n_total_messages": n_total,
        "n_empty_remarks_and_reason": n_empty,
        "frac_empty": (n_empty / n_total) if n_total else None,
        "n_english_heuristic": n_english_heuristic,
        "n_swedish_heuristic": n_swedish_heuristic,
        "frac_english_of_nonempty": (n_english_heuristic / (n_total - n_empty)) if (n_total - n_empty) else None,
        "langdetect_available": langdetect_available,
        "langdetect_label_counts": langdetect_counts,
        "examples_empty": examples_empty,
        "examples_english": examples_english,
        "examples_other_or_swedish": examples_other,
    }
    out = DATA_DIR / "english_check_summary.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    _report(name, True, **{k: v for k, v in result.items() if k not in ("examples_empty", "examples_english", "examples_other_or_swedish")})
    print("  examples_english:", examples_english[:1])
    print("  examples_other_or_swedish:", examples_other[:1])


def main() -> None:
    load_dotenv()
    api_key = os.environ.get("ENTSOE_E_API_KEY")
    if not api_key:
        raise RuntimeError("ENTSOE_E_API_KEY not found in environment / .env")

    client = EntsoePandasClient(api_key=api_key)

    print(f"Zone: {ZONE_CODE} ({ZONE_EIC}), timezone {TZ}")
    print(f"Hourly sample day: {HOURLY_DAY}; quarter-hourly sample day: {QUARTER_HOUR_DAY}")
    print(f"Outage window: {OUTAGE_WINDOW_START} to {OUTAGE_WINDOW_END}")
    print("Estimated request count: ~8 ENTSO-E calls, ~1 UMM bulk call (200 messages, one request)")
    print()

    t0 = time.time()

    pull_price(client)
    pull_load_forecast(client)
    pull_wind_solar_forecast(client)
    matched_row = pull_outage(client)
    items = pull_umm(matched_row)
    english_check(items)

    elapsed = time.time() - t0
    summary["_meta"] = {"elapsed_seconds": elapsed, "zone": ZONE_CODE, "zone_eic": ZONE_EIC}
    (DATA_DIR / "run_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nDone in {elapsed:.1f}s. Full summary written to {DATA_DIR / 'run_summary.json'}")


if __name__ == "__main__":
    main()
