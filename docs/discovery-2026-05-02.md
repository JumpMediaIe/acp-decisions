# Discovery: real ACP page + PDF structure (2026-05-02)

The original spec/plan made selector and URL assumptions that didn't survive
contact with the live site. This memo records what's actually there. Tasks 7–10
of the plan need rewriting against these findings.

## URL structure

- Pattern: `https://www.pleanala.ie/en-ie/case/{numericId}` (e.g. `319750`).
- Old `/case/PL06F.249482` URLs return 404 — the canonical-reference URL form
  was retired.
- Listings: `https://www.pleanala.ie/en-ie/cases?type={CODE}` with codes
  observed: `LH` (large housing / LRD), `PA` (planning appeal). Server-rendered
  with all `<a href="/en-ie/case/{id}">` links inline, so HTML scraping works.
- `case-search` is a JS-driven SPA — not useful without a browser engine. The
  type-filtered listings are the right entry point.

## Three different reference formats per case

| Where | Format | Example |
|---|---|---|
| URL path | numeric only | `315183` |
| Page header `<h3 class="section-title">` | `{authorityCode}.{numericId}` | `LH02.319750`, `PA05E.300460`, `PA09.300506` |
| Inside Order PDF | `ABP-{numericId}-{yy}` | `ABP-315183-22` |

The ABP form is the canonical reference used in legal/news contexts. Schema
should store all three; index on the ABP form for human lookup.

## HTML page layout

Not a definition list. Foundation grid pattern:

```html
<div class="grid-x grid-padding-x">
  <div class="medium-3 cell"><p class="case-sub">FieldName</p></div>
  <div class="medium-9 cell"><p class="case-summary">FieldValue</p></div>
</div>
```

Stable extractable fields: `Description`, `Case type`, `Decision`, `Date signed`,
`EIAR`, `NIS`, `Parties`, `History`, `Documents`. Plus `<p class="address">` and
`<p class="council">` outside the grid.

Map iframe (ArcGIS) sits inside the page but isn't needed — we already have
GIS in irish-planning-tool.

## Decision outcomes — free text, no inline enum

Real values seen across 13 sampled cases:

- `Grant permission with conditions`
- `Grant permission with revised conditions`
- `Grant Permissions with Conditions` (capitalisation drift)
- `Grant Perm. w   Conditions` (older abbreviated form)
- `Refuse Permission`
- `Refuse permission`
- `Refuse Perm.`
- `1st Refuse permission`
- `Contribution Appeal Decided`
- `Invalid`

Original spec's enum (`refused/granted/granted_with_conditions/set_aside/withdrawn/refused_after_review`)
needs broadening AND a normalisation layer that maps free text → canonical
bucket. Store both: `decision_outcome_raw` (verbatim) and `decision_outcome`
(normalised).

## Reasons for refusal — only in PDFs

There is **no** "Reasons and Considerations" block in the case HTML. Reasons live
in the linked Order PDF (`d{id}.pdf`, sometimes `d{id}(a).pdf` etc. for
multi-part orders).

PDF structure varies:

**Short order (5 pp.):** clean numbered list straight after the header.

```
Reasons and Considerations
1. Having regard to the 29 zoning... [paragraphs]
2. Objective CU025 of the Dublin City Development Plan... [paragraphs]
3. Having regard to the submitted Natura Impact Statement...
```

**Long order (13 pp.):** the "Reasons and Considerations" header is followed
first by extensive legislative/policy context (bulleted), then by numbered
reasons much later. Parser must search past the bullet list.

Encoding pitfalls in extracted text:
- Curly quotes mojibake to `�` (single Unicode replacement char). Need a
  normalise step that replaces with straight quotes.
- Headers/footers like `ABP-XXXXXX-YY An Coimisiún Pleanála Page X of Y` and
  `Page X of Y` interleave into reason text on page boundaries — strip with a
  regex pass before parsing reasons.
- Numbered items: usually `1.`, `2.` — but `3 Having` (no period) seen.

## PDF link discovery

Always parse `<a href>` links from the case page. Don't construct PDF URLs from
the case ID — the path can have suffixes (`d300506(a).pdf`, `d300506(b).pdf`)
when the order is split.

PDF categories observed:
- `orders/{group}/d{id}.pdf` — Board Order (contains decision + reasons)
- `reports/{group}/r{id}.pdf` — Inspector's Report (longer, full analysis)
- `directions/{group}/s{id}.pdf` — Direction
- `bmr/{group}/b{id}.pdf` — Meeting Records

For our refusal-reason analytics we only need the **Order**. Inspector Report
is Phase 2 if we later want richer data.

## Schema deltas (vs. current schema.sql)

Add to `decisions`:

- `case_id_url INTEGER` — numeric URL ID, the natural scrape key
- `abp_reference TEXT` — canonical `ABP-XXXXX-YY`
- `pa_reference TEXT` — page-header form (`LH02.319750`)
- `decision_outcome_raw TEXT` — verbatim free-text
- `case_type_raw TEXT` — `Appeal - LRD`, `Private Development - Application`, etc.

Rename: existing `reference` → make it ALIAS for whichever we settle on as the
external display key (probably `abp_reference`).

New table `documents` (one row per PDF link) so we keep all PDF URLs even when
we only parse the Order:

```sql
CREATE TABLE documents (
  id INTEGER PRIMARY KEY,
  case_id_url INTEGER NOT NULL REFERENCES decisions(case_id_url) ON DELETE CASCADE,
  doc_type TEXT NOT NULL,        -- 'order' | 'inspector_report' | 'direction' | 'bmr' | 'other'
  url TEXT NOT NULL,
  fetched_at TEXT
);
```

## Plan deltas (Tasks 7+)

- **Task 7** rewritten: parse the case HTML page → `Decision` (metadata only) +
  `list[DocumentLink]`. Tested against the three saved fixtures.
- **NEW Task 7b**: PDF Order parser. Tested against the two saved Order PDFs.
  Returns `list[RefusalReason]`. Handles both short-form (numbered list right
  after header) and long-form (legislative preamble before reasons) layouts.
- **NEW Task 7c**: decision-outcome normaliser — free text → canonical bucket
  (`granted`, `granted_with_conditions`, `refused`, `withdrawn`, `invalid`,
  `procedural`).
- **Task 8** (DB upsert): unchanged in shape; works against the extended
  schema.
- **Task 9** (walker): walks `/en-ie/cases?type=LH`, `?type=PA`, … (we'll
  enumerate the type codes empirically). Each listing is server-rendered HTML
  with all case links inline, so a single GET per listing page is enough — no
  pagination by date needed for the type-filtered views observed so far.
- **Task 10** (orchestrator): for each case → fetch HTML → upsert metadata +
  documents → if refused, fetch Order PDF and parse reasons.
- Tasks 11–15 unchanged in spirit but the CLI command surface stays the same.

## Fixtures saved

- `tests/fixtures/case_granted_lrd_319750.html`
- `tests/fixtures/case_refused_lrd_315183.html`
- `tests/fixtures/case_refused_pa_300506.html`
- `tests/fixtures/order_refused_lrd_315183.pdf` (5 pp., clean numbered list)
- `tests/fixtures/order_refused_pa_300506_a.pdf` (13 pp., legislative preamble)
