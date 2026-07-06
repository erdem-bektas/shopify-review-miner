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
from collections import defaultdict
from datetime import date

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
    rows = conn.execute(
        f"SELECT t.app_slug, t.theme, t.kind, t.review_id, r.review_date, "
        f"       r.usage_duration, t.churn_signal, t.switched_to, t.quote, "
        f"       t.confidence, r.rating, r.shop_name "
        f"FROM review_tags t JOIN reviews r ON r.source=t.source "
        f"  AND r.app_slug=t.app_slug AND r.review_id=t.review_id {where}", args)
    cols = ["app_slug", "theme", "kind", "review_id", "review_date", "usage_duration",
            "churn_signal", "switched_to", "quote", "confidence", "rating", "shop_name"]
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

            if len(feat_reviews) < MIN_FREQ_REVIEWS or feature_shape == 0:
                app_out[theme] = _metrics(theme, tt, feat, feat_reviews,
                                          0.0, 0.0, 0.0, 0.0, feature_shape,
                                          n_service, n_pricing, n_bug, n_praise, None, None, 0.0)
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
                dates[-1].isoformat() if dates else None, avg_usage)
        out[app] = app_out
    return out


def _metrics(theme, tt, feat, feat_reviews, freq, pers, sen, score, fshape,
             n_service, n_pricing, n_bug, n_praise, first, last, avg_usage):
    return {
        "theme": theme, "score": round(score, 1),
        "frequency": round(freq, 3), "persistence": round(pers, 3),
        "seniority": round(sen, 3), "feature_shape": round(fshape, 3),
        "n_feature_reviews": len(feat_reviews), "n_tags": len(tt),
        "n_service": n_service, "n_pricing": n_pricing, "n_bug": n_bug,
        "n_praise": n_praise, "first_seen": first, "last_seen": last,
        "avg_usage_years": round(avg_usage, 2),
        "n_churn": sum(t["churn_signal"] for t in tt),
    }


def print_tables(scored):
    for app in sorted(scored):
        themes = [m for m in scored[app].values() if m["score"] > 0]
        themes.sort(key=lambda m: -m["score"])
        if not themes:
            continue
        print(f"\n### {app}")
        print(f"  {'theme':<22}{'score':>6}{'freq':>6}{'pers':>6}{'seny':>6}"
              f"{'fshp':>6}{'#feat':>6}{'span':>18}{'chrn':>5}")
        for m in themes:
            span = f"{m['first_seen'] or '?'}→{m['last_seen'] or '?'}"
            print(f"  {m['theme']:<22}{m['score']:>6.1f}{m['frequency']:>6.2f}"
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
            s = scored[a].get(th, {}).get("score", 0)
            cells.append(f"{s:>11.0f}" if s else f"{'·':>11}")
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
        print(f"\n═══ {app} / {m['theme']}  —  score {m['score']}")
        print(f"    freq {m['frequency']} · persistence {m['persistence']} "
              f"({m['first_seen']}→{m['last_seen']}) · seniority {m['seniority']} "
              f"(~{m['avg_usage_years']}y) · feature-shape {m['feature_shape']}")
        print(f"    {m['n_feature_reviews']} feature-shaped reviews, "
              f"{m['n_churn']} churn signals")
        picks = sorted(quotes[(app, m["theme"])],
                       key=lambda t: (t["confidence"] != "high", t["kind"]))[:5]
        for t in picks:
            flag = " ⚠churn" if t["churn_signal"] else ""
            to = f" →{t['switched_to']}" if t["switched_to"] else ""
            print(f"      • [{t['kind']}{to}{flag}] \"{t['quote']}\"")


def main():
    ap = argparse.ArgumentParser(description="Score unbundling opportunities from review_tags")
    ap.add_argument("--db", default="reviews.db")
    ap.add_argument("--app", help="restrict to one app_slug")
    ap.add_argument("--heatmap", action="store_true")
    ap.add_argument("--top", type=int, metavar="N", help="top N themes with quotes")
    ap.add_argument("--json", action="store_true", help="dump scored metrics as JSON")
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

    if args.json:
        print(json.dumps(scored, ensure_ascii=False, indent=2))
    elif args.heatmap:
        print_heatmap(scored)
    elif args.top:
        print_top(scored, tags, args.top)
    else:
        print_tables(scored)
    conn.close()


if __name__ == "__main__":
    main()
