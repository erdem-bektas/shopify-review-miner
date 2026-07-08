# Requirements — Continuous Radar, Signal Quality, Robustness

**Date:** 2026-07-07
**Status:** Accepted — decisions resolved in
[`radar-signal-robustness-design.md`](radar-signal-robustness-design.md).
**Scope note:** Market-sizing axis (pricing/volume metadata) explicitly deferred to a later iteration.

## Goal

Evolve the pipeline from *one-shot research* ("scrape once, tag by hand, look")
into a *continuously refreshed opportunity radar* whose signals can be trusted:
cheap re-scrapes, hands-off tagging, vendor-acknowledgment and trend signals,
and guardrails against silent breakage (DOM rot).

Invariants that hold across everything below:

- Politeness unchanged: delay ≥ 3s, existing backoff, in every new mode.
- No external API baked into the core; any LLM driver is optional and pluggable.
- DB migrations backward-compatible; `sample.db` keeps working in the UI.
- Python/JS scoring parity is a stated requirement for every scoring change.
- Everything committed (fixtures, reports) must pass the repo's own privacy hooks:
  synthetic app slugs/names only.

---

## Workstream A — Continuous radar

*Story: as a researcher, I re-run one command weekly and read a short diff
telling me which opportunities rose, fell, or appeared — without babysitting
scrapes or tagging batches.*

### A1. Delta refresh (scrape.py)

- **A1.1** A refresh mode that, per (app, ratings-filter), pages newest-first and
  stops early once pages contain only already-known `review_id`s (stop rule:
  K consecutive all-known pages; K configurable, default small).
- **A1.2** Refresh also updates the `apps` star-distribution/metadata row.
- **A1.3** Refresh prints a change summary per app: N new, M updated reviews.
- **A1.4** Upsert semantics preserved — edited known reviews still update when
  encountered; the early-stop rule must not be so aggressive that new reviews
  interleaved by sorting quirks are missed.
- **Acceptance:** refreshing an unchanged app costs ≤ K+1 page requests per
  (app, filter); reviews posted since the last run are always captured.

### A2. Auto-tagging driver (tag_reviews.py or companion script)

- **A2.1** Optional `--auto` mode: export batch → invoke a local LLM CLI with the
  rubric → validate returned JSON → import; loop until no untagged reviews
  remain. Core export/import flow unchanged for manual use.
- **A2.2** Resumable; invalid/failed batches are quarantined to files and
  reported, never silently dropped. Validation reuses the same controlled-vocab
  rules as `--import-batch`.
- **A2.3** Guardrails: max reviews per LLM invocation, `--dry-run`, and a hard
  batch-count cap per run.
- **Acceptance:** from a fresh scrape, one command reaches 100% tagged;
  interrupting mid-run loses no work.

### A3. Radar diff report (opportunity_report.py or companion)

- **A3.1** Compare current scores against a stored snapshot of the previous run:
  new themes crossing a score threshold, biggest risers/fallers per theme×app,
  themes gone quiet. Output markdown + JSON.
- **A3.2** Snapshot storage mechanism is a design decision (in-DB table vs JSON
  file), but snapshots must be cheap and diffable.
- **Acceptance:** given two runs over changed data, the report lists risers and
  fallers with supporting quotes.

---

## Workstream B — Signal quality

*Story: as a researcher, when the radar surfaces a theme I can see whether the
vendor has acknowledged the gap, whether the complaint is growing or fading,
and whether the rubric is missing an emergent theme.*

### B1. Vendor acknowledgment (dev-reply mining)

- **B1.1** Rubric + `review_tags` gain a `vendor_ack` field per tag, derived from
  `dev_reply` during tagging. Controlled vocab (proposal):
  `none | acknowledged | roadmap | shipped | disputed`.
- **B1.2** Scoring: "acknowledged/roadmap & unshipped" strengthens a theme;
  `shipped` flags possible staleness (gap may be closed). Whether this is a
  score factor or an annotation is a design decision — parity requirement applies.
- **B1.3** UI drill-down displays `vendor_ack`.
- **B1.4** Migration idempotent; existing rows default to `none`.

### B2. Theme trend

- **B2.1** Per theme×app, a time-bucketed frequency series with a trend
  classification (growing / stable / fading).
- **B2.2** Heatmap cells show a trend indicator; report gains a trend view.
  Python and JS must classify identically.

### B3. Emergent theme discovery

- **B3.1** A periodic pass over weakly-tagged reviews (`other` / low-confidence)
  that clusters them and proposes candidate themes with example quotes.
- **B3.2** Output is a report for human rubric review — candidates are **never**
  auto-added to the controlled vocabulary.
- Clustering method (LLM pass vs embeddings) is a design decision.

---

## Workstream C — Robustness

*Story: as the operator, when Shopify changes its DOM I find out from a failing
test or a loud `--check` error — not from a quietly empty scrape.*

### C1. Parser fixture tests

- **C1.1** Sanitized HTML fixtures (synthetic slugs, invented shop names — must
  pass the privacy hooks) covering: normal review, edited review, dev reply
  present/absent, star-distribution block, empty page.
- **C1.2** Tests cover `parse_reviews` and `parse_star_distribution`, runnable
  via `uv run` (PEP 723 inline pytest is acceptable).
- **Acceptance:** a selector change breaks a test, not the data.

### C2. Live canary

- **C2.1** `scrape.py --check <slug>`: fetch one page, assert every selector
  yields a plausible value, exit non-zero naming the broken selector.
- **C2.2** `scrape_app` warns loudly when a page yields 0 reviews for an app
  whose metadata claims reviews exist (instead of proceeding silently).

---

## Open questions — all resolved (2026-07-07)

Decisions recorded in [`radar-signal-robustness-design.md`](radar-signal-robustness-design.md):

1. **Edited reviews / early-stop:** K = 2 consecutive all-known pages
   (`--stop-after-known`); occasional manual full pass documented as backstop,
   not automated.
2. **LLM driver:** `claude -p` (user decision).
3. **vendor_ack backfill:** going forward only; pre-existing rows stay `NULL`
   (user decision).
4. **Snapshot storage:** in-DB tables (`snapshots`, `snapshot_scores`).
5. **Trend buckets:** yearly.
