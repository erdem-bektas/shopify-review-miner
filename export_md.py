# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Export scraped reviews from SQLite to per-app markdown files.

Usage:
    uv run export_md.py                     # all apps, all ratings -> exports/
    uv run export_md.py --ratings 1,2,3     # complaints only
    uv run export_md.py --apps some-app-slug --out exports
"""

import argparse
import sqlite3
from pathlib import Path
from urllib.parse import urlparse


def slug_of(arg: str) -> str:
    if arg.startswith("http"):
        path = urlparse(arg).path.strip("/")
        return path.split("/")[0]
    return arg.strip("/")


def export_app(conn: sqlite3.Connection, slug: str, ratings: list[int] | None,
               out_dir: Path) -> Path:
    app = conn.execute("SELECT * FROM apps WHERE app_slug = ?", (slug,)).fetchone()
    where = "app_slug = ?"
    params: list = [slug]
    if ratings:
        where += f" AND rating IN ({','.join('?' * len(ratings))})"
        params += ratings
    rows = conn.execute(
        f"""SELECT rating, review_date, shop_name, country, usage_duration,
                   body, dev_reply, edited
            FROM reviews WHERE {where}
            ORDER BY rating ASC, review_date DESC""", params).fetchall()

    lines = [f"# {app['app_name'] or slug} — reviews\n" if app else f"# {slug} — reviews\n"]
    if app:
        lines.append(
            f"> Total on store: {app['total_count']} | "
            f"5★ {app['stars_5']} · 4★ {app['stars_4']} · 3★ {app['stars_3']} · "
            f"2★ {app['stars_2']} · 1★ {app['stars_1']} | "
            f"scraped {app['last_scraped']}\n"
        )
    lines.append(f"_{len(rows)} reviews in this export"
                 f"{' (stars: ' + ','.join(map(str, ratings)) + ')' if ratings else ''}_\n")

    current: object = object()  # sentinel: a rating can legitimately be NULL/None
    for r in rows:
        if r["rating"] != current:
            current = r["rating"]
            n = sum(1 for x in rows if x["rating"] == current)
            label = f"{'★' * current} {current}-star" if current else "unrated"
            lines.append(f"\n## {label} ({n})\n")
        meta = " · ".join(x for x in (r["review_date"], r["shop_name"], r["country"],
                                      r["usage_duration"],
                                      "✏️ edited" if r["edited"] else None) if x)
        lines.append(f"### {meta}\n")
        lines.append((r["body"] or "_(no text)_") + "\n")
        if r["dev_reply"]:
            reply = "\n> ".join(r["dev_reply"].splitlines())
            lines.append(f"> **Developer reply:** {reply}\n")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slug}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Export reviews.db to markdown")
    ap.add_argument("--db", default="reviews.db")
    ap.add_argument("--apps", nargs="*", help="app slugs (default: every app in db)")
    ap.add_argument("--ratings", help="comma-separated stars to include, e.g. 1,2,3")
    ap.add_argument("--out", default="exports", help="output directory")
    args = ap.parse_args()

    ratings = [int(x) for x in args.ratings.split(",")] if args.ratings else None
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    slugs = ([slug_of(a) for a in args.apps] if args.apps else
             [r["app_slug"] for r in
              conn.execute("SELECT DISTINCT app_slug FROM reviews")])
    for slug in slugs:
        path = export_app(conn, slug, ratings, Path(args.out))
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
