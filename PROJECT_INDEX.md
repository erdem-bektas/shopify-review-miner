# Project Index: shopify-review-miner

Generated: 2026-07-07 (post radar/signal/robustness implementation)

Dependency-light toolkit: scrape Shopify App Store reviews into SQLite, browse
them in a serverless (sql.js/WASM) browser UI, and mine them for *unbundling*
opportunity signals. Runs as a **continuous radar**: delta refresh → auto
tagging via `claude -p` → snapshot diff. No scraped data ships; `sample.db` is
synthetic.

## 📁 Project Structure

```
scrape.py               scraper: full / --refresh / --check → reviews.db
tag_reviews.py          tag plumbing + --auto (claude -p) + --discover
opportunity_report.py   scoring, trend, --snapshot/--diff reports
export_md.py            markdown export
reviews.html            serverless UI (sql.js), Turkish labels
web/vendor/             sql-wasm.js / .wasm (vendored)
tests/                  test_parse / test_refresh / test_trend + fixtures/
docs/                   tagging-rubric.md, specs/*.md (design docs)
.githooks/              pre-commit + pre-push privacy guards
```

## 🚀 Entry Points (PEP 723, `uv run <script>.py`)

- **`scrape.py <slug|url>...`** — flags: `--ratings`, `--delay` (≥3!), `--db`, `--max-pages`, `--resume`, **`--refresh`** (delta: newest-first, stops after `--stop-after-known` (2) consecutive all-known pages; needs a completed baseline per (app, filter)), **`--check`** (live per-selector canary, no db writes). DOM-rot guards abort loudly (`plausible_listing_end`) instead of faking completion. Star counts parsed exactly from link `aria-label`s.
- **`tag_reviews.py`** — `--export/--import-batch/--stats` (manual loop); **`--auto`** (batches → `claude -p`, rubric file is the prompt, 1 retry then quarantine to `exports/quarantine/`, `--max-batches` 40, `--dry-run`, `--claude-args "--model …"`); **`--discover`** (weakly-tagged reviews → candidate new themes → `exports/theme-candidates.md`, report only).
- **`opportunity_report.py`** — ranked tables (with trend arrows), `--heatmap`, `--top N` (quotes + ⚑vendor-ack markers), `--trend` (yearly share series), `--json`, **`--snapshot`** (persist scores), **`--diff [OLD NEW]`** (new/risers/fallers/gone-quiet vs previous snapshot, markdown or `--json`).
- **`export_md.py`** — `--apps --ratings --out` → per-app markdown.
- **UI `reviews.html`** — browse tab + opportunities tab (heatmap with trend arrows, drill-down with vendor_ack ⚑ badges, churn flows).

## 🗄️ Data Model (SQLite, source-agnostic)

- **`reviews`** — PK `(source, app_slug, review_id)`; COALESCE upsert (None never overwrites).
- **`apps`** — PK `(source, app_slug)`; `product` groups cross-source via local `products.json`.
- **`scrape_progress`** — per `(app_slug, ratings_key)`; `--refresh` requires `completed=1` and never writes here.
- **`review_tags`** — theme (14 controlled), kind, churn_signal, switched_to, quote, confidence, **`vendor_ack`** (`none|acknowledged|roadmap|shipped|disputed`; pre-migration rows NULL, no backfill).
- **`snapshots` + `snapshot_scores`** — created by opportunity_report.py; feed `--diff`.
- All migrations idempotent (PRAGMA-checked ALTERs); old DBs and sample.db keep working.

## 🧮 Scoring & Signals

- `score = 100 × geomean(frequency, persistence, seniority, feature_shape)`; knobs in CONFIG (FREQ_SAT 0.20, DIFF_MIN_SCORE 25, DIFF_MIN_DELTA 10, TREND_RECENT_YEARS 2, TREND_RATIO 1.6, TREND_MIN_TAGS 4).
- **Trend** (`trend_of`): yearly share, recent-2-years vs earlier mean; growing/stable/fading/None. **Python is normative; `trendOf` in reviews.html mirrors it — change both together** (parity verified by tests + node check).
- **vendor_ack** is annotation-only (not a score factor yet).

## 🧪 Tests

`uv run tests/test_parse.py` (8: parsers vs synthetic fixtures) · `tests/test_refresh.py` (5: delta logic, fetch faked) · `tests/test_trend.py` (5: classification, normative for JS). Live canary: `scrape.py --check <slug>`.

## 📚 Docs

- `docs/tagging-rubric.md` — tagging SSOT incl. vendor_ack vocab (the `--auto` prompt).
- `docs/specs/radar-signal-robustness-{requirements,design}.md` — current iteration's spec.
- `.githooks/README.md` — enable via `git config core.hooksPath .githooks`; blocks private files/strings. `exports/` (incl. quarantine, theme-candidates) is denylisted.

## 📝 Radar cadence

```bash
uv run scrape.py <slugs> --refresh && uv run tag_reviews.py --auto -n 40 && \
uv run opportunity_report.py --snapshot --diff
```
Occasional plain full scrape picks up edits deep in the listing.
