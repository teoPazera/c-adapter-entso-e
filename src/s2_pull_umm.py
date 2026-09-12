"""S2 step 1: pull Nord Pool UMM (REMIT/inside-information) messages relevant
to SE3 -- thread lists for the six generation units that can plausibly clear
the >=300 MW reduction threshold (five Vattenfall nuclear units plus
Trangslet hydro -- see notes/02-events.md for why Trangslet was added beyond
the plan's original five-EIC list), SE3-touching transmission thread lists
(year-chunked), and three enum tables.

Idempotent: every response is cached as JSON under data/raw/s2/<query_key>.json.
A re-run of main() makes 0 new requests.

Also exposes fetch_message_versions(), imported by s2_curate_events.py to
pull full version history for threads matched to a candidate ENTSO-E outage.
It is kept here rather than duplicated so both scripts' pulls land in the
same cache directory under the same idempotency guarantee, and so
s2_curate_events.py's own request count can be folded into one pull_summary.

No auth token: this API takes no key, so none of common.py's ENTSO-E-key
machinery (_redact, make_client) applies here -- only ROOT is reused, for
the same data/ layout convention as the S1 scripts.

Run: uv run python src/s2_pull_umm.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from common import ROOT

DATA_RAW = ROOT / "data" / "raw" / "s2"
DATA_RAW.mkdir(parents=True, exist_ok=True)
DATA_S2 = ROOT / "data" / "s2"
DATA_S2.mkdir(parents=True, exist_ok=True)

BASE = "https://ummapi.nordpoolgroup.com"
# Verified live 2026-09-11: this API 403s on Python's default urllib/requests
# User-Agent string; a normal browser-like one works. No auth token needed.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "thesis-research-script/0.1 (research use; contact pazerateo@gmail.com)"
)
SLEEP_S = 0.5

# The five Vattenfall SE3 nuclear units the plan named, plus Trangslet
# (hydro, 330 MW) -- added here because it is the plan's own 1-of-60
# generation candidate that clears >=300 MW reduction but was left off the
# original EIC constant list; excluding it silently would have reported
# "no_umm_thread" for a unit we never actually queried. One extra request.
# EICs verified against data/s1/installed_capacity_per_unit_se3.csv (S1 pull).
GENERATION_UNITS = {
    "46WPU0000000014Y": "Ringhals 4",
    "46WPU00000000121": "Ringhals 3",
    "46WPU0000000015W": "Forsmark 1",
    "46WPU0000000016U": "Forsmark 2",
    "46WPU0000000017S": "Forsmark 3",
    "46WPU0000000018Q": "Trangslet",
}

# Oskarshamn 3 is deliberately NOT in GENERATION_UNITS and never queried --
# the plan's live check found no UMM thread with an event in 2018-2025 for
# this unit (6 threads total, event years 2017 and 2026 only). Its ~10
# candidate rows are rejected with reason "okg_unit" without a live call.
OKG_EIC = "46WPU0000000061P"

# EICs verified 2026-09-11 against the installed entsoe-py package's own
# mappings (entsoe.mappings), not guessed -- same practice S1b used for zone
# codes. SE3's EIC also independently confirmed live: it is the areaEic the
# UMM API itself returns for Forsmark-3 messages.
SE3_EIC = "10Y1001A1001A46L"
BORDER_EICS = {
    "no1": "10YNO-1--------2",
    "fi": "10YFI-1--------U",
    "dk1": "10YDK-1--------W",
}

WINDOW_START_YEAR = 2023
WINDOW_END = "2026-09-12"  # matches S1's WINDOW_END, exclusive

_session = requests.Session()
_session.headers.update({"User-Agent": UA})
request_count = 0


# ---------------------------------------------------------------- cache I/O

def _cache_path(query_key: str) -> Path:
    return DATA_RAW / f"{query_key}.json"


def _error_path(query_key: str) -> Path:
    return DATA_RAW / f"{query_key}.error.json"


def fetch_json(query_key: str, path: str, params: dict | None = None, allow_error: bool = False):
    """GET BASE+path with params; cache the JSON body under
    data/raw/s2/<query_key>.json. A cache hit (ok or error) costs 0 requests.
    allow_error=True caches failures instead of raising, returning None."""
    global request_count
    cache = _cache_path(query_key)
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    err = _error_path(query_key)
    if err.exists():
        return None
    try:
        resp = _session.get(f"{BASE}{path}", params=params or {}, timeout=30)
        request_count += 1
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        request_count += 1
        if not allow_error:
            raise
        err.write_text(json.dumps({"error": str(e), "path": path, "params": params}), encoding="utf-8")
        print(f"  [{query_key}] FAILED - {e}")
        time.sleep(SLEEP_S)
        return None
    cache.write_text(json.dumps(data), encoding="utf-8")
    time.sleep(SLEEP_S)
    return data


def fetch_message_versions(message_id: str) -> list[dict]:
    """GET /messages/{messageId} -- every version of one thread, cached.
    Imported by s2_curate_events.py during candidate matching; lives here so
    both scripts' pulls share one cache dir and one idempotency guarantee."""
    data = fetch_json(f"message_versions_{message_id}", f"/messages/{message_id}", allow_error=True)
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------- phase 1: list pulls

def fetch_enums() -> dict:
    # Plan's stated paths (/unavailabilitytypes, /reasoncodes) 404 live;
    # corrected paths verified against the API's own swagger spec
    # (/swagger/v1/swagger.json) 2026-09-11.
    return {
        "unavailabilitytypes": fetch_json("enum_unavailabilitytypes", "/infrastructure/unavailabilitytypes"),
        "reasoncodes": fetch_json("enum_reasoncodes", "/infrastructure/reasoncodes"),
        "fueltypes": fetch_json("enum_fueltypes", "/infrastructure/fueltypes"),
    }


def fetch_unit_threads() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for eic, name in GENERATION_UNITS.items():
        data = fetch_json(f"unit_threads_{eic}", "/messages", {
            "Units": eic, "IncludeOutdated": "false", "Limit": 500,
        })
        print(f"  unit {name} ({eic}): {len(data['items'])} of {data['total']} threads")
        if data["total"] > len(data["items"]):
            raise RuntimeError(f"{name}: total {data['total']} exceeds Limit=500 -- add paging")
        out[eic] = data["items"]
    return out


def fetch_transmission_threads() -> list[dict]:
    """One /messages call per calendar year with Areas=<SE3 EIC>&MessageTypes=3
    (no Units/pair filter -- SE3-touching messages already carry both
    direction legs of whichever border they describe in transmissionUnits[],
    verified live). Bucketing by year is a request-budget device only, not a
    filter: every thread's *latest* version has exactly one publicationDate,
    and the four year windows are contiguous and cover the full pull window,
    so every thread lands in exactly one bucket regardless of which year its
    underlying event occurred in."""
    items: list[dict] = []
    end_year = int(WINDOW_END[:4]) + 1
    for y in range(WINDOW_START_YEAR, end_year):
        pub_start = f"{y}-01-01T00:00:00Z"
        pub_stop = f"{y + 1}-01-01T00:00:00Z" if (y + 1) < end_year else f"{WINDOW_END}T00:00:00Z"
        key = f"tx_threads_{y}"
        params = {
            "Areas": SE3_EIC, "MessageTypes": 3, "IncludeOutdated": "false",
            "Limit": 500, "PublicationStartDate": pub_start, "PublicationStopDate": pub_stop,
        }
        data = fetch_json(key, "/messages", params)
        print(f"  transmission {y}: {len(data['items'])} of {data['total']} threads")
        page_items = list(data["items"])
        skip = len(page_items)
        page = 1
        while data["total"] > skip:
            page_params = dict(params, Skip=skip)
            data = fetch_json(f"{key}_skip{skip}", "/messages", page_params)
            page_items.extend(data["items"])
            skip += len(data["items"])
            page += 1
            if page > 5:
                raise RuntimeError(f"transmission {y}: too many pages ({page}), check Skip logic")
        items.extend(page_items)
    return items


# ---------------------------------------------------------------- summary

def write_pull_summary(extra: dict | None = None) -> None:
    out = DATA_S2 / "pull_summary.json"
    existing = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    existing.setdefault("runs", []).append({
        "requests_this_run": request_count,
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **(extra or {}),
    })
    existing["requests_total_all_runs"] = sum(r["requests_this_run"] for r in existing["runs"])
    out.write_text(json.dumps(existing, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- main

def main() -> None:
    t0 = time.time()
    print("Phase 1: list-level pulls (enums, unit threads, transmission threads)")
    print(f"Generation units ({len(GENERATION_UNITS)}): {list(GENERATION_UNITS.values())}")
    print(f"OKG ({OKG_EIC}) deliberately excluded -- plan's live check found no UMM coverage 2018-2025")
    print()

    fetch_enums()
    fetch_unit_threads()
    fetch_transmission_threads()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s. Real HTTP requests this run: {request_count}")
    write_pull_summary({"phase": "phase1_lists", "elapsed_s": round(elapsed, 1)})
    print(f"Summary written to {DATA_S2 / 'pull_summary.json'}")


if __name__ == "__main__":
    main()
