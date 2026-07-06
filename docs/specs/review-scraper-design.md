# Shopify App Store Review Scraper — Design

**Date:** 2026-07-04
**Status:** Approved; stated needs — MD output sufficient, filterable UI nice-to-have.

## Purpose

Pull all (or rating-filtered) reviews of chosen Shopify App Store apps (e.g. a large marketing app) for
competitor/complaint research, without clicking through pages manually. Read and filter them
locally by star rating, app, and date.

## Feasibility findings (verified 2026-07-04)

- `apps.shopify.com/<slug>/reviews` is fully server-rendered; plain HTTP + browser UA returns 200,
  no bot challenge. No headless browser needed.
- Star filter is a query param: `?ratings[]=2` (repeatable). Pagination: `&page=N`, 10 reviews/page.
  `sort_by=newest` gives deterministic ordering across pages.
- `robots.txt` does not disallow review pages. Public, unauthenticated pages; no link to the user's
  Shopify account → worst case is a temporary IP rate-limit, not any account action.
- Review block structure (per `div#review-{id}`):
  - rating: `div[role=img]` `aria-label="N out of 5 stars"`
  - date: `div.tw-text-body-xs.tw-text-fg-tertiary` (e.g. "April 22, 2026")
  - body: `[data-truncate-content-copy]` paragraphs
  - shop: `span[title]` inside the heading column; country and "X using the app" as sibling divs
  - developer reply: `[data-merchant-review-reply]` (empty div when no reply)

## Architecture

Two small units, one data store:

1. **`scrape.py`** — single-file Python script, PEP 723 inline deps (`requests`, `beautifulsoup4`),
   run with `uv run scrape.py`. Input: app slugs or full app-store URLs. Flags: `--ratings 1,2,3`
   (default: all), `--delay` (default 1.5s), `--db` (default `reviews.db`), `--max-pages` safety cap.
   Fetches pages politely (single session, browser UA, delay + jitter, retry with backoff on
   429/5xx), parses review blocks, **upserts** by `(app_slug, review_id)` so re-runs are
   incremental and resumable.
2. **`export_md.py`** — reads SQLite, writes `exports/<app>.md` grouped by star rating (ascending,
   complaints first), with date/shop/country/usage metadata and developer replies quoted.

**Store:** SQLite `reviews.db`, table `reviews(app_slug, review_id PK-ish, rating, review_date,
shop_name, country, usage_duration, body, dev_reply, scraped_at)` + `apps` table with per-star
counts snapshot. Unique index on `(app_slug, review_id)`.

**Filterable UI:** no CMS to maintain — `uvx datasette reviews.db` gives a local web UI with
faceted filtering (rating, app, country), full-text search, and SQL. If a real headless CMS is
wanted later, the SQLite data imports trivially.

## Alternatives considered

- **MD only:** simplest, but no filtering; kept as an export, not the store.
- **Obsidian + frontmatter/Bases:** zero new tools, but thousands of small files get unwieldy.
- **Headless CMS (Directus/PocketBase):** richest UX (tags, read/unread), but a service to run —
  overkill for a read-and-analyze workflow. Deferred; SQLite is the migration-friendly base.

## Error handling

- 429/503 → exponential backoff (respect `Retry-After`), max 5 tries, then abort with clear message;
  partial progress is already committed to SQLite.
- Parse misses (HTML drift) → warn with page URL + review id, skip field, never crash the run.
- Count check: after a run, compare scraped count per rating with the page's own distribution
  numbers and report drift.

## Testing

End-to-end against a live app's data: scrape ratings 1–3 (~308 reviews, ~31 requests), verify
counts match the page's distribution (244/35/29 at design time), spot-check fields, verify a
developer-reply case parses.
