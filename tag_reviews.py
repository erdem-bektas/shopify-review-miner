# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Export untagged reviews for tagging, and import tags back into reviews.db.

The tagging itself is done by an LLM you drive (see docs/tagging-rubric.md), not by this
script or any external API. This script is only the plumbing: it hands out
batches of untagged reviews as JSON and writes the returned tags into a new
`review_tags` table, resumably (already-tagged reviews are never handed out
again).

    # migrate the db (idempotent) and show progress
    uv run tag_reviews.py --stats

    # calibration: a stratified ~100-review sample from one app, reproducible
    uv run tag_reviews.py --export --app some-app-slug \
        --stratified -n 100 --seed 7 --out batch.json

    # full run: next N untagged reviews (optionally one app at a time)
    uv run tag_reviews.py --export --app some-app-slug -n 200 --out batch.json

    # write the tags back (idempotent: re-importing a review replaces its tags)
    uv run tag_reviews.py --import-batch batch.tags.json

Schema additions are backward compatible: a new `review_tags` table plus indexes,
nothing touched in `reviews`/`apps`, so the existing UI keeps working.
"""

import argparse
import json
import random
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

THEMES = {
    "deliverability", "flows_automation", "segmentation", "data_management",
    "templates_editor", "reporting_analytics", "integrations_sync", "sms",
    "forms_popups", "pricing_billing", "support", "onboarding_migration",
    "performance_bugs", "other",
}
KINDS = {"feature_gap", "feature_request", "service", "pricing", "bug", "praise"}
CONFIDENCE = {"high", "medium", "low"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_tags (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    app_slug      TEXT NOT NULL,
    review_id     TEXT NOT NULL,
    theme         TEXT NOT NULL,
    kind          TEXT NOT NULL,
    churn_signal  INTEGER NOT NULL DEFAULT 0,
    switched_to   TEXT,
    quote         TEXT,
    confidence    TEXT NOT NULL DEFAULT 'medium',
    tagged_at     TEXT NOT NULL,
    FOREIGN KEY (source, app_slug, review_id)
        REFERENCES reviews (source, app_slug, review_id)
);
CREATE INDEX IF NOT EXISTS idx_tags_review ON review_tags (source, app_slug, review_id);
CREATE INDEX IF NOT EXISTS idx_tags_theme  ON review_tags (app_slug, theme, kind);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def usage_to_years(text: str | None) -> float | None:
    """Parse 'About 1 year using the app' / '6 months' / '24 minutes' → years.

    Modifiers (About/Almost/Over/Less than) are ignored; the number is taken as
    an approximation, which is all the seniority signal needs.
    """
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(minute|hour|day|week|month|year)", text, re.I)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2).lower()
    per_year = {"minute": 525600, "hour": 8760, "day": 365, "week": 52.143,
                "month": 12, "year": 1}[unit]
    return round(n / per_year, 4)


def era_of(review_date: str | None) -> str:
    """Coarse era bucket for stratified sampling."""
    if not review_date or len(review_date) < 4 or not review_date[:4].isdigit():
        return "unknown"
    y = int(review_date[:4])
    if y >= 2024:
        return "recent"
    if y >= 2020:
        return "mid"
    return "old"


def _untagged_where() -> str:
    return ("r.body IS NOT NULL AND trim(r.body) <> '' "
            "AND NOT EXISTS (SELECT 1 FROM review_tags t "
            "WHERE t.source = r.source AND t.app_slug = r.app_slug "
            "AND t.review_id = r.review_id)")


def fetch_untagged(conn: sqlite3.Connection, app: str | None) -> list[dict]:
    sql = (f"SELECT r.source, r.app_slug, r.review_id, r.rating, r.review_date, "
           f"r.shop_name, r.country, r.usage_duration, r.edited, r.body, r.dev_reply "
           f"FROM reviews r WHERE {_untagged_where()}")
    args: list = []
    if app:
        sql += " AND r.app_slug = ?"
        args.append(app)
    sql += " ORDER BY r.app_slug, r.rating, r.review_date"
    cols = ["source", "app_slug", "review_id", "rating", "review_date", "shop_name",
            "country", "usage_duration", "edited", "body", "dev_reply"]
    rows = []
    for rec in conn.execute(sql, args):
        d = dict(zip(cols, rec))
        d["usage_years"] = usage_to_years(d["usage_duration"])
        rows.append(d)
    return rows


def stratified_sample(rows: list[dict], n: int, seed: int) -> list[dict]:
    """Sample n rows spread across (rating × era) cells.

    Every non-empty cell gets at least one pick; the remaining budget is shared
    proportionally to cell size. Deterministic for a given seed.
    """
    rng = random.Random(seed)
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        cells[(r["rating"], era_of(r["review_date"]))].append(r)
    if not cells:
        return []
    n = min(n, len(rows))
    keys = sorted(cells, key=lambda k: (str(k[0]), k[1]))
    # floor of 1 per cell, then distribute the rest by cell weight
    alloc = {k: 1 for k in keys if cells[k]}
    remaining = n - sum(alloc.values())
    if remaining < 0:  # more non-empty cells than budget: keep the largest cells
        keep = sorted(keys, key=lambda k: -len(cells[k]))[:n]
        alloc = {k: 1 for k in keep}
        remaining = 0
    total = sum(len(cells[k]) for k in alloc)
    if remaining > 0 and total:
        extra = {k: int(remaining * len(cells[k]) / total) for k in alloc}
        for k in alloc:
            alloc[k] = min(len(cells[k]), alloc[k] + extra[k])
    # top up any rounding shortfall from cells that still have slack
    picked: list[dict] = []
    for k in alloc:
        picked += rng.sample(cells[k], min(alloc[k], len(cells[k])))
    if len(picked) < n:
        pool = [r for k in keys for r in cells[k] if r not in picked]
        rng.shuffle(pool)
        picked += pool[: n - len(picked)]
    rng.shuffle(picked)
    return picked[:n]


def cmd_export(conn: sqlite3.Connection, args) -> None:
    rows = fetch_untagged(conn, args.app)
    if args.stratified:
        rows = stratified_sample(rows, args.n, args.seed)
    else:
        rows = rows[: args.n]
    payload = json.dumps(rows, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)
        # a compact distribution summary to stderr so the operator sees the shape
        dist = defaultdict(int)
        for r in rows:
            dist[(r["rating"], era_of(r["review_date"]))] += 1
        print(f"exported {len(rows)} reviews → {args.out}", file=sys.stderr)
        for k in sorted(dist, key=lambda x: (str(x[0]), x[1])):
            print(f"  {k[0]}★ {k[1]:>7}: {dist[k]}", file=sys.stderr)
    else:
        print(payload)


def _iter_tags(data) -> list[dict]:
    if isinstance(data, dict) and "tags" in data:
        return data["tags"]
    if isinstance(data, list):
        return data
    raise SystemExit("import file must be a JSON array or {\"tags\": [...]}")


def cmd_import(conn: sqlite3.Connection, path: str) -> None:
    with open(path, encoding="utf-8") as f:
        tags = _iter_tags(json.load(f))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    valid_ids = {r[0]: (r[1], r[2]) for r in
                 conn.execute("SELECT review_id, source, app_slug FROM reviews")}
    touched: set[tuple] = set()
    rows: list[dict] = []
    warn = 0
    for i, t in enumerate(tags):
        rid = t.get("review_id")
        src = t.get("source", "shopify")
        slug = t.get("app_slug")
        theme, kind = t.get("theme"), t.get("kind")
        conf = t.get("confidence", "medium")
        problems = []
        if not rid or rid not in valid_ids:
            problems.append(f"unknown review_id {rid!r}")
        if theme not in THEMES:
            problems.append(f"bad theme {theme!r}")
        if kind not in KINDS:
            problems.append(f"bad kind {kind!r}")
        if conf not in CONFIDENCE:
            conf = "low"
        if problems:
            print(f"  skip tag #{i}: {'; '.join(problems)}", file=sys.stderr)
            warn += 1
            continue
        quote = (t.get("quote") or None)
        if quote:
            quote = quote.strip()[:240]
        rows.append({
            "source": src, "app_slug": slug, "review_id": rid, "theme": theme,
            "kind": kind, "churn_signal": 1 if t.get("churn_signal") else 0,
            "switched_to": (t.get("switched_to") or None), "quote": quote,
            "confidence": conf, "tagged_at": now,
        })
        touched.add((src, slug, rid))
    # idempotent: replace all tags for every review present in this batch
    for key in touched:
        conn.execute("DELETE FROM review_tags WHERE source=? AND app_slug=? AND review_id=?", key)
    conn.executemany(
        "INSERT INTO review_tags (source, app_slug, review_id, theme, kind, "
        "churn_signal, switched_to, quote, confidence, tagged_at) VALUES "
        "(:source, :app_slug, :review_id, :theme, :kind, :churn_signal, "
        ":switched_to, :quote, :confidence, :tagged_at)", rows)
    conn.commit()
    print(f"imported {len(rows)} tags across {len(touched)} reviews"
          f"{f' ({warn} skipped)' if warn else ''}", file=sys.stderr)


def cmd_stats(conn: sqlite3.Connection, app: str | None) -> None:
    where = "WHERE r.app_slug = ?" if app else ""
    args = [app] if app else []
    print("=== tagging progress (taggable = non-empty body) ===")
    for slug, total, tagged in conn.execute(
        f"SELECT r.app_slug, "
        f"  SUM(CASE WHEN r.body IS NOT NULL AND trim(r.body)<>'' THEN 1 ELSE 0 END), "
        f"  COUNT(DISTINCT t.review_id) "
        f"FROM reviews r LEFT JOIN review_tags t "
        f"  ON t.source=r.source AND t.app_slug=r.app_slug AND t.review_id=r.review_id "
        f"{where} GROUP BY r.app_slug ORDER BY 2 DESC", args):
        pct = f"{100*tagged/total:.0f}%" if total else "-"
        print(f"  {slug:<34} {tagged or 0:>5}/{total or 0:<6} tagged ({pct})")
    n_tags = conn.execute("SELECT COUNT(*) FROM review_tags").fetchone()[0]
    if not n_tags:
        print("\n(no tags yet — run --export, tag, then --import-batch)")
        return
    print(f"\n=== {n_tags} tags — theme × kind ===")
    grid: dict = defaultdict(lambda: defaultdict(int))
    kinds_seen: set = set()
    for theme, kind, c in conn.execute(
        "SELECT theme, kind, COUNT(*) FROM review_tags "
        f"{'WHERE app_slug=?' if app else ''} GROUP BY theme, kind", args):
        grid[theme][kind] = c
        kinds_seen.add(kind)
    order = [k for k in ("feature_gap", "feature_request", "bug", "service",
                         "pricing", "praise") if k in kinds_seen]
    print(f"  {'theme':<22}" + "".join(f"{k[:8]:>9}" for k in order) + f"{'total':>8}")
    for theme in sorted(grid, key=lambda t: -sum(grid[t].values())):
        row = grid[theme]
        print(f"  {theme:<22}" + "".join(f"{row.get(k,0):>9}" for k in order)
              + f"{sum(row.values()):>8}")
    churn = conn.execute(
        f"SELECT COUNT(*) FROM review_tags WHERE churn_signal=1"
        f"{' AND app_slug=?' if app else ''}", args).fetchone()[0]
    print(f"\n  churn signals: {churn}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Export/import review tags for reviews.db")
    ap.add_argument("--db", default="reviews.db")
    ap.add_argument("--export", action="store_true", help="export untagged reviews as JSON")
    ap.add_argument("--import-batch", dest="import_batch", metavar="FILE",
                    help="import tags from a JSON file")
    ap.add_argument("--stats", action="store_true", help="show tagging progress")
    ap.add_argument("--app", help="restrict to one app_slug")
    ap.add_argument("-n", type=int, default=100, help="batch/sample size (default 100)")
    ap.add_argument("--stratified", action="store_true",
                    help="draw a stratified (rating × era) sample instead of sequential")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed for --stratified")
    ap.add_argument("--out", help="write export JSON to a file (default stdout)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    ensure_schema(conn)
    if args.export:
        cmd_export(conn, args)
    elif args.import_batch:
        cmd_import(conn, args.import_batch)
    elif args.stats:
        cmd_stats(conn, args.app)
    else:
        ap.error("one of --export / --import-batch / --stats is required")
    conn.close()


if __name__ == "__main__":
    main()
