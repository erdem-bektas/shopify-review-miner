# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Score unbundling opportunities from review_tags, per (app, theme).

    score = frequency x persistence x seniority x feature_shapedness

combined as a weighted geometric mean so every factor matters and a theme that
is purely pricing/support (feature_shapedness = 0) scores 0 — "not every
complaint is an opportunity". The weights and saturation constants live in the
CONFIG block below; nothing is hard-coded deep in the logic.

    uv run opportunity_report.py                    # all apps, ranked table
    uv run opportunity_report.py --app some-app-slug
    uv run opportunity_report.py --heatmap          # theme x app score grid
    uv run opportunity_report.py --top 3            # top themes + supporting quotes
    uv run opportunity_report.py --json             # machine-readable, for the UI/summary

Reads the same `review_tags` the UI reads; the UI mirrors this formula in JS so
the heatmap stays serverless. Keep the two in sync (see docs/tagging-rubric.md).
"""

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone

# ── CONFIG — tweak freely, this is the whole knob panel ──────────────────────
WEIGHTS = {  # exponents in the geometric mean; raise one to lean on that factor
    "frequency": 1.0,
    "persistence": 1.0,
    "seniority": 1.0,
    "feature_shape": 1.0,
}
FREQ_SAT = 0.20          # theme = 20%+ of an app's tagged reviews → max frequency
DATA_SPAN_YEARS = 11.0   # normalizer for the first→last span of a theme
RECENCY_MONTHS = 12      # "still alive" window, measured from the newest review in the db
RECENCY_PENALTY = 0.40   # persistence multiplier when a theme has gone quiet
SENIORITY_SAT_YEARS = 3.0  # complaining after 3+ years of use → max seniority
PERSISTENCE_FLOOR = 0.15  # a fresh but recurring theme isn't zero-persistent
SENIORITY_FLOOR = 0.20   # new-user gaps are discounted, not erased
MIN_FREQ_REVIEWS = 2     # a single voice isn't a pattern; drop themes below this
FEATURE_KINDS = {"feature_gap", "feature_request"}
DIFF_MIN_SCORE = 25.0    # scores are 0–100: a theme "is an opportunity" in diffs above this
DIFF_MIN_DELTA = 10.0    # score movement that makes the risers/fallers list
TREND_RECENT_YEARS = 2   # the "recent" window: last N calendar years
TREND_RATIO = 1.6        # recent/base share ratio that flips growing/fading
TREND_MIN_TAGS = 4       # fewer feature tags than this → no trend call
# ─────────────────────────────────────────────────────────────────────────────

FALLBACK_TODAY = date(2026, 7, 5)


def usage_to_years(text):
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(minute|hour|day|week|month|year)", text, re.I)
    if not m:
        return None
    per_year = {"minute": 525600, "hour": 8760, "day": 365, "week": 52.143,
                "month": 12, "year": 1}[m.group(2).lower()]
    return float(m.group(1)) / per_year


def parse_date(s):
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def load(conn, app):
    where, args = "", []
    if app:
        where, args = "WHERE t.app_slug = ?", [app]
    # vendor_ack arrived later; older dbs (or sample.db) may not have it yet
    db_cols = {r[1] for r in conn.execute("PRAGMA table_info(review_tags)")}
    ack = "t.vendor_ack" if "vendor_ack" in db_cols else "NULL"
    rows = conn.execute(
        f"SELECT t.app_slug, t.theme, t.kind, t.review_id, r.review_date, "
        f"       r.usage_duration, t.churn_signal, t.switched_to, t.quote, "
        f"       t.confidence, {ack}, r.rating, r.shop_name "
        f"FROM review_tags t JOIN reviews r ON r.source=t.source "
        f"  AND r.app_slug=t.app_slug AND r.review_id=t.review_id {where}", args)
    cols = ["app_slug", "theme", "kind", "review_id", "review_date", "usage_duration",
            "churn_signal", "switched_to", "quote", "confidence", "vendor_ack",
            "rating", "shop_name"]
    return [dict(zip(cols, r)) for r in rows]


def newest_review_date(conn):
    r = conn.execute("SELECT MAX(review_date) FROM reviews "
                     "WHERE review_date GLOB '20[0-9][0-9]-*'").fetchone()[0]
    return parse_date(r) or FALLBACK_TODAY


def geomean(parts):
    prod, wsum = 1.0, 0.0
    for value, w in parts:
        prod *= value ** w
        wsum += w
    return prod ** (1.0 / wsum) if wsum else 0.0


TREND_ARROW = {"growing": "↑", "stable": "→", "fading": "↓"}


def trend_of(feat, app_tags, current_year):
    """Classify a theme's yearly share series as growing/stable/fading.

    share[y] = theme's feature-kind tags in year y / the app's distinct tagged
    reviews in year y. recent = mean share of the last TREND_RECENT_YEARS
    calendar years (missing years count as 0); base = mean share of all
    earlier years with tagged data. This is the normative definition — the JS
    in reviews.html mirrors it; change both together.

    Returns (label | None, shares); None = not enough data to call a trend.
    """
    app_years = defaultdict(set)
    for t in app_tags:
        d = parse_date(t["review_date"])
        if d:
            app_years[d.year].add(t["review_id"])
    feat_count = defaultdict(int)
    for t in feat:
        d = parse_date(t["review_date"])
        if d:
            feat_count[d.year] += 1
    shares = {y: feat_count.get(y, 0) / len(app_years[y]) for y in sorted(app_years)}
    if len(app_years) < 3 or sum(feat_count.values()) < TREND_MIN_TAGS:
        return None, shares
    window_start = current_year - TREND_RECENT_YEARS + 1
    recent = sum(shares.get(y, 0.0)
                 for y in range(window_start, current_year + 1)) / TREND_RECENT_YEARS
    earlier = [shares[y] for y in shares if y < window_start]
    base = sum(earlier) / len(earlier) if earlier else 0.0
    if recent > 0 and (base == 0 or recent >= TREND_RATIO * base):
        return "growing", shares
    if base > 0 and recent <= base / TREND_RATIO:
        return "fading", shares
    return "stable", shares


def score_apps(tags, today):
    """Return {app: {theme: metrics}} with component breakdown and score."""
    by_app = defaultdict(list)
    for t in tags:
        by_app[t["app_slug"]].append(t)

    out = {}
    for app, app_tags in by_app.items():
        tagged_reviews = {t["review_id"] for t in app_tags}
        t_app = len(tagged_reviews) or 1
        themes = defaultdict(list)
        for t in app_tags:
            themes[t["theme"]].append(t)

        app_out = {}
        for theme, tt in themes.items():
            feat = [t for t in tt if t["kind"] in FEATURE_KINDS]
            feat_reviews = {t["review_id"] for t in feat}
            n_service = sum(t["kind"] == "service" for t in tt)
            n_pricing = sum(t["kind"] == "pricing" for t in tt)
            n_bug = sum(t["kind"] == "bug" for t in tt)
            n_praise = sum(t["kind"] == "praise" for t in tt)

            # feature-shapedness (gate): buildable gaps vs. operational gripes
            fs_denom = len(feat) + n_service + n_pricing
            feature_shape = len(feat) / fs_denom if fs_denom else 0.0

            trend, _ = trend_of(feat, app_tags, today.year)

            if len(feat_reviews) < MIN_FREQ_REVIEWS or feature_shape == 0:
                app_out[theme] = _metrics(theme, tt, feat, feat_reviews,
                                          0.0, 0.0, 0.0, 0.0, feature_shape,
                                          n_service, n_pricing, n_bug, n_praise,
                                          None, None, 0.0, trend)
                continue

            frequency = min(1.0, (len(feat_reviews) / t_app) / FREQ_SAT)

            dates = sorted(d for d in (parse_date(t["review_date"]) for t in feat) if d)
            if dates:
                span_years = (dates[-1] - dates[0]).days / 365.0
                months_since = (today - dates[-1]).days / 30.44
                recent = months_since <= RECENCY_MONTHS
            else:
                span_years, recent = 0.0, False
            p_span = min(1.0, span_years / DATA_SPAN_YEARS)
            persistence = (PERSISTENCE_FLOOR + (1 - PERSISTENCE_FLOOR) * p_span) \
                * (1.0 if recent else RECENCY_PENALTY)

            usages = [usage_to_years(t["usage_duration"]) for t in feat]
            usages = [u for u in usages if u is not None]
            avg_usage = sum(usages) / len(usages) if usages else 0.0
            seniority = SENIORITY_FLOOR + (1 - SENIORITY_FLOOR) * \
                min(1.0, avg_usage / SENIORITY_SAT_YEARS)

            score = 100 * geomean([
                (max(frequency, 1e-6), WEIGHTS["frequency"]),
                (persistence, WEIGHTS["persistence"]),
                (seniority, WEIGHTS["seniority"]),
                (feature_shape, WEIGHTS["feature_shape"]),
            ])
            app_out[theme] = _metrics(
                theme, tt, feat, feat_reviews, frequency, persistence, seniority,
                score, feature_shape, n_service, n_pricing, n_bug, n_praise,
                dates[0].isoformat() if dates else None,
                dates[-1].isoformat() if dates else None, avg_usage, trend)
        out[app] = app_out
    return out


def _metrics(theme, tt, feat, feat_reviews, freq, pers, sen, score, fshape,
             n_service, n_pricing, n_bug, n_praise, first, last, avg_usage,
             trend):
    return {
        "theme": theme, "score": round(score, 1), "trend": trend,
        "frequency": round(freq, 3), "persistence": round(pers, 3),
        "seniority": round(sen, 3), "feature_shape": round(fshape, 3),
        "n_feature_reviews": len(feat_reviews), "n_tags": len(tt),
        "n_service": n_service, "n_pricing": n_pricing, "n_bug": n_bug,
        "n_praise": n_praise, "first_seen": first, "last_seen": last,
        "avg_usage_years": round(avg_usage, 2),
        "n_churn": sum(t["churn_signal"] for t in tt),
        # non-"none" dev-reply acknowledgments; annotation only, not a factor
        "vendor_ack": dict(Counter(
            t["vendor_ack"] for t in tt
            if t.get("vendor_ack") and t["vendor_ack"] != "none")),
    }


def print_tables(scored):
    for app in sorted(scored):
        themes = [m for m in scored[app].values() if m["score"] > 0]
        themes.sort(key=lambda m: -m["score"])
        if not themes:
            continue
        print(f"\n### {app}")
        print(f"  {'theme':<22}{'score':>6}{'trnd':>5}{'freq':>6}{'pers':>6}{'seny':>6}"
              f"{'fshp':>6}{'#feat':>6}{'span':>18}{'chrn':>5}")
        for m in themes:
            span = f"{m['first_seen'] or '?'}→{m['last_seen'] or '?'}"
            arrow = TREND_ARROW.get(m["trend"], "·")
            print(f"  {m['theme']:<22}{m['score']:>6.1f}{arrow:>5}{m['frequency']:>6.2f}"
                  f"{m['persistence']:>6.2f}{m['seniority']:>6.2f}"
                  f"{m['feature_shape']:>6.2f}{m['n_feature_reviews']:>6}"
                  f"{span:>18}{m['n_churn']:>5}")


def print_heatmap(scored):
    apps = sorted(scored)
    themes = sorted({th for a in scored.values() for th in a})
    print("\ntheme × app opportunity score\n")
    print(f"{'theme':<22}" + "".join(f"{a[:10]:>11}" for a in apps))
    for th in themes:
        cells = []
        for a in apps:
            m = scored[a].get(th)
            s = m["score"] if m else 0
            if s:
                # ↑/↓ only — a stable arrow on every cell is noise
                arrow = {"growing": "↑", "fading": "↓"}.get(m["trend"], " ")
                cells.append(f"{s:>10.0f}{arrow}")
            else:
                cells.append(f"{'·':>11}")
        print(f"{th:<22}" + "".join(cells))


def print_top(scored, tags, n):
    quotes = defaultdict(list)
    for t in tags:
        if t["kind"] in FEATURE_KINDS and t["quote"]:
            quotes[(t["app_slug"], t["theme"])].append(t)
    ranked = sorted(
        ((a, m) for a in scored for m in scored[a].values() if m["score"] > 0),
        key=lambda x: -x[1]["score"])[:n]
    for app, m in ranked:
        trend = f"  {TREND_ARROW[m['trend']]} {m['trend']}" if m["trend"] else ""
        print(f"\n═══ {app} / {m['theme']}  —  score {m['score']}{trend}")
        print(f"    freq {m['frequency']} · persistence {m['persistence']} "
              f"({m['first_seen']}→{m['last_seen']}) · seniority {m['seniority']} "
              f"(~{m['avg_usage_years']}y) · feature-shape {m['feature_shape']}")
        ack_line = ""
        if m["vendor_ack"]:
            ack_line = ", vendor ack: " + " ".join(
                f"{k}×{v}" for k, v in sorted(m["vendor_ack"].items(),
                                              key=lambda x: -x[1]))
        print(f"    {m['n_feature_reviews']} feature-shaped reviews, "
              f"{m['n_churn']} churn signals{ack_line}")
        picks = sorted(quotes[(app, m["theme"])],
                       key=lambda t: (t["confidence"] != "high", t["kind"]))[:5]
        for t in picks:
            flag = " ⚠churn" if t["churn_signal"] else ""
            to = f" →{t['switched_to']}" if t["switched_to"] else ""
            ack = (f" ⚑{t['vendor_ack']}"
                   if t.get("vendor_ack") and t["vendor_ack"] != "none" else "")
            print(f"      • [{t['kind']}{to}{flag}{ack}] \"{t['quote']}\"")


def print_trend(tags, today):
    """Yearly share series per theme×app — the data behind the arrows."""
    by_app = defaultdict(list)
    for t in tags:
        by_app[t["app_slug"]].append(t)
    for app in sorted(by_app):
        app_tags = by_app[app]
        themes = defaultdict(list)
        for t in app_tags:
            if t["kind"] in FEATURE_KINDS:
                themes[t["theme"]].append(t)
        header = False
        for th in sorted(themes):
            label, shares = trend_of(themes[th], app_tags, today.year)
            if not shares:
                continue
            if not header:
                print(f"\n### {app}")
                header = True
            series = "  ".join(f"{y}:{shares[y]:.2f}" for y in sorted(shares))
            print(f"  {th:<22} {TREND_ARROW.get(label, '·')}  {series}")


SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL
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
"""


def ensure_snapshot_schema(conn):
    conn.executescript(SNAPSHOT_SCHEMA)
    conn.commit()


def cmd_snapshot(conn, scored, tags):
    """Persist the current scores so later runs can diff against them."""
    ensure_snapshot_schema(conn)
    taken = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sid = conn.execute("INSERT INTO snapshots (taken_at) VALUES (?)",
                       (taken,)).lastrowid
    tagged = defaultdict(set)
    for t in tags:
        tagged[t["app_slug"]].add(t["review_id"])
    conn.executemany(
        "INSERT INTO snapshot_scores VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(sid, app, th, m["score"], m["frequency"], m["persistence"],
          m["seniority"], m["feature_shape"], m["n_feature_reviews"],
          len(tagged[app]))
         for app, themes in scored.items() for th, m in themes.items()])
    conn.commit()
    print(f"snapshot {sid} saved ({taken})", file=sys.stderr)
    return sid


def _snapshot_scores(conn, sid):
    return {(a, t): s for a, t, s in conn.execute(
        "SELECT app_slug, theme, score FROM snapshot_scores "
        "WHERE snapshot_id = ?", (sid,))}


def cmd_diff(conn, ids, tags, as_json):
    """Compare two snapshots: new opportunities, risers/fallers, gone quiet."""
    ensure_snapshot_schema(conn)
    snaps = dict(conn.execute("SELECT id, taken_at FROM snapshots ORDER BY id"))
    if ids:
        old_id, new_id = (ids if len(ids) == 2 else (ids[0], max(snaps, default=0)))
    elif len(snaps) >= 2:
        new_id, old_id = sorted(snaps)[-1], sorted(snaps)[-2]
    else:
        sys.exit("need two snapshots to diff — run --snapshot on separate days")
    if old_id not in snaps or new_id not in snaps:
        sys.exit(f"unknown snapshot id (have: {sorted(snaps)})")

    old, new = _snapshot_scores(conn, old_id), _snapshot_scores(conn, new_id)
    quotes = defaultdict(list)
    for t in tags:
        if t["kind"] in FEATURE_KINDS and t["quote"]:
            quotes[(t["app_slug"], t["theme"])].append(t["quote"])

    fresh, gone, risers, fallers = [], [], [], []
    for key in sorted(set(old) | set(new)):
        o, n = old.get(key, 0.0), new.get(key, 0.0)
        if n >= DIFF_MIN_SCORE > o:
            fresh.append((key, o, n))
        elif o >= DIFF_MIN_SCORE > n:
            gone.append((key, o, n))
        elif n - o >= DIFF_MIN_DELTA:
            risers.append((key, o, n))
        elif o - n >= DIFF_MIN_DELTA:
            fallers.append((key, o, n))

    if as_json:
        enc = lambda rows: [{"app": a, "theme": t, "old": round(o, 1),
                             "new": round(n, 1), "quotes": quotes[(a, t)][:2]}
                            for (a, t), o, n in rows]
        print(json.dumps({"old_snapshot": old_id, "new_snapshot": new_id,
                          "new_opportunities": enc(fresh), "risers": enc(risers),
                          "fallers": enc(fallers), "gone_quiet": enc(gone)},
                         ensure_ascii=False, indent=2))
        return

    print(f"## Opportunity diff: snapshot {old_id} ({snaps[old_id][:10]}) "
          f"→ {new_id} ({snaps[new_id][:10]})")
    sections = [
        (f"New opportunities (score ≥ {DIFF_MIN_SCORE:.0f})", fresh, True),
        (f"Risers (Δ ≥ {DIFF_MIN_DELTA:.0f})", sorted(risers, key=lambda x: x[1] - x[2]), True),
        ("Fallers", sorted(fallers, key=lambda x: x[2] - x[1]), False),
        (f"Gone quiet (dropped below {DIFF_MIN_SCORE:.0f})", gone, False),
    ]
    for title, rows, with_quotes in sections:
        if not rows:
            continue
        print(f"\n### {title}")
        for (a, t), o, n in rows:
            print(f"- **{a} / {t}** — {o:.1f} → {n:.1f} ({n - o:+.1f})")
            if with_quotes:
                for q in quotes[(a, t)][:2]:
                    print(f"  > \"{q}\"")
    if not any(rows for _, rows, _ in sections):
        print("\n(no movement above thresholds)")


def main():
    ap = argparse.ArgumentParser(description="Score unbundling opportunities from review_tags")
    ap.add_argument("--db", default="reviews.db")
    ap.add_argument("--app", help="restrict to one app_slug")
    ap.add_argument("--heatmap", action="store_true")
    ap.add_argument("--top", type=int, metavar="N", help="top N themes with quotes")
    ap.add_argument("--trend", action="store_true",
                    help="yearly share series per theme×app (the arrows' data)")
    ap.add_argument("--json", action="store_true", help="dump scored metrics as JSON")
    ap.add_argument("--snapshot", action="store_true",
                    help="persist current scores for later --diff")
    ap.add_argument("--diff", nargs="*", type=int, metavar="ID",
                    help="diff two snapshots (default: the latest two); "
                         "combines with --json")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='review_tags'").fetchone():
        sys.exit("no review_tags table yet — run tagging first (tag_reviews.py)")
    tags = load(conn, args.app)
    if not tags:
        sys.exit("no tags found — run tag_reviews.py --import-batch first")
    today = newest_review_date(conn)
    scored = score_apps(tags, today)

    if args.snapshot or args.diff is not None:
        if args.snapshot:
            cmd_snapshot(conn, scored, tags)
        if args.diff is not None:
            cmd_diff(conn, args.diff, tags, args.json)
    elif args.json:
        print(json.dumps(scored, ensure_ascii=False, indent=2))
    elif args.heatmap:
        print_heatmap(scored)
    elif args.trend:
        print_trend(tags, today)
    elif args.top:
        print_top(scored, tags, args.top)
    else:
        print_tables(scored)
    conn.close()


if __name__ == "__main__":
    main()
