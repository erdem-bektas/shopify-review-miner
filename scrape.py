# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "beautifulsoup4"]
# ///
"""Scrape Shopify App Store reviews into SQLite.

Usage:
    uv run scrape.py some-app-slug
    uv run scrape.py some-app-slug another-app-slug --ratings 1,2,3
    uv run scrape.py https://apps.shopify.com/some-app-slug --ratings 1 --delay 4
    uv run scrape.py some-app-slug --refresh   # delta: stop at known reviews
    uv run scrape.py some-app-slug --check     # live selector canary, no writes

Re-runs are incremental: reviews upsert by (app_slug, review_id), and an
interrupted run continues exactly where it stopped with --resume (progress is
tracked per (app, ratings-filter) in the scrape_progress table).
"""

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://apps.shopify.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
MAX_RETRIES = 8
RETRYABLE = {429, 500, 502, 503, 504}
SOURCE = "shopify"  # this scraper's source id; other sources get their own scrapers

# Optional slug -> canonical product-name map, so the same product can be tracked
# across sources (e.g. Shopify + G2). Ships empty; the tool works on any slug
# without it (product falls back to the app's own store name). To add your own,
# drop a products.json next to this script — it's git-ignored. See
# products.example.json for the format.
def _load_products():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "products.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


PRODUCTS = _load_products()

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    source         TEXT NOT NULL DEFAULT 'shopify',
    app_slug       TEXT NOT NULL,
    review_id      TEXT NOT NULL,
    rating         INTEGER,
    review_date    TEXT,
    shop_name      TEXT,
    country        TEXT,
    usage_duration TEXT,
    body           TEXT,
    dev_reply      TEXT,
    edited         INTEGER DEFAULT 0,
    scraped_at     TEXT NOT NULL,
    PRIMARY KEY (source, app_slug, review_id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_src_rating ON reviews (source, app_slug, rating);

CREATE TABLE IF NOT EXISTS apps (
    source       TEXT NOT NULL DEFAULT 'shopify',
    app_slug     TEXT NOT NULL,
    app_name     TEXT,
    product      TEXT,
    total_count  INTEGER,
    stars_5      INTEGER,
    stars_4      INTEGER,
    stars_3      INTEGER,
    stars_2      INTEGER,
    stars_1      INTEGER,
    last_scraped TEXT,
    PRIMARY KEY (source, app_slug)
);

CREATE TABLE IF NOT EXISTS scrape_progress (
    app_slug    TEXT NOT NULL,
    ratings_key TEXT NOT NULL,
    next_page   INTEGER NOT NULL,
    completed   INTEGER DEFAULT 0,
    updated_at  TEXT,
    PRIMARY KEY (app_slug, ratings_key)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables; migrate pre-multi-source DBs (no source column) in place."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reviews)")}
    if cols and "source" not in cols:
        conn.executescript(
            "ALTER TABLE reviews RENAME TO reviews_old;"
            "ALTER TABLE apps RENAME TO apps_old;"
            "DROP INDEX IF EXISTS idx_reviews_rating;")
        conn.executescript(SCHEMA)
        conn.execute(
            """INSERT INTO reviews (source, app_slug, review_id, rating, review_date,
                                    shop_name, country, usage_duration, body,
                                    dev_reply, edited, scraped_at)
               SELECT 'shopify', app_slug, review_id, rating, review_date, shop_name,
                      country, usage_duration, body, dev_reply, edited, scraped_at
               FROM reviews_old""")
        conn.execute(
            """INSERT INTO apps (source, app_slug, app_name, product, total_count,
                                 stars_5, stars_4, stars_3, stars_2, stars_1,
                                 last_scraped)
               SELECT 'shopify', app_slug, app_name, NULL, total_count,
                      stars_5, stars_4, stars_3, stars_2, stars_1, last_scraped
               FROM apps_old""")
        conn.executescript("DROP TABLE reviews_old; DROP TABLE apps_old;")
        print("migrated reviews.db to multi-source schema", file=sys.stderr)
    else:
        conn.executescript(SCHEMA)
    for slug, product in PRODUCTS.items():
        conn.execute("UPDATE apps SET product = ? WHERE app_slug = ? AND product IS NULL",
                     (product, slug))
    conn.commit()


def slug_of(arg: str) -> str:
    if arg.startswith("http"):
        path = urlparse(arg).path.strip("/")
        return path.split("/")[0]
    return arg.strip("/")


def fetch(session: requests.Session, url: str, params: dict) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        last = attempt == MAX_RETRIES
        try:
            resp = session.get(url, params=params, timeout=30)
        except requests.exceptions.RequestException as e:
            if last:
                break
            # Shopify's edge resets connections after bursts; needs a real
            # cooldown, not seconds — observed windows are 1min+.
            wait = min(240, 15 * 2 ** (attempt - 1))
            print(f"  connection error ({e.__class__.__name__}), cooling down "
                  f"{wait}s (attempt {attempt}/{MAX_RETRIES})", file=sys.stderr)
            session.close()  # drop the pooled connection the server reset
            time.sleep(wait)
            continue
        if resp.status_code == 200:
            return resp.text
        if resp.status_code in RETRYABLE:
            if last:
                break
            retry_after = resp.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else float(2**attempt)
            print(f"  HTTP {resp.status_code}, retrying in {wait:.0f}s "
                  f"(attempt {attempt}/{MAX_RETRIES})", file=sys.stderr)
            time.sleep(wait)
            continue
        raise RuntimeError(f"HTTP {resp.status_code} on {resp.url} — not retryable "
                           f"(bad app slug? bot-blocked?)")
    raise RuntimeError(f"Giving up on {url} after {MAX_RETRIES} attempts")


def parse_star_distribution(soup: BeautifulSoup) -> dict[int, int]:
    """Counts on the rating-filter links of the UNFILTERED reviews page.

    Only trust links with exactly one ratings param and no page param —
    pagination links on filtered pages also carry ratings[] in their href.
    """
    dist = {}
    for link in soup.select('a[href*="ratings%5B%5D="], a[href*="ratings[]="]'):
        href = link.get("href", "")
        stars = re.findall(r"ratings(?:%5B%5D|\[\])=(\d)", href)
        if len(stars) != 1 or "page=" in href:
            continue
        # the aria-label carries the exact count ("2572 total reviews") while
        # the link text is rounded ("2.6K") — prefer exact, fall back to text
        am = re.match(r"([\d,]+)\s+total reviews", link.get("aria-label") or "")
        if am:
            dist[int(stars[0])] = int(am.group(1).replace(",", ""))
            continue
        text = link.get_text(" ", strip=True)
        cm = re.fullmatch(r"([\d.,]+)\s*([Kk])?", text)
        if not cm:
            continue
        n = float(cm.group(1).replace(",", ""))
        dist[int(stars[0])] = int(n * 1000) if cm.group(2) else int(n)
    return dist


def app_name_from_title(soup: BeautifulSoup) -> str | None:
    title = soup.select_one("title")
    if not title:
        return None
    name = re.sub(r"\s*[-–—|].*$", "", title.get_text(strip=True)).strip()
    return re.sub(r"^Reviews:\s*", "", name) or None


def plausible_listing_end(page: int, expected: int) -> bool:
    """Is a 0-block page at `page` believable as the end of the listing?

    True when the pages walked so far already cover most of the expected
    count; False means the listing should still be going — the selectors
    probably broke. 0.7 absorbs star-count rounding and deleted reviews.
    """
    return expected <= 0 or (page - 1) * 10 >= 0.7 * expected


def parse_reviews(soup: BeautifulSoup) -> tuple[list[dict], int]:
    """Returns (parsed reviews, number of review blocks seen).

    The block count drives last-page detection: a block that fails to parse
    must not make a full page look like a short (= last) one.
    """
    out = []
    n_blocks = 0
    for block in soup.select('div[id^="review-"]'):
        rid = block["id"].removeprefix("review-")
        if rid.startswith("reply-"):  # developer replies get their own review-reply-N block
            continue
        n_blocks += 1
        r = {"review_id": rid, "rating": None, "review_date": None,
             "shop_name": None, "country": None, "usage_duration": None,
             "body": None, "dev_reply": None, "edited": 0}

        stars = block.select_one('div[role="img"][aria-label*="out of 5 stars"]')
        if stars:
            m = re.match(r"(\d)", stars["aria-label"])
            r["rating"] = int(m.group(1)) if m else None

        date_el = block.select_one("div.tw-text-body-xs.tw-text-fg-tertiary")
        if date_el:
            raw = date_el.get_text(strip=True)
            if raw.startswith("Edited"):  # "Edited September 9, 2023"
                r["edited"] = 1
                raw = raw.removeprefix("Edited").strip()
            try:
                r["review_date"] = datetime.strptime(raw, "%B %d, %Y").date().isoformat()
            except ValueError:
                r["review_date"] = raw

        body_el = block.select_one("[data-truncate-content-copy]")
        if body_el:
            r["body"] = body_el.get_text("\n", strip=True)

        shop_el = block.select_one("span[title]")
        if shop_el:
            r["shop_name"] = shop_el["title"]
            meta_col = shop_el.find_parent("div").find_parent("div")
            plain_divs = [d.get_text(strip=True)
                          for d in meta_col.find_all("div", recursive=False)
                          if not d.find("span")]
            for txt in plain_divs:
                if "using the app" in txt:
                    r["usage_duration"] = txt
                elif txt:
                    r["country"] = txt

        reply_el = block.select_one("[data-merchant-review-reply]")
        if reply_el:
            # the reply body sits in its own truncate-copy inside the reply block;
            # avoid the "<Dev> replied <date>" header text around it
            reply_body = reply_el.select_one("[data-truncate-content-copy]")
            r["dev_reply"] = reply_body.get_text("\n", strip=True) if reply_body else None

        if r["rating"] is None and r["body"] is None:
            print(f"  warning: review {rid} parsed empty, HTML may have drifted",
                  file=sys.stderr)
            continue
        out.append(r)
    return out, n_blocks


def _review_changed(old: tuple, new: dict) -> bool:
    """Mirror the upsert's COALESCE semantics: a None parse never overwrites,
    so only a non-None differing value (or a raised edited flag) is a change."""
    _, rating, review_date, body, dev_reply, edited = old
    return any(
        nv is not None and nv != ov
        for nv, ov in ((new["rating"], rating), (new["review_date"], review_date),
                       (new["body"], body), (new["dev_reply"], dev_reply))
    ) or (new["edited"] or 0) > (edited or 0)


def scrape_app(conn: sqlite3.Connection, session: requests.Session, slug: str,
               ratings: list[int] | None, delay: float, max_pages: int,
               resume: bool = False, refresh: bool = False,
               stop_after_known: int = 2) -> None:
    url = f"{BASE}/{slug}/reviews"
    params: dict = {"sort_by": "newest"}
    if ratings:
        params["ratings[]"] = ratings
    ratings_key = ",".join(map(str, ratings)) if ratings else "all"

    print(f"Scraping {slug} (stars: {ratings_key})")

    if refresh:
        # Early-stop is only sound over a fully crawled population: an
        # incomplete baseline would make every unscraped page look "new".
        row = conn.execute(
            "SELECT completed FROM scrape_progress "
            "WHERE app_slug = ? AND ratings_key = ?", (slug, ratings_key)).fetchone()
        if not row or not row[0]:
            print("  no completed baseline for this filter — falling back to a "
                  "full scrape", file=sys.stderr)
            refresh = False

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Distribution counts come from the unfiltered page: on filtered pages the
    # filter links carry combined ratings params and can't be mapped to a star.
    first_html = fetch(session, url, {"sort_by": "newest", "page": 1})
    first_soup = BeautifulSoup(first_html, "html.parser")
    dist = parse_star_distribution(first_soup)
    app_name = app_name_from_title(first_soup)
    expected = sum(dist.get(s, 0) for s in ratings) if ratings else sum(dist.values())
    print(f"  distribution {dict(sorted(dist.items(), reverse=True))}, "
          f"expecting ~{expected} reviews (K-counts are approximate)")

    start_page = 1
    if resume:
        row = conn.execute(
            "SELECT next_page, completed FROM scrape_progress "
            "WHERE app_slug = ? AND ratings_key = ?", (slug, ratings_key)).fetchone()
        if row and not row[1]:
            start_page = row[0]
            print(f"  resuming interrupted run at page {start_page}")
        elif row:
            print("  previous run for this filter completed; "
                  "starting from page 1 to pick up new reviews")
        else:
            print("  no interrupted run recorded for this filter; starting from page 1")

    total_saved = 0
    n_new = n_updated = pages_fetched = consec_known = 0
    cap_hit = True  # flips to False when we see the listing's natural end
    for page in range(start_page, max_pages + 1):
        if page == start_page and page == 1 and not ratings:
            soup = first_soup
        else:
            time.sleep(delay + random.uniform(0, delay / 2))
            html = fetch(session, url, {**params, "page": page})
            soup = BeautifulSoup(html, "html.parser")

        reviews, n_blocks = parse_reviews(soup)
        # DOM-rot guards: a 0-block page mid-listing, or blocks that all fail
        # to parse, must abort loudly — never masquerade as the natural end.
        if n_blocks == 0 and not plausible_listing_end(page, expected):
            raise RuntimeError(
                f"page {page} has 0 review blocks but ~{expected} reviews "
                f"expected — selectors likely broke; run scrape.py --check {slug}")
        if n_blocks > 0 and not reviews:
            raise RuntimeError(
                f"page {page}: {n_blocks} review blocks, none parsed — "
                f"selectors likely broke; run scrape.py --check {slug}")
        page_all_known = False
        if refresh and reviews:
            ids = [r["review_id"] for r in reviews]
            marks = ",".join("?" * len(ids))
            existing = {row[0]: row for row in conn.execute(
                f"""SELECT review_id, rating, review_date, body, dev_reply, edited
                    FROM reviews WHERE source = ? AND app_slug = ?
                    AND review_id IN ({marks})""", [SOURCE, slug, *ids])}
            page_all_known = len(existing) == len(ids)
            for r in reviews:
                old = existing.get(r["review_id"])
                if old is None:
                    n_new += 1
                elif _review_changed(old, r):
                    n_updated += 1

        if reviews:
            conn.executemany(
                """INSERT INTO reviews (source, app_slug, review_id, rating,
                                        review_date, shop_name, country,
                                        usage_duration, body, dev_reply, edited,
                                        scraped_at)
                   VALUES (:source, :app_slug, :review_id, :rating, :review_date,
                           :shop_name, :country, :usage_duration, :body, :dev_reply,
                           :edited, :scraped_at)
                   ON CONFLICT (source, app_slug, review_id) DO UPDATE SET
                     rating=COALESCE(excluded.rating, rating),
                     review_date=COALESCE(excluded.review_date, review_date),
                     shop_name=COALESCE(excluded.shop_name, shop_name),
                     country=COALESCE(excluded.country, country),
                     usage_duration=COALESCE(excluded.usage_duration, usage_duration),
                     body=COALESCE(excluded.body, body),
                     dev_reply=COALESCE(excluded.dev_reply, dev_reply),
                     edited=MAX(edited, excluded.edited),
                     scraped_at=excluded.scraped_at""",
                [{**r, "source": SOURCE, "app_slug": slug, "scraped_at": now}
                 for r in reviews],
            )
        if not refresh:
            # refresh never touches progress: the baseline stays "completed"
            conn.execute(
                """INSERT INTO scrape_progress (app_slug, ratings_key, next_page,
                                                completed, updated_at)
                   VALUES (?, ?, ?, 0, ?)
                   ON CONFLICT (app_slug, ratings_key) DO UPDATE SET
                     next_page=excluded.next_page, completed=0,
                     updated_at=excluded.updated_at""",
                (slug, ratings_key, page + 1, now),
            )
        conn.commit()
        total_saved += len(reviews)
        pages_fetched += 1
        print(f"  page {page}: {len(reviews)} reviews ({total_saved} this run)")

        if refresh:
            consec_known = consec_known + 1 if page_all_known else 0
            if consec_known >= stop_after_known:
                cap_hit = False
                print(f"  refresh: {stop_after_known} consecutive all-known pages "
                      f"— stopping early")
                break

        if n_blocks < 10:  # short or empty page = end of listing
            cap_hit = False
            break

    if cap_hit:
        print(f"  WARNING: stopped at --max-pages {max_pages} before the end of the "
              f"listing — re-run with --resume to continue", file=sys.stderr)
    elif not refresh:
        conn.execute(
            "UPDATE scrape_progress SET completed = 1, updated_at = ? "
            "WHERE app_slug = ? AND ratings_key = ?", (now, slug, ratings_key))

    product = PRODUCTS.get(slug) or app_name or slug
    conn.execute(
        """INSERT INTO apps (source, app_slug, app_name, product, total_count,
                             stars_5, stars_4, stars_3, stars_2, stars_1,
                             last_scraped)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (source, app_slug) DO UPDATE SET
             app_name=excluded.app_name, product=excluded.product,
             total_count=excluded.total_count,
             stars_5=excluded.stars_5, stars_4=excluded.stars_4,
             stars_3=excluded.stars_3, stars_2=excluded.stars_2,
             stars_1=excluded.stars_1, last_scraped=excluded.last_scraped""",
        (SOURCE, slug, app_name, product, sum(dist.values()) or None,
         dist.get(5), dist.get(4), dist.get(3), dist.get(2), dist.get(1), now),
    )
    conn.commit()
    if refresh:
        print(f"  refresh done: +{n_new} new, ~{n_updated} updated "
              f"({pages_fetched} pages fetched) for {slug}")
    else:
        print(f"  done: {total_saved} reviews saved this run for {slug}")


def check_app(session: requests.Session, slug: str) -> list[str]:
    """Fetch one live page and verify every selector still extracts data.

    Field checks are systemic: DOM rot breaks a field on EVERY review, so a
    check fails only when no review on the page yields the field. Returns the
    list of failed check names (empty = healthy). Writes nothing to the db.
    """
    url = f"{BASE}/{slug}/reviews"
    soup = BeautifulSoup(fetch(session, url, {"sort_by": "newest", "page": 1}),
                         "html.parser")
    fails: list[str] = []

    def report(ok: bool, label: str, detail: str) -> None:
        print(f"  {'✓' if ok else '✗'} {label}: {detail}")
        if not ok:
            fails.append(label)

    app_name = app_name_from_title(soup)
    report(bool(app_name), "app name from <title>", app_name or "not found")

    dist = parse_star_distribution(soup)
    report(bool(dist), "star-distribution links",
           str(dict(sorted(dist.items(), reverse=True))) if dist else "none found")

    reviews, n_blocks = parse_reviews(soup)
    total = sum(dist.values())
    report(n_blocks > 0 or total == 0, "review blocks",
           f"{n_blocks} on page 1 (~{total} reviews listed)")
    report(bool(reviews) or n_blocks == 0, "blocks parse",
           f"{len(reviews)}/{n_blocks}")

    if reviews:
        n = len(reviews)
        for label, pred in [
            ("rating", lambda r: r["rating"] in (1, 2, 3, 4, 5)),
            ("review_date", lambda r: re.fullmatch(r"\d{4}-\d{2}-\d{2}",
                                                   r["review_date"] or "")),
            ("body", lambda r: r["body"] is not None),
            ("shop_name", lambda r: bool(r["shop_name"])),
        ]:
            good = sum(1 for r in reviews if pred(r))
            report(good > 0, label, f"{good}/{n}")
    return fails


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape Shopify App Store reviews to SQLite")
    ap.add_argument("apps", nargs="+", help="app slugs or apps.shopify.com URLs")
    ap.add_argument("--ratings", help="comma-separated stars to fetch, e.g. 1,2,3 (default: all)")
    ap.add_argument("--delay", type=float, default=3.0, help="base delay between requests (s)")
    ap.add_argument("--db", default="reviews.db", help="SQLite file (default reviews.db)")
    ap.add_argument("--max-pages", type=int, default=2000, help="safety cap per app")
    ap.add_argument("--resume", action="store_true",
                    help="continue an interrupted run from where the db left off")
    ap.add_argument("--refresh", action="store_true",
                    help="delta mode: walk newest-first and stop early once "
                         "pages contain only already-known reviews (needs a "
                         "completed baseline scrape for the same filter)")
    ap.add_argument("--stop-after-known", type=int, default=2, metavar="K",
                    help="consecutive all-known pages that end a --refresh "
                         "(default 2)")
    ap.add_argument("--check", action="store_true",
                    help="canary: fetch one live page per app and verify every "
                         "selector still works; no db writes")
    args = ap.parse_args()

    if args.refresh and args.resume:
        ap.error("--refresh and --resume are mutually exclusive: --resume "
                 "finishes an interrupted crawl, --refresh assumes a finished one")

    ratings = None
    if args.ratings:
        ratings = sorted({int(x) for x in args.ratings.split(",")})
        if not all(1 <= r <= 5 for r in ratings):
            ap.error("--ratings values must be 1..5")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    if args.check:
        broken = []
        for i, app in enumerate(args.apps):
            slug = slug_of(app)
            if i:
                time.sleep(args.delay + random.uniform(0, args.delay / 2))
            print(f"Checking {slug}")
            if check_app(session, slug):
                broken.append(slug)
        if broken:
            print(f"CHECK FAILED for: {', '.join(broken)}", file=sys.stderr)
            sys.exit(1)
        print("all checks passed")
        return

    conn = sqlite3.connect(args.db)
    ensure_schema(conn)

    failed = []
    for app in args.apps:
        slug = slug_of(app)
        try:
            scrape_app(conn, session, slug, ratings, args.delay, args.max_pages,
                       resume=args.resume, refresh=args.refresh,
                       stop_after_known=args.stop_after_known)
        except (RuntimeError, requests.exceptions.RequestException) as e:
            n = conn.execute("SELECT COUNT(*) FROM reviews WHERE app_slug = ?",
                             (slug,)).fetchone()[0]
            print(f"  {e}\n  partial progress saved ({n} reviews in db) — "
                  f"re-run with --resume to continue", file=sys.stderr)
            failed.append(slug)

    conn.close()
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
