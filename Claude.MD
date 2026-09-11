# ENTSO-E context adapter — simple benchmark (Sweden)

## What this tests

Does an LLM that reads real documents (grid outage announcements, market
messages) improve a frozen statistical forecasting model, by adjusting the
model's future driver inputs instead of retraining the model itself? This is
a small, self-contained proof of concept on public Swedish electricity market
data. If it works here, the same idea gets tried on a different, private
forecasting setup next — you don't need that context, just this benchmark.

## The mechanism, in short

- No-context baseline: `X_t =(f)> y_t` — f trained on historical driver and
  target values, forecasts forward from the last known state.
- Context adapter: `C_t => X_t+ =(f)> y_t+` — the same frozen f, but the
  future slot of one driver (available generation/transmission capacity) is
  filled with a distribution derived from reading documents active at
  forecast time, instead of a naive extrapolation.
- f itself never changes between the two conditions. Only what fills the
  future-driver slot changes. Don't refit f per condition.

## Region

Sweden — bidding zones SE1–SE4, via the ENTSO-E Transparency Platform API
and Nord Pool's REMIT/UMM messages. Picked for two reasons: a mixed
generation fleet (nuclear, hydro, wind) gives enough varied outage events,
and Nordic market messages are conventionally reported in English.

## Ground rules

- Work one stage at a time. Finish a stage, report what you found, then
  stop and wait. Don't move to the next stage on your own.
- If you're unsure which specific series, date range, zone code, or event
  to use, ask. Don't guess and proceed.
- Start small: one zone, a short date range, a handful of events. Scale up
  only once a stage is approved.
- Before any API pull that might be large or slow, say how many requests
  and roughly how much data it involves.
- Rate limit is 400 requests/minute per token — a full historical pull for
  one zone is on the order of tens of requests, so this shouldn't bind, but
  throttle anyway rather than assume.

## Stages

### S0 — Confirm the data exists and is usable
Pull one sample of each: day-ahead price, day-ahead load forecast,
day-ahead wind/solar forecast, one outage record, one REMIT/UMM message.
Confirm fields are present, message text is actually in English, and time
ranges line up the way expected.
**Report back:** one real example of each, and whether the English
assumption holds.

### S1 — Pull the full numerical history
Target: Swedish day-ahead price (pick one zone, e.g. SE3, to start).
Drivers: day-ahead load forecast, day-ahead wind/solar forecast, available
generation capacity (installed minus declared unavailable), day-ahead NTC
on the main interconnectors. Roughly 2023-01 to present, hourly. Watch for
a mid-window switch from hourly to 15-minute settlement — check before
committing the window.
**Report back:** any gaps, frequency changes, or missing drivers found.

### S2 — Curate a small set of context events
About 15–20 outage/capacity events, spread across different seasons and
driver types: some short generation or transmission outages (effect should
end cleanly when the document says it ends), and a few in periods where a
hydro reservoir effect is plausible (effect may outlast the document). Each
event needs: start/end, zone, and at least one matching document.
**Report back:** the event list, before it's used in anything downstream.

### S3 — Build and backtest the baseline model (no context)
Pick one baseline to start (LEAR or SARIMAX). Walk-forward backtest over
the pulled history, recalibrating on a rolling window (try 1 and 2 years,
pick via a validation slice — don't fix this in advance). Report MAE and
rMAE against a naive persistence baseline, plus CRPS via pinball loss over
a quantile grid.
**Report back:** whether the baseline clears naive at all. This is the
"how good are the models" check, before anything else is built on it.

### S4 — Add the context adapter
For each curated event: have the model read the document(s) actually
available at that point in time (correct revision, not the final one), and
produce a distribution over the availability driver's effect — direction,
start, duration, size — not a single number. Run the same frozen f under
several sampled paths and average the resulting forecast distributions.
**Report back:** one worked example end to end, before running it across
all events.

### S5 — Compare and visualize
For the curated events: no-context vs. context-adapted vs. oracle (using
the realized outage path). Same metrics as S3, plus a Diebold-Mariano test
on the paired differences where the sample allows. One chart with all three
conditions side by side.
**Report back:** the chart, and which condition wins, honestly — including
if the result isn't statistically significant at this sample size.