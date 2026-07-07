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

    # or hands-off: the same loop driven through `claude -p` (rubric = prompt)
    uv run tag_reviews.py --auto -n 40

    # mine weakly-tagged reviews for themes the rubric might be missing
    uv run tag_reviews.py --discover

Schema additions are backward compatible: a new `review_tags` table plus indexes,
nothing touched in `reviews`/`apps`, so the existing UI keeps working.
"""

import argparse
import json
import random
import re
import shlex
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

THEMES = {
    "deliverability", "flows_automation", "segmentation", "data_management",
    "templates_editor", "reporting_analytics", "integrations_sync", "sms",
    "forms_popups", "pricing_billing", "support", "onboarding_migration",
    "performance_bugs", "other",
}
KINDS = {"feature_gap", "feature_request", "service", "pricing", "bug", "praise"}
CONFIDENCE = {"high", "medium", "low"}
# what the dev_reply says about THIS tag's claim (see docs/tagging-rubric.md);
# rows tagged before this field existed stay NULL — "untracked", not "none"
VENDOR_ACK = {"none", "acknowledged", "roadmap", "shipped", "disputed"}

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
    vendor_ack    TEXT,
    tagged_at     TEXT NOT NULL,
    FOREIGN KEY (source, app_slug, review_id)
        REFERENCES reviews (source, app_slug, review_id)
);
CREATE INDEX IF NOT EXISTS idx_tags_review ON review_tags (source, app_slug, review_id);
CREATE INDEX IF NOT EXISTS idx_tags_theme  ON review_tags (app_slug, theme, kind);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # migrate tables created before vendor_ack existed (old rows stay NULL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(review_tags)")}
    if "vendor_ack" not in cols:
        conn.execute("ALTER TABLE review_tags ADD COLUMN vendor_ack TEXT")
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
    raise ValueError("tags must be a JSON array or {\"tags\": [...]}")


def validate_tags(tags: list, valid_ids: set) -> tuple[list[dict], list[str]]:
    """Controlled-vocab validation shared by --import-batch and --auto.

    Returns (rows ready to insert, problem messages for skipped tags).
    Auxiliary fields degrade instead of discarding the tag: an unknown
    confidence becomes 'low', an unknown vendor_ack becomes NULL.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict] = []
    problems_out: list[str] = []
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
        ack = t.get("vendor_ack")
        if ack is not None and ack not in VENDOR_ACK:
            ack = None
        if problems:
            problems_out.append(f"tag #{i}: {'; '.join(problems)}")
            continue
        quote = (t.get("quote") or None)
        if quote:
            quote = quote.strip()[:240]
        rows.append({
            "source": src, "app_slug": slug, "review_id": rid, "theme": theme,
            "kind": kind, "churn_signal": 1 if t.get("churn_signal") else 0,
            "switched_to": (t.get("switched_to") or None), "quote": quote,
            "confidence": conf, "vendor_ack": ack, "tagged_at": now,
        })
    return rows, problems_out


def import_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Idempotent insert: replace all tags of every review present in `rows`."""
    touched = {(r["source"], r["app_slug"], r["review_id"]) for r in rows}
    for key in touched:
        conn.execute("DELETE FROM review_tags WHERE source=? AND app_slug=? AND review_id=?", key)
    conn.executemany(
        "INSERT INTO review_tags (source, app_slug, review_id, theme, kind, "
        "churn_signal, switched_to, quote, confidence, vendor_ack, tagged_at) VALUES "
        "(:source, :app_slug, :review_id, :theme, :kind, :churn_signal, "
        ":switched_to, :quote, :confidence, :vendor_ack, :tagged_at)", rows)
    conn.commit()
    return len(touched)


def valid_review_ids(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("SELECT review_id FROM reviews")}


def cmd_import(conn: sqlite3.Connection, path: str) -> None:
    with open(path, encoding="utf-8") as f:
        try:
            tags = _iter_tags(json.load(f))
        except ValueError as e:
            raise SystemExit(str(e))
    rows, problems = validate_tags(tags, valid_review_ids(conn))
    for p in problems:
        print(f"  skip {p}", file=sys.stderr)
    n_reviews = import_rows(conn, rows)
    print(f"imported {len(rows)} tags across {n_reviews} reviews"
          f"{f' ({len(problems)} skipped)' if problems else ''}", file=sys.stderr)


OUTPUT_CONTRACT = """\
---
You are tagging the app reviews below. Follow the rubric above EXACTLY.
Return ONLY a JSON array of tag objects — no prose, no markdown fences.
Each object has the fields: review_id, source, app_slug, theme, kind,
churn_signal, switched_to, quote, confidence, vendor_ack.
Every review_id in the batch must appear in at least one tag.
The reviews to tag (JSON):
"""


def rubric_text() -> str:
    path = Path(__file__).resolve().parent / "docs" / "tagging-rubric.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"rubric not found at {path} — --auto needs it as the prompt")


def _strip_fences(s: str) -> str:
    s = s.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.S)
    return m.group(1) if m else s


def run_claude(prompt: str, claude_args: str, timeout: int = 900) -> str:
    cmd = ["claude", "-p", *shlex.split(claude_args)]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        raise SystemExit("`claude` CLI not found — --auto drives tagging "
                         "through Claude Code's print mode")
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exited {proc.returncode}: "
                           f"{proc.stderr.strip()[-500:]}")
    return proc.stdout


def cmd_auto(conn: sqlite3.Connection, args) -> None:
    """Drive the export → LLM → validate → import loop with `claude -p`.

    The rubric file IS the prompt (no duplicated instructions here). A batch
    that fails to parse/validate is retried once with the errors appended,
    then quarantined; its reviews are skipped for the rest of the run so a
    poisoned batch can never spin the loop.
    """
    rubric = rubric_text()
    valid = valid_review_ids(conn)
    quarantine = Path("exports") / "quarantine"  # already git-ignored + hook-denylisted
    skip: set = set()
    batches = total_tags = 0

    while batches < args.max_batches:
        rows = [r for r in fetch_untagged(conn, args.app)
                if r["review_id"] not in skip]
        if not rows:
            break
        batch = rows[:args.n]
        batch_ids = {r["review_id"] for r in batch}
        prompt = (rubric + "\n" + OUTPUT_CONTRACT
                  + json.dumps(batch, ensure_ascii=False, indent=1))

        if args.dry_run:
            print(f"dry-run: would send {len(batch)} reviews "
                  f"({len(prompt):,} chars) to `claude -p {args.claude_args}` "
                  f"— up to {args.max_batches} batches, {len(rows)} untagged now",
                  file=sys.stderr)
            return

        batches += 1
        error = None
        last_raw = ""
        imported_rows = None
        for attempt in (1, 2):
            p = prompt if attempt == 1 else (
                prompt + f"\n---\nYour previous output was invalid "
                         f"({error}). Return ONLY the corrected JSON array.")
            try:
                last_raw = run_claude(p, args.claude_args)
                tags = _iter_tags(json.loads(_strip_fences(last_raw)))
                rows_v, problems = validate_tags(tags, valid)
                if not rows_v:
                    raise ValueError("; ".join(problems[:5]) or "no valid tags")
                imported_rows = rows_v
                if problems:
                    print(f"  batch {batches}: {len(problems)} tag(s) skipped: "
                          f"{'; '.join(problems[:3])}", file=sys.stderr)
                break
            except (RuntimeError, ValueError, json.JSONDecodeError,
                    subprocess.TimeoutExpired) as e:
                error = str(e)[:500]

        if imported_rows is None:
            quarantine.mkdir(parents=True, exist_ok=True)
            (quarantine / f"batch-{batches:03d}.json").write_text(
                json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
            (quarantine / f"batch-{batches:03d}.response.txt").write_text(
                f"error: {error}\n\n{last_raw}", encoding="utf-8")
            skip |= batch_ids
            print(f"  batch {batches}: FAILED twice ({error}) — quarantined to "
                  f"{quarantine}/batch-{batches:03d}.*", file=sys.stderr)
            continue

        n_reviews = import_rows(conn, imported_rows)
        total_tags += len(imported_rows)
        uncovered = batch_ids - {r["review_id"] for r in imported_rows}
        if uncovered:
            # legitimate but incomplete output: retry these next RUN, not now
            skip |= uncovered
            print(f"  batch {batches}: {len(uncovered)} review(s) left untagged "
                  f"by the model — will retry on the next run", file=sys.stderr)
        remaining = len(rows) - len(batch)
        print(f"  batch {batches}: {len(imported_rows)} tags across {n_reviews} "
              f"reviews, ~{remaining} untagged left", file=sys.stderr)

    if batches >= args.max_batches:
        print(f"stopped at --max-batches {args.max_batches}", file=sys.stderr)
    print(f"auto-tagging done: {total_tags} tags in {batches} batch(es)"
          f"{f', {len(skip)} review(s) skipped/quarantined' if skip else ''}",
          file=sys.stderr)


DISCOVER_PROMPT = """\
You are mining app reviews for THEME DISCOVERY.
The controlled theme vocabulary is: {themes}.
The reviews below were tagged only 'other' or only with low confidence — the
vocabulary may be missing a theme. Propose up to 5 CANDIDATE themes NOT in the
vocabulary that would cover recurring, feature-shaped topics in these reviews.
Return ONLY a JSON array (no prose, no fences):
[{{"name": "snake_case_name", "definition": "one line",
   "quotes": [{{"review_id": "...", "quote": "verbatim substring"}}],
   "n_reviews": 3}}]
Return [] if nothing recurs. The reviews (JSON):
"""

MERGE_PROMPT = """\
Merge these candidate-theme proposals from separate batches: combine
near-duplicates, keep at most 8, prefer candidates with more evidence.
Return ONLY the merged JSON array in the same schema — no prose, no fences.
"""


def cmd_discover(conn: sqlite3.Connection, args) -> None:
    """Propose candidate NEW themes from weakly-tagged reviews — report only.

    Never touches the db or THEMES: adopting a candidate is a human edit to
    docs/tagging-rubric.md and the THEMES set here.
    """
    tagref = ("t.source = r.source AND t.app_slug = r.app_slug "
              "AND t.review_id = r.review_id")
    where = " AND r.app_slug = ?" if args.app else ""
    qargs = [args.app] if args.app else []
    rows = conn.execute(
        f"""SELECT r.app_slug, r.review_id, r.rating, r.review_date, r.body
            FROM reviews r
            WHERE EXISTS (SELECT 1 FROM review_tags t WHERE {tagref})
              AND (NOT EXISTS (SELECT 1 FROM review_tags t
                               WHERE {tagref} AND t.theme <> 'other')
                OR NOT EXISTS (SELECT 1 FROM review_tags t
                               WHERE {tagref} AND t.confidence <> 'low'))
              {where}""", qargs).fetchall()
    if not rows:
        print("nothing to mine: no reviews tagged only-'other' or only-low-"
              "confidence", file=sys.stderr)
        return
    reviews = [{"app_slug": a, "review_id": rid, "rating": rat,
                "review_date": d, "body": (b or "")[:800]}
               for a, rid, rat, d, b in rows]
    if args.dry_run:
        print(f"dry-run: would mine {len(reviews)} weakly-tagged reviews in "
              f"batches of {args.n} (max {args.max_batches})", file=sys.stderr)
        return

    themes_list = ", ".join(sorted(THEMES))
    candidates: list[dict] = []
    batches = 0
    for i in range(0, len(reviews), args.n):
        if batches >= args.max_batches:
            print(f"stopped at --max-batches {args.max_batches}", file=sys.stderr)
            break
        batches += 1
        prompt = (DISCOVER_PROMPT.format(themes=themes_list)
                  + json.dumps(reviews[i:i + args.n], ensure_ascii=False, indent=1))
        data, err = None, None
        for attempt in (1, 2):
            try:
                data = json.loads(_strip_fences(run_claude(prompt, args.claude_args)))
                break
            except (RuntimeError, ValueError, json.JSONDecodeError,
                    subprocess.TimeoutExpired) as e:
                err = str(e)[:200]
        if data is None:
            print(f"  batch {batches} failed twice ({err}) — continuing",
                  file=sys.stderr)
            continue
        if isinstance(data, dict):
            data = data.get("candidates", [])
        fresh = [c for c in data if isinstance(c, dict)
                 and c.get("name") and c["name"] not in THEMES]
        candidates += fresh
        print(f"  batch {batches}: {len(fresh)} candidate(s)", file=sys.stderr)
    if not candidates:
        print("no candidate themes proposed", file=sys.stderr)
        return

    if batches > 1 and len(candidates) > 1:
        try:
            merged = json.loads(_strip_fences(run_claude(
                MERGE_PROMPT + json.dumps(candidates, ensure_ascii=False),
                args.claude_args)))
            if isinstance(merged, list) and merged:
                candidates = [c for c in merged if isinstance(c, dict)
                              and c.get("name")]
        except (RuntimeError, ValueError, json.JSONDecodeError,
                subprocess.TimeoutExpired):
            print("  merge pass failed — keeping raw candidates", file=sys.stderr)

    out = Path("exports") / "theme-candidates.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    lines = [f"# Candidate themes — {today}", "",
             f"Mined from {len(reviews)} weakly-tagged reviews. Adopting a "
             f"candidate is a manual edit to docs/tagging-rubric.md and the "
             f"THEMES set in tag_reviews.py — nothing is automatic.", ""]
    for c in candidates:
        lines += [f"## {c['name']}", "", str(c.get("definition", "")).strip(), ""]
        for q in (c.get("quotes") or [])[:3]:
            if isinstance(q, dict):
                lines.append(f"- \"{q.get('quote', '')}\" ({q.get('review_id', '?')})")
        if c.get("n_reviews"):
            lines.append(f"- ~{c['n_reviews']} reviews")
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"{len(candidates)} candidate theme(s) → {out}", file=sys.stderr)


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
    acks = conn.execute(
        f"SELECT vendor_ack, COUNT(*) FROM review_tags "
        f"WHERE vendor_ack IS NOT NULL{' AND app_slug=?' if app else ''} "
        f"GROUP BY 1 ORDER BY 2 DESC", args).fetchall()
    if acks:
        print("  vendor ack: " + " · ".join(f"{a} {c}" for a, c in acks))


def main() -> None:
    ap = argparse.ArgumentParser(description="Export/import review tags for reviews.db")
    ap.add_argument("--db", default="reviews.db")
    ap.add_argument("--export", action="store_true", help="export untagged reviews as JSON")
    ap.add_argument("--import-batch", dest="import_batch", metavar="FILE",
                    help="import tags from a JSON file")
    ap.add_argument("--stats", action="store_true", help="show tagging progress")
    ap.add_argument("--auto", action="store_true",
                    help="tag everything hands-off: export → `claude -p` with the "
                         "rubric → validate → import, in batches of -n "
                         "(25–50 recommended) until no untagged reviews remain")
    ap.add_argument("--discover", action="store_true",
                    help="mine weakly-tagged reviews for candidate NEW themes "
                         "→ exports/theme-candidates.md (report only, no db writes)")
    ap.add_argument("--max-batches", type=int, default=40,
                    help="hard cap on LLM invocations per --auto/--discover run "
                         "(default 40)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --auto: show what would be sent, invoke nothing")
    ap.add_argument("--claude-args", default="", metavar="ARGS",
                    help='extra args for the claude CLI, e.g. "--model claude-haiku-4-5"')
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
    elif args.auto:
        cmd_auto(conn, args)
    elif args.discover:
        cmd_discover(conn, args)
    elif args.stats:
        cmd_stats(conn, args.app)
    else:
        ap.error("one of --export / --import-batch / --auto / --discover / "
                 "--stats is required")
    conn.close()


if __name__ == "__main__":
    main()
