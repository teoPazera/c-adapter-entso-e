# S4 — Minimal context adapter

## Purpose

The S4 prototype keeps the S3 LEAR model frozen. It asks an LLM to read the
UMM revisions that were public at a delivery day's forecast origin, infer a
small distribution over a single availability-driver effect, samples five
future driver paths, and averages five frozen-model forecasts.

## Smallest pipeline

1. **Select context at t0.** For delivery day `D`, set `t0 = D-1 12:00`
   Europe/Stockholm. Load only event-document revisions whose
   `publicationDate <= t0`.
2. **One structured LLM call.** The prompt explains the target (24 hourly SE3
   prices), frozen S3 inputs, the single driver that can change, and the
   time-safe UMM context. The model returns strict JSON:
   `reasoning`, `p_active`, start/end-hour mean+sd, `size_mw` p10/p50/p90,
   and `direction`.
3. **Validate/cache.** Validate key set, ranges, and ordered size quantiles;
   cache by SHA-256 of prompt plus schema under `data/s4/llm_cache/`.
4. **Five paths.** Start from S3's persistence fill. For each path, sample
   active/not-active, time interval, and MW size; apply the effect only to
   the day-D driver block.
5. **Frozen forecast comparison.** Feed each path through the existing
   `predict_day(..., exog_override=...)`; average points for `context`.
   Also write `no_context` and `oracle` predictions for the same day.

No retrieval service, agent loop, model retraining, or extra LLM call is
used in the first prototype. Documents are short enough to pass directly;
a reduction call is intentionally deferred until an event exceeds the
context budget.

## Provider

`src/llm_client.py` uses LangChain `ChatOpenAI` with:

- model: `gpt-5.4-nano-20260317-global`
- API key: `LLM_API_KEY` from `.env`
- endpoint: configured LiteLLM base URL
- output: strict `json_schema`

Run the worked example:

```bash
uv run python src/s4_run.py --event G074 --day 2025-02-06 --provider openai --paths 5
```

Use `--provider mock` for a no-cost wiring check. Repeated real calls reuse
cache unless `--force-llm` is passed.

## G074 correction

At `D=2025-02-06`, t0 is **2025-02-05 12:00 Europe/Stockholm**. G074 revision
2 was published at 08:08 UTC that day, so revision **2**—not revision 1—is
the latest permitted document under the stated t0 rule.
