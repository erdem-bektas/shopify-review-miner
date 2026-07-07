# Design — Continuous Radar, Signal Quality, Robustness

**Date:** 2026-07-07
**Status:** Approved decisions from requirements review; implements
[`radar-signal-robustness-requirements.md`](radar-signal-robustness-requirements.md).
**Resolved:** LLM driver = `claude -p`; `vendor_ack` applies going forward only;
early-stop K = 2; snapshots live in the DB; trend buckets are yearly.

Design principles carried over: single-file PEP 723 scripts, no external API in
the core (`claude -p` is optional plumbing), backward-compatible migrations,
Python/JS scoring parity, politeness invariants (delay ≥ 3s, existing backoff).

---

## A1. Delta refresh — `scrape.py --refresh`

**Walk:** identical to a normal scrape (newest-first, same `--ratings`
semantics), always starting at page 1 and ignoring `scrape_progress.next_page`.

**Early stop:** per page, collect parsed `review_id`s and run one existence
query (`SELECT review_id FROM reviews WHERE source=? AND app_slug=? AND
review_id IN (...)`). A page where *every* id is known increments a counter;
any unknown id resets it. Stop after **K = 2** consecutive all-known pages
(`--stop-after-known`, default 2). Pages scanned before the stop are still
upserted, so recent edits are captured (the existing COALESCE upsert already
handles content updates; `edited=MAX(...)` is preserved).

**Soundness precondition:** early-stop is only valid when the DB already
contains the population being walked. Refresh therefore requires a completed
baseline for that `(app_slug, ratings_key)` in `scrape_progress`; if absent or
incomplete, print a warning and fall back to a full scrape for that app.

**Change summary (A1.3):** before each page's upsert, fetch existing rows for
the page's ids and diff `(rating, review_date, body, dev_reply, edited)` in
Python. Per app print: `refresh: +N new, ~M updated, P pages fetched`.

**Metadata:** the unfiltered page-1 fetch (already present) refreshes the
`apps` row every run.

**Backstop for missed edits:** none automated. A plain run (no `--refresh`)
remains the full re-scrape; README will recommend an occasional full pass.

**Flag interactions:** `--refresh` and `--resume` are mutually exclusive
(argparse error) — resume finishes an interrupted crawl; refresh assumes a
finished one.

## A2. Auto-tagging — `tag_reviews.py --auto`

**Loop:**

```
skip = set()                     # quarantined this run
while batches < --max-batches:
    rows = fetch_untagged(...) minus skip     # respects --app
    if not rows: break
    batch = rows[:n]                          # -n, recommended 25–50 for --auto
    prompt = RUBRIC_TEXT + OUTPUT_CONTRACT + json(batch)
    out = subprocess.run(["claude", "-p", *shlex.split(--claude-args)],
                         input=prompt, capture_output=True, text=True,
                         timeout=900)
    tags = parse_and_validate(out.stdout)     # strip code fences → json.loads
    on failure: retry ONCE with validation errors appended
    on second failure: quarantine batch+response, skip |= batch ids, continue
    import tags (same code path as --import-batch)
    print(f"batch {i}: tagged {len(batch)}, {remaining} untagged left")
```

- **Rubric is the prompt:** `docs/tagging-rubric.md` read from the repo at
  runtime — no duplicated instructions in code. `OUTPUT_CONTRACT` is a short
  fixed footer: "return ONLY a JSON array of tag objects, schema: …".
- **Validation refactor:** extract the controlled-vocab checks from
  `cmd_import` into `validate_tags(tags) -> (ok, errors)` used by both paths.
- **Quarantine:** `exports/quarantine/batch-<seq>.json` + `.response.txt`
  (`exports/` is already git-ignored and hook-denylisted). Quarantined ids are
  skipped in-memory for the rest of the run so the loop cannot spin on a
  poisoned head-of-queue batch.
- **Guardrails:** `--max-batches` (default 40), `--dry-run` (print the first
  prompt and batch shape, invoke nothing), 900s subprocess timeout,
  `--claude-args` passthrough (e.g. `--model haiku`).
- **Resumability:** free — untagged queries exclude tagged rows; import is
  idempotent per review.

**Acceptance check:** `--auto` then `--stats` shows 100% tagged (minus
quarantined, which are listed on exit).

## A3. Snapshots & diff — `opportunity_report.py --snapshot / --diff`

**Schema** (created by `opportunity_report.py`, idempotent, in `reviews.db`):

```sql
CREATE TABLE IF NOT EXISTS snapshots (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL                      -- ISO-8601 UTC
);
CREATE TABLE IF NOT EXISTS snapshot_scores (
    snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id),
    app_slug     TEXT NOT NULL,
    theme        TEXT NOT NULL,
    score        REAL NOT NULL,
    frequency    REAL, persistence REAL, seniority REAL, feature_shape REAL,
    feat_reviews INTEGER, tagged_total INTEGER,
    PRIMARY KEY (snapshot_id, app_slug, theme)
);
```

- `--snapshot` — compute scores exactly as today, insert one snapshot.
- `--diff [OLD [NEW]]` — default: latest two snapshots. Sections, markdown to
  stdout (`--json` for machine output):
  - **New** — present now at score ≥ `DIFF_MIN_SCORE`, absent (or below) before.
  - **Risers / fallers** — |Δscore| ≥ `DIFF_MIN_DELTA`, sorted by Δ.
  - **Gone quiet** — was ≥ `DIFF_MIN_SCORE`, now below.
  - New/riser entries include top quotes (reuse `print_top`'s selection).
- New CONFIG constants: `DIFF_MIN_SCORE = 25.0`, `DIFF_MIN_DELTA = 10.0`
  (scores are on the 0–100 scale; an earlier draft wrote these as 0–1).
- Snapshots are report-side only — no UI/JS work, no parity impact.

**Radar cadence** (documented in README, not automated):
`scrape.py --refresh … && tag_reviews.py --auto && opportunity_report.py --snapshot --diff`.

## B1. Vendor acknowledgment — `vendor_ack`

- **Migration** (in `tag_reviews.py ensure_schema`, idempotent via
  `PRAGMA table_info`): `ALTER TABLE review_tags ADD COLUMN vendor_ack TEXT`.
  Existing rows stay `NULL` = "tagged before this field existed" (per decision:
  no backfill).
- **Vocab:** `none` (no reply, or reply ignores the point) · `acknowledged`
  (admits limitation, no commitment) · `roadmap` (commits/planned) · `shipped`
  (says it now exists) · `disputed` (claims user error / feature exists).
- **Rubric:** new section in `docs/tagging-rubric.md` defining the five values
  with examples; export already carries `dev_reply`, so no export change.
- **Import:** accept the field when valid, else `NULL`; validation added to
  `validate_tags`.
- **Scoring: annotation only, no formula change** (keeps JS parity untouched).
  Surfaces as: ack distribution in `--stats`; `⚑ roadmap`/`⚑ shipped` markers
  next to quotes in `--top`; a `vendor_ack` line per tag in the UI drill-down.
  `shipped`/`disputed` markers are the staleness warning — a future score
  factor can be revisited once enough tagged data carries the field.

## B2. Theme trend — yearly, Python/JS identical

**Algorithm** (the normative definition — both implementations follow this):

```
for (app, theme):
  for each calendar year y in the app's tagged data:
    share[y] = feature-kind tags of theme in y / distinct tagged reviews of app in y
  current_year = year(newest review in db)
  recent = mean(share of last TREND_RECENT_YEARS years ending current_year)
  base   = mean(share of all earlier years that have ≥1 tagged review)
  insufficient (→ no arrow) if: theme's total feature tags < TREND_MIN_TAGS
                                or app has < 3 distinct years of tagged data
  growing if recent > 0 and (base == 0 or recent ≥ TREND_RATIO × base)
  fading  if recent ≤ base / TREND_RATIO
  else stable
```

- Constants (CONFIG + mirrored in the JS config block): `TREND_RECENT_YEARS = 2`,
  `TREND_RATIO = 1.6`, `TREND_MIN_TAGS = 4`.
- **Report:** trend arrow (`↑ → ↓`) as a column in the ranked table; `--trend`
  prints the yearly share series per theme×app.
- **UI:** arrow appended in heatmap cells; drill-down shows the series.
- **Parity check:** `--json` output includes the trend label; comparing it
  against the UI for the same DB is the documented manual parity check.

## B3. Emergent theme discovery — `tag_reviews.py --discover`

- **Input:** reviews whose tags are *all* `theme='other'` or *all*
  `confidence='low'` (SQL over `review_tags` join `reviews`).
- **Pass 1:** batches of ~60 (body + existing quotes) → `claude -p`:
  "propose ≤5 candidate themes NOT in <THEMES>, each with a one-line definition
  and 2–3 verbatim example quotes."
- **Pass 2:** one final `claude -p` call merges/dedupes candidates across
  batches.
- **Output:** `exports/theme-candidates.md` (git-ignored) — candidate name,
  definition, evidence quotes, rough count. **Never touches the DB or THEMES;**
  adopting a candidate is a human edit to the rubric + `THEMES` set.
- Reuses `--auto`'s subprocess/validation plumbing and guardrails.

## C1. Parser fixture tests — `tests/`

- `tests/test_parse.py`: PEP 723 script (`dependencies = ["pytest", "requests",
  "beautifulsoup4"]`) ending in `sys.exit(pytest.main([__file__]))` so it runs
  as `uv run tests/test_parse.py`. Imports `scrape.py` via repo-root
  `sys.path` insertion.
- **Fixtures** (`tests/fixtures/`), hand-authored from the selector inventory in
  `review-scraper-design.md`, fully synthetic (slug `example-app`, shops like
  "Demo Shop" — must pass the privacy hooks):
  - `reviews_page.html` — 3 reviews: plain; edited + dev reply; minimal (no
    country/usage). Includes a star-distribution block.
  - `empty_page.html` — the genuine "no reviews" empty state.
- **Assertions:** review count and `n_blocks`; exact field values (id, rating,
  date, shop, country, usage_duration, body, dev_reply, edited flag);
  `dev_reply is None` when the reply div is empty; star distribution
  `{5:…,4:…,3:…,2:…,1:…}`; empty page → `([], 0)`.
- One-time operator step at implementation: diff fixture structure against a
  live page to confirm the fixtures aren't already stale.

## C2. Live canary & zero-review guard — `scrape.py`

**`--check`** (no DB writes): for each app argument, fetch unfiltered page 1
and print a per-selector ✓/✗ table:

| check | rule |
|---|---|
| title → app_name | non-empty after cleanup |
| star distribution | keys 1..5 present |
| review blocks | ≥ 1 when distribution total > 0 |
| per review: id / rating / date / body | id non-empty; 1 ≤ rating ≤ 5; date parses; body non-empty |

Exit non-zero naming every failed check. Respects `--delay` between apps.

**Zero-review guard in `scrape_app`:** today `n_blocks == 0` is treated as the
natural end of the listing — which is exactly how DOM rot becomes a silent
"completed".

*Implementation note (2026-07-07):* the draft proposed detecting a reviews-list
container element, but live-page inspection found no reliable one (the page
chrome, including `#ReviewsIndex`, renders identically on beyond-end and even
404 pages). Implemented instead as a selector-free **count-plausibility rule**
(`plausible_listing_end`): a 0-block page at page P is believable as the end
only when `(P−1)×10 ≥ 0.7 × expected`; the exact star counts now parsed from
the filter links' `aria-label`s make `expected` tight. Below that, or when a
page has blocks but none parse, the app aborts loudly
(`selectors likely broke — run --check`) and `scrape_progress` is **not**
marked completed. Page 1 with 0 blocks while the distribution claims reviews
always aborts.

---

## Cross-cutting

**File impact**

| file | changes |
|---|---|
| `scrape.py` | `--refresh` (+`--stop-after-known`), `--check`, zero-review guard, container detection |
| `tag_reviews.py` | `vendor_ack` migration + validation refactor, `--auto` (+`--max-batches`, `--dry-run`, `--claude-args`), `--discover` |
| `opportunity_report.py` | snapshot tables, `--snapshot`, `--diff`, trend computation, new CONFIG constants |
| `reviews.html` | `vendor_ack` in drill-down; trend arrows + JS config mirror |
| `docs/tagging-rubric.md` | `vendor_ack` section |
| `tests/` | new: `test_parse.py`, `fixtures/` |
| `README.md` | refresh/auto/snapshot/check usage; radar cadence; full-pass recommendation |

`.gitignore` and `.githooks` need no changes — all new private artifacts
(quarantine, theme candidates) live under the already-denylisted `exports/`.

**Implementation order** (each step independently shippable):

1. **C1** fixtures + tests — safety net before touching the parser.
2. **C2** `--check` + zero-review guard (touches `parse_reviews`, now covered).
3. **A1** `--refresh`.
4. **B1** rubric + schema (`vendor_ack`) — before the driver, so auto-tagging
   emits it from day one.
5. **A2** `--auto`.
6. **A3** snapshots + diff.
7. **B2** trend (Python, then JS mirror).
8. **B3** `--discover`.
