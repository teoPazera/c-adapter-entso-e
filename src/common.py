"""Shared helpers for ENTSO-E pull scripts: redaction, frequency inference,
client construction, throttling. Extracted from s0_probe.py during S1 so
s1_pull_history.py can reuse them without duplicating code.

Run nothing directly -- this is a library module.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from entsoe import EntsoePandasClient

ROOT = Path(__file__).resolve().parents[1]

_TOKEN_RE = re.compile(r"(securityToken=)[^&\s]+", re.IGNORECASE)


def _redact(text: str) -> str:
    """entsoe-py embeds the API key in query-string URLs inside its own
    exception messages; strip it before anything touches a print or a file."""
    return _TOKEN_RE.sub(r"\1***REDACTED***", text)


def _redact_fields(fields: dict) -> dict:
    return {
        k: (_redact(v) if isinstance(v, str) else v)
        for k, v in fields.items()
    }


def _infer_freq(index: pd.Index) -> str:
    if len(index) < 2:
        return "n/a (fewer than 2 points)"
    diffs = pd.Series(index).diff().dropna().unique()
    if len(diffs) == 1:
        return str(pd.Timedelta(diffs[0]))
    return f"irregular: {[str(pd.Timedelta(d)) for d in diffs]}"


def _stringify_keys(d: dict) -> dict:
    return {str(k): v for k, v in d.items()}


class CountingSession(requests.Session):
    """requests.Session that counts every real HTTP GET it makes.

    Verified 2026-09-11 by reading entsoe/decorators.py and entsoe/entsoe.py:
    a single EntsoePandasClient method call can fan out into several real
    HTTP requests internally (year/month chunking via @year_limited /
    @month_limited, document-offset pagination via @documents_limited, range
    bisection via @paginated on PaginationError) -- so counting our own
    top-level calls understates the true request count. entsoe-py's
    EntsoeRawClient.__init__ accepts an injected requests.Session and stores
    every real call goes through self.session.get(...) in _base_request, so
    wrapping .get() here gives an exact count with no change to entsoe-py
    itself.
    """

    def __init__(self):
        super().__init__()
        self.n_requests = 0

    def get(self, *args, **kwargs):
        self.n_requests += 1
        return super().get(*args, **kwargs)


def make_client() -> EntsoePandasClient:
    """Load .env and build a live EntsoePandasClient with a request-counting
    session (read via client.session.n_requests). Raises RuntimeError if
    ENTSOE_E_API_KEY is missing."""
    load_dotenv()
    import os

    api_key = os.environ.get("ENTSOE_E_API_KEY")
    if not api_key:
        raise RuntimeError("ENTSOE_E_API_KEY not found in environment / .env")
    return EntsoePandasClient(api_key=api_key, session=CountingSession())


def throttled(fn, *args, sleep: float = 1.0, **kwargs):
    """Call fn(*args, **kwargs); sleep `sleep` seconds; on HTTP 429 back off
    30s and retry once. Any other exception propagates to the caller."""
    try:
        result = fn(*args, **kwargs)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status != 429:
            raise
        print("    429 rate-limited, backing off 30s and retrying once...")
        time.sleep(30.0)
        result = fn(*args, **kwargs)
    time.sleep(sleep)
    return result
