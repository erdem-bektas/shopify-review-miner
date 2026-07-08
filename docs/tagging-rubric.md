# Review Tagging Rubric

Turns raw Shopify App Store reviews into structured **opportunity signals**. The
end goal is an *unbundling* search: which structural, feature-shaped weaknesses
of a big incumbent app could become a standalone product?

This file is the single source of truth for how a review becomes tags. Both the
human-in-the-loop pass and every tagging subagent follow it verbatim, so tags
stay consistent across batches.

## Core principle: not every complaint is an opportunity

- **Gold** — feature-shaped gaps ("can't build X in the flow editor",
  "segmentation can't do Y") and explicit wishes from otherwise-happy users
  ("love it, *but I wish*..."). These isolate a capability someone could build.
- **Not opportunities** (but still tagged, for contrast and scoring) — "support
  is slow" (`service`), "too expensive" (`pricing`). They describe the vendor's
  operations, not a product you can ship.
- A 5★ "love it, but I wish it had Z" is often **more** valuable than a 1★ rant:
  it isolates one missing capability from an engaged, paying user.

## One review → one or more tags

Emit one tag per distinct **(theme, kind) claim**. A review that praises
deliverability but wishes for better segmentation yields TWO rows:
`(deliverability, praise)` and `(segmentation, feature_request)`. Don't collapse
multi-theme reviews into one row, and don't invent themes a review doesn't raise.
Aim for 1–3 tags per review; more only if it genuinely raises that many points.
**Every processed review gets ≥1 tag** (even pure praise → one `praise` row) so
the pipeline can tell "tagged, nothing actionable" from "not yet tagged".

## Fields (per tag)

| field | values | notes |
|-------|--------|-------|
| `theme` | controlled vocab below | one value |
| `kind` | controlled vocab below | one value |
| `churn_signal` | `true`/`false` | leaving/switching intent or a done switch |
| `switched_to` | competitor name or `null` | only if a rival is named |
| `quote` | ≤200 chars, **verbatim** substring | the evidence for THIS claim |
| `confidence` | `high`/`medium`/`low` | low when unsure — never fabricate |
| `vendor_ack` | vocab below | what the `dev_reply` says about THIS claim |

## `theme` — controlled vocabulary (email/SMS marketing domain)

| theme | covers |
|-------|--------|
| `deliverability` | inbox placement, spam foldering, sender reputation, bounces, DKIM/SPF/DNS |
| `flows_automation` | automation flows, triggers, conditional splits, the flow editor/builder, abandoned-cart/welcome series |
| `segmentation` | building segments, filters, dynamic vs static lists, audience targeting, suppression |
| `data_management` | bulk profile operations (mass delete/merge/cleanup), backup & restore, data export/import hygiene, GDPR/deletion, duplicate handling, controlling the underlying profile data |
| `templates_editor` | email/template design editor, drag-and-drop builder, template library, content editing |
| `reporting_analytics` | dashboards, metrics, revenue attribution, A/B-test analytics, exports |
| `integrations_sync` | Shopify/third-party sync, product feed, API, webhooks, data flowing in/out |
| `sms` | SMS/MMS sending, SMS compliance, credits/SMS pricing mechanics, text campaigns |
| `forms_popups` | signup forms, popups, embeds, form builder |
| `pricing_billing` | cost, plan tiers, profile-based pricing, overages, price hikes, billing loops |
| `support` | support quality/speed, account managers, docs, onboarding *help* (the service, not the product) |
| `onboarding_migration` | getting started, learning curve, migrating **from/to** another tool, initial import/setup |
| `performance_bugs` | app slowness, crashes, downtime, glitches not tied to one feature area |
| `other` | genuinely doesn't fit above |

Pick the **most specific** theme. A broken sync belongs to `integrations_sync`
(kind `bug`), not `performance_bugs`. `performance_bugs` is for cross-cutting
instability. Use `other` sparingly.

**`segmentation` vs `data_management`**: building/filtering an audience is
`segmentation`; maintaining, deleting, exporting, or restoring the underlying
profile data is `data_management`. "can't mass-delete profiles", "no backup /
restore", "had to rebuild my whole list" → `data_management`. When a user
deletes profiles specifically to lower a profile-based bill, tag BOTH
`data_management` (the missing bulk-op, `feature_gap`) and `pricing_billing`
(the billing pain, `pricing`) — the coupling is the whole point.

## `kind` — controlled vocabulary

| kind | meaning |
|------|---------|
| `feature_gap` | a capability that **does not exist / isn't supported by design**. "there's no way to…", "doesn't support…", "can't…". The strongest raw material. |
| `feature_request` | an **explicit** ask or wish. "wish", "would be great if", "please add", "only thing missing", "hope they add". |
| `service` | support / account-management quality, good or bad. Not a product. |
| `pricing` | cost, tiers, profile-based billing, overages, hikes, billing loops. Not a product (usually). |
| `bug` | an existing capability is **broken/malfunctioning** (sync fails, crashes, wrong counts). |
| `praise` | positive, no actionable gap. |

**`feature_gap` vs `bug`** (matters for scoring — gaps are structural, bugs get
fixed): "segments don't support nested AND/OR logic" = `feature_gap` (never
existed). "my segment shows 0 people when it should show 400" = `bug` (existing
feature broken). Both under `segmentation`, different `kind`.

**`feature_gap` vs `feature_request`**: gap = the reviewer describes the missing
capability as a limitation they hit; request = the reviewer explicitly asks for
it. When both are present, prefer `feature_request` (explicit signal is stronger).

## Special rules

1. **4–5★ mining.** Scan every high-rated review for `but`, `wish`, `only
   complaint`, `would be great if`, `the one thing`, `if only`, `just needs`.
   These are `feature_request` candidates and the highest-value signal — do not
   let the high rating hide the request. A 5★ with a wish still gets a
   `feature_request` row (plus a `praise` row if warranted).
2. **Churn rigor.** `usage_duration` ≥ ~1 year **and** a low rating → weigh
   `churn_signal` carefully. Set `true` when the reviewer states intent to leave,
   is actively evaluating alternatives, or has already left: "looking hard to get
   out", "moving to X", "cancelled and switched". Long-tenured churn is the
   costliest signal — a user who paid for years then left.
3. **`switched_to`.** If a competitor is named as a destination or the
   alternative being weighed, record it (the competitor's name exactly as written
   in the review). `null` otherwise. Never guess a name.
4. **Stale promises.** If a `dev_reply` promises a fix ("on our roadmap",
   "coming soon", "we're working on it") **and the review is old**, tag the
   promised feature's theme as `feature_gap` and put the promise (with its year)
   in the quote, prefixed `[dev]: `. A years-unfulfilled promise is strong
   evidence of structural inability.
5. **Uncertainty.** When unsure, `confidence: low`. Never fabricate a `quote` or
   a `switched_to`. The quote must be an actual substring of the review body
   (or of `dev_reply`, prefixed `[dev]: `).

## `vendor_ack` — what the dev reply admits

Judged **per tag**, from `dev_reply` only. A reply that addresses one claim but
ignores another gets different values on the two tags.

| value | meaning |
|-------|---------|
| `none` | no dev reply, or the reply doesn't address this claim (generic apology, "contact support") |
| `acknowledged` | the reply admits the limitation/problem but commits to nothing |
| `roadmap` | the reply says it's planned / being worked on / coming — the strongest open-gap evidence |
| `shipped` | the reply says the capability now exists or was released — the gap may be closed (staleness warning) |
| `disputed` | the reply claims the feature already exists or the reviewer erred |

Always emit the field. Combine with special rule 4 (stale promises): an old
review with `vendor_ack: roadmap` and no later `shipped` evidence is a
years-unfulfilled promise.

## `quote` rules

- Verbatim substring, ≤200 chars, the single most evidence-bearing fragment for
  THIS tag's claim. Trim to the sentence that carries the gap/wish/switch.
- Don't quote generic filler ("great app!") on a `feature_request` tag — quote
  the wish itself.

## `confidence`

- `high` — claim is explicit and unambiguous.
- `medium` — reasonable inference from clear context.
- `low` — guess / sparse text / ambiguous. Better low than wrong.

## Output JSON

A JSON array (or `{"tags": [...]}`) of tag objects:

```json
{"review_id": "...", "source": "shopify", "app_slug": "some-app-slug",
 "theme": "segmentation", "kind": "feature_request", "churn_signal": false,
 "switched_to": null, "quote": "would be amazing if segments could nest AND/OR",
 "confidence": "high", "vendor_ack": "none"}
```

Every `review_id` in the batch must appear in ≥1 tag. Omit no review.

## Worked examples

1. **1★, ~6 years, pricing rant, churn** — "my monthly price went from 80usd to
   150usd… I am looking very hard to get out, and never come back."
   → `(pricing_billing, pricing, churn=true, switched_to=null, conf=high)`.
   *Not* an opportunity theme, but the churn + tenure matter for the score's
   seniority/persistence signals.

2. **1★, ~1 year, sync broken** — "created a segment in Shopify to import to
   the app and it shows up with ZERO people on it." Dev reply offers help.
   → `(integrations_sync, bug, churn=false, conf=high)`. A bug, not a gap — but
   if this recurs across years it hints at a structural sync weakness.

3. **4★, over 1 year** — "I have some difficulties with certain things, but
   overall really easy to use and support is responsive."
   → `(support, praise, conf=high)`. The "difficulties" are too vague to be a
   feature tag → don't invent a theme; `confidence: low` if you must, or skip
   the vague part. Keep the praise row.

4. **5★, "love it but wish"** — "Best email tool we've used. Only wish: no way
   to A/B test the *timing* of a flow, just the content."
   → `(templates_editor, praise, conf=medium)` **and**
   `(flows_automation, feature_request, quote="no way to A/B test the timing of
   a flow, just the content", conf=high)`. The second row is the gold.
