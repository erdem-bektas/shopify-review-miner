# shopify-review-miner

A tiny, dependency-light toolkit for reading Shopify App Store reviews *at scale*
and mining them for **opportunity signals** — the feature-shaped gaps and wishes
hiding in an app's reviews. Data lives in SQLite; the UI runs entirely in your
browser (no server, no Python) via [sql.js](https://sql.js.org) (WASM).

> **What it is:** a scraper + a serverless review browser + an opportunity-scoring
> layer, for product/competitive research on *public* review pages.
> **What it is not:** a bulk dataset. No scraped data ships here — you point it at
> the apps you care about and build your own `reviews.db`. Please read
> [Responsible use](#responsible-use) first.

A synthetic `sample.db` is included so you can try the UI immediately without
scraping anything.

## Requirements

- [uv](https://docs.astral.sh/uv/) — scripts declare their own deps inline, so
  there is nothing to `pip install`. Just `uv run <script>.py`.
- A modern browser for the UI. That's it.

## Try it in 30 seconds (no scraping)

1. Open `reviews.html` in your browser (double-click it).
2. When prompted, choose the included **`sample.db`**.
3. Browse reviews, filter by stars/product, and open the **Opportunities** tab
   for the theme × app heatmap. All data in `sample.db` is invented for the demo.

## Scrape your own

```bash
# All reviews for one app (slug = apps.shopify.com/<slug>) — all star ratings
uv run scrape.py some-app-slug

# Several apps at once
uv run scrape.py app-one app-two app-three

# Only certain star ratings
uv run scrape.py some-app-slug --ratings 1,2,3

# A full URL works too
uv run scrape.py https://apps.shopify.com/some-app-slug

# Resume an interrupted run — progress is tracked per (app, ratings-filter)
uv run scrape.py some-app-slug --resume

# Weekly refresh: walks newest-first, stops after 2 consecutive already-known
# pages (needs one completed full scrape for the same filter first)
uv run scrape.py some-app-slug --refresh

# Did Shopify change their DOM? Live one-page selector check, no db writes
uv run scrape.py some-app-slug --check
```

Data is written to `reviews.db` (SQLite, git-ignored). Re-runs are safe and
incremental — reviews upsert by `(source, app_slug, review_id)`, so nothing is
duplicated. Large apps can take a while; run in the background with `--resume`.

Optional: to show a friendly product name (and group the same product across
sources), drop a `products.json` next to the scripts mapping slugs to names — see
[`products.example.json`](products.example.json). It's git-ignored, so your own
list stays local.

## Read & filter — the serverless UI

Open `reviews.html` and load your `reviews.db`. Everything runs in the browser;
SQLite is queried client-side with sql.js (WASM). No server, no Python.

- **Source** menu — currently Shopify App Store; the schema is source-agnostic, so
  other sources (e.g. G2, Trustpilot) can be added under the same tables.
- **Product** chips — group a product's reviews across sources with one click.
- **Star** multi-select with live counts, **date/rating** sort, **text search**,
  and collapsible developer replies.
- Hit **⟳ DB** to reload the file while a scrape is running.

Adding a new source: have its scraper write to the `reviews` table with its own
`source` value and fill `product` in the `apps` table — the UI picks it up
automatically.

## Opportunity mining — tag, score, heatmap

To go from raw reviews to an *unbundling* opportunity list: tag reviews by
theme/kind, score each theme × app, and explore a heatmap with drill-down in the
**Opportunities** tab. Core principle: **not every complaint is an opportunity** —
"support is slow" / "too expensive" don't become products; *feature-shaped* gaps
("can't do X in the flow editor", "love it, *but I wish*…") do. The full rubric is
in [`docs/tagging-rubric.md`](docs/tagging-rubric.md).

### Tagging

Tagging is done by an LLM you drive (no external API baked in); rules live in the
rubric. The schema is backward-compatible: it adds a `review_tags` table and
leaves the existing UI untouched.

```bash
# export untagged reviews (sequential batch or a stratified sample)
uv run tag_reviews.py --export --app some-app-slug -n 200 --out batch.json
uv run tag_reviews.py --export --app some-app-slug --stratified -n 100 --seed 42 --out sample.json

# an LLM tags the batch per the rubric -> batch.tags.json; then import it
uv run tag_reviews.py --import-batch batch.tags.json    # idempotent, resumable

# progress + theme × kind distribution
uv run tag_reviews.py --stats --app some-app-slug

# or hands-off: export → `claude -p` (the rubric IS the prompt) → validated
# import, looping until everything is tagged; failed batches are quarantined
# to exports/quarantine/, never silently dropped
uv run tag_reviews.py --auto -n 40 --claude-args "--model claude-haiku-4-5"
```

`review_tags` fields: `theme` (14 controlled themes), `kind` (`feature_gap`,
`feature_request`, `service`, `pricing`, `bug`, `praise`), `churn_signal`,
`switched_to`, `quote` (≤200-char evidence quote), `confidence`, `vendor_ack`
(what the dev reply admits: `none/acknowledged/roadmap/shipped/disputed` —
a years-old `roadmap` promise is gold, `shipped` is a staleness warning). A
review can be multi-theme → multiple rows.

`tag_reviews.py --discover` mines weakly-tagged reviews (all-`other` or
all-low-confidence) for candidate themes the rubric might be missing →
`exports/theme-candidates.md`. Report only; adopting a candidate is a manual
rubric edit.

### Opportunity score

```bash
uv run opportunity_report.py --app some-app-slug   # theme table
uv run opportunity_report.py --heatmap             # theme × app grid
uv run opportunity_report.py --top 3               # highest + quotes
```

    score = frequency × persistence × seniority × feature-shapedness   (weighted geometric mean)

- **frequency** — feature_gap+feature_request reviews in a theme / the app's total tagged reviews
- **persistence** — first→last seen span × still-present in the last 12 months (present in 2019 *and* 2026 → strongest signal)
- **seniority** — average tenure of the complainers (years-long users complaining > week-one churn)
- **feature-shapedness** — themes high in feature share and low in service/pricing rise; a pure support/pricing theme falls to 0

Weights and thresholds live in the `CONFIG` block at the top of
`opportunity_report.py`. The UI heatmap applies the exact same formula in JS
(still serverless).

Tables and heatmap cells carry a **trend arrow** (↑ growing · → stable ·
↓ fading — yearly share of the theme, last 2 years vs. earlier);
`--trend` prints the series behind the arrows. Quotes in `--top` carry
`⚑ roadmap`-style markers when the dev reply acknowledged the gap.

## The radar loop (continuous mode)

After one full scrape + tagging pass, a periodic one-liner keeps the
opportunity list fresh:

```bash
uv run scrape.py app-one app-two --refresh && \
uv run tag_reviews.py --auto -n 40 && \
uv run opportunity_report.py --snapshot --diff
```

- `--refresh` costs a couple of pages per unchanged app instead of a re-crawl.
- `--auto` tags only what's new; interrupting it loses nothing.
- `--snapshot --diff` reports new opportunities, risers/fallers and gone-quiet
  themes against the previous snapshot (`--diff --json` for machines).
- Occasionally run a plain full scrape (no `--refresh`) to pick up edits deep
  in the listing, and `scrape.py --check` when a scrape looks off.

## Tests

```bash
uv run tests/test_parse.py     # parsers vs synthetic DOM fixtures
uv run tests/test_refresh.py   # delta-refresh logic (no network)
uv run tests/test_trend.py     # trend classification (JS mirror matches)
```

Fixtures can't tell you Shopify changed their DOM — `scrape.py --check <slug>`
is the live canary for that, and the scraper itself now aborts loudly instead
of mistaking selector rot for the end of a listing.

## Alternative: Datasette

```bash
uvx datasette reviews.db
```

For ad-hoc SQL. Example:

```sql
SELECT review_date, rating, shop_name, body
FROM reviews
WHERE rating <= 3 AND body LIKE '%price%'
ORDER BY review_date DESC;
```

## Markdown export (optional)

```bash
uv run export_md.py --ratings 1,2,3     # exports/<app-slug>.md, grouped by star
```

## Responsible use

This tool reads **public, un-authenticated** review pages — no Shopify account or
login is involved. Even so, use it responsibly:

- **Respect rate limits.** Shopify's edge starts resetting connections after
  ~15–20 rapid requests. The scraper handles this itself with long cool-downs
  (15s→240s); don't drop `--delay` below 3. If interrupted, `--resume`.
- **Respect robots.txt and Terms of Service.** These can change — check them for
  your target before running, and stop if they disallow it.
- **Don't redistribute bulk scraped data.** Reviews are user-generated content
  owned by their authors. This repo intentionally ships **no** scraped reviews —
  only the tools and a synthetic `sample.db`. Keep your `reviews.db` and any
  exports private (they're git-ignored by default).
- Use this for research and product discovery, not for spamming, scraping
  personal data, or anything a review author wouldn't expect.

This repo also ships git hooks (`.githooks/`) that refuse to commit or push
private files (your `reviews.db`, `products.json`, `exports/`, personal notes) —
a safety net against an accidental `git add -A`. Turn them on once per clone:
`git config core.hooksPath .githooks`.

## Notes / lessons learned

- Review pages are fully server-rendered; plain HTTP is enough — no headless
  browser needed.
- Star-distribution counts come from the filter links' `aria-label`s, which
  carry exact totals; the visible "2.6K"-style text is only the fallback.

## License

[MIT](LICENSE) © 2026 Erdem Bektaş
