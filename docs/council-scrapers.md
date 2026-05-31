# Per-council refusal-reason scrapers

The LGMA national dataset gives us the binary "REFUSED" status for every Irish
planning application, plus a `link_app_details` URL where one exists. It does
not give us **why** each application was refused — that lives in each council's
own document portal.

This doc records, for each council we've backfilled, where the reasons live
and how the scraper extracts them. Read it before adding another council so the
right script gets reused — most new councils now need **no new code**, just a
run of the generic iDocs scraper.

## Portal families

Every council we've done falls into one of these buckets:

| Portal family | Used by | Doc structure | Auth | Script |
|---|---|---|---|---|
| **agileapplications.ie** | Cork County, Cork City, South Dublin, Fingal, Wexford, Dun Laoghaire, Dublin City | Structured JSON API (prescriptionCode='R') — no PDFs | None | `fetch-council-reasons.py` |
| **iDocsWeb / iDocsWebDPSS** (generic) | Kildare, Meath, Wicklow, Louth, Kerry, Waterford, Limerick, Mayo, Kilkenny, Clare, Westmeath, Roscommon, Galway City, Offaly, Tipperary, Cavan, Laois, Carlow, Sligo, Longford, Leitrim | PDF/DjVu — decision doc reached via `listFiles → ViewFiles → files/<uuid>` chain | Session cookie from the initial `listFiles.aspx` hit | **`fetch-idocs-reasons.py`** (parametrised; the workhorse) |
| **apps.galwaycoco.ie/ViewExternalDocuments** | Galway County | PDF — reasons after "SCHEDULE REFERRED TO" / "for the reason(s) set out hereunder" | None | `fetch-galway-reasons.py` |
| **Laserfiche WebLink 11** (`portal.monaghancoco.ie`) | Monaghan | Per-document entries; **server-side text** via `GetTextHtmlForPage` (no OCR) | Browser-like UA + session cookie | `fetch-monaghan-reasons.py` |
| **Cloudflare + ASP.NET WebForms** (`eplan.donegalcoco.ie`) | Donegal | PDF behind a Cloudflare challenge + session-stateful `ViewFile.aspx` | **Cloudflare clearance — interactive browser only** | MCP-browser recipe + `load-donegal-pdfs.py` (see Donegal section) |
| **eplanning.ie/{Council}CC** (national portal) | Councils with no own portal | LGMA-managed; no usable structured reasons | None | (not scraped — would need per-case PDF extraction) |

The early per-council iDocs scripts (`fetch-kildare-reasons.py`,
`fetch-kildare-schedule.py`, `fetch-meath-reasons.py`, `fetch-louth-reasons.py`,
`fetch-wicklow-reasons.py`) are **superseded** by the generic
`fetch-idocs-reasons.py`. They're kept for historical reference but you should
not clone them for a new council — extend the generic scraper instead.

## The generic iDocs scraper (primary path)

`scripts/fetch-idocs-reasons.py` handles any iDocsWeb / iDocsWebDPSS council.
Most Irish councils run this backend (a Local Government Computer Services
product), differing only by base URL.

```bash
uv run python scripts/fetch-idocs-reasons.py \
    --db acp.db --council "Laois County Council" \
    --base "https://plandocs.laois.ie/iDocsWeb" \
    [--delay 0.6] [--retry] [--insecure] [--limit N] [--no-followup]
```

How it works:
1. `listFiles.aspx?catalog=planning&id=<application_number>` — sets the session
   cookie and lists the application's documents.
2. `fetch_nod_docid()` picks the decision doc by label, in priority order:
   Notification of Decision → Managers/Chief Executive's Order → Schedule of
   Conditions → Notification of Decision (fallback) → Decision → Notification →
   Correspondence (last resort).
3. `fetch_doc_bytes()` walks `ViewFiles.aspx?docid=… → iframe src → files/<uuid>`
   and returns the PDF/DjVu bytes. Handles both the direct `files/<uuid>` form
   and Galway City's `ViewPdf.aspx?file=<uuid>` viewer variant.
4. `_extract_text()` (shared `pdf_parser`) — pypdf first, OCR fallback for
   scanned docs and junk/watermark text layers.
5. `parse_reasons()` — two-tier marker matching (see below), splits numbered
   items, writes `council_refusal_reasons` + `council_reasons_fetch`.
6. Auto-chains classify + categorise unless `--no-followup`.

Flags worth knowing:
- `--retry` — re-attempt only refs whose last fetch errored (idempotent resume).
- `--insecure` — skip TLS verification for councils with a broken cert chain
  (Sligo's `www.sligococo.ie`). Use only when you've confirmed the cert is the
  problem, not a MITM.
- `--limit N` — sanity-test a handful of refs before a full run.

### Reason-section markers

`parse_reasons()` anchors on the reasons section using two tiers (STRONG tried
first, then a WEAK bare-"SCHEDULE" fallback for OCR'd docs), then splits on
line-anchored numbered items. STRONG markers accumulated across councils:

- `schedule of reasons for refusal` (Mayo 2nd schedule)
- `reasons for refusal` (Meath)
- `refusal reason(s)` — reversed word order, no "for" (Cavan)
- `permission is refused for the following reason(s)` (Kildare modern)
- `for the reason(s) set out hereunder` (Kildare older)
- `refused for the following reason(s)`
- `on the grounds stipulated/set out … schedule` (Carlow)
- `PL Ref: NN/NN Refusal` — schedule header (Carlow)
- `reference number in register: NN/NN` (Wicklow OCR anchor)
- `reference no[:.] NN` — colon-tolerant (Louth, Cavan schedule page)

WEAK: bare `SCHEDULE` / `S C H E D U L E` / OCR `CHEDULE` (Wicklow), with a
negative lookbehind so `END OF SCHEDULE` / `FIRST SCHEDULE` don't win the
last-occurrence anchor over the real header (Mayo).

**Over-split guard:** a parse yielding more than 25 reasons is rejected as
noise — it means the anchor matched a combined "Correspondence" bundle and is
splitting unrelated numbered items (bylaws, conditions) into refusal reasons
(Galway City's older combined docs). Max legitimate observed is 24.

## Adding a new iDocs council

1. Find its portal base URL — visit the council's "online planning / ePlan"
   search and grab a `…/iDocsWeb…/listFiles.aspx?catalog=planning&id=<ref>` URL.
   The base is everything up to `/listFiles.aspx`.
2. Confirm `?id=` is the `application_number` (it has been for every council so
   far) by opening one known refusal by hand.
3. Sanity-run with `--limit 3 --no-followup`. If you get reasons, run the full
   council. If you get errors, diagnose by bucket (see below).
4. If a single shard outgrows the others later, update `SHARD_COUNCILS` in
   `split-shipped-db.py` (every RoI authority must be mapped there).

Diagnosing a failed iDocs run (`council_reasons_fetch.error_message`):
- **"no decision document on portal"** — `fetch_nod_docid` found no matching
  label. Dump the listing's row labels; the council may use an older/odd term
  (we added "Managers Order", "Correspondence", "Notification" this way). Watch
  the **cell-count guard** — Sligo's listing has 7 columns; the guard allows
  5–7. Some old apps genuinely have only a combined "Consolidated Application"
  scan and no separate decision doc (Kerry pre-2018) — not worth chasing.
- **"no reasons section parsed"** — doc fetched but markers missed. Read the
  text; add a STRONG marker if the phrasing is new (don't loosen WEAK — it
  causes mis-anchoring). Verify any new marker against existing councils' stored
  reasons for false positives before committing.
- **"text extraction empty (incl OCR)"** — scanned doc, OCR produced nothing,
  or a junk watermark text layer fooled the length check. `_extract_text` now
  detects repeated-watermark layers and forces OCR (recovered Cavan, Galway).
- **SSL/cert errors** — add `--insecure` (Sligo).
- **Cloudflare / "Just a moment"** — not iDocs; needs the browser approach
  (see Donegal).

## Monaghan — Laserfiche WebLink 11 (`fetch-monaghan-reasons.py`)

Monaghan is **not** iDocs — it runs Laserfiche WebLink 11 (an Angular SPA). The
API was reverse-engineered from `app/dist/search/main.js`:

1. GET `search.aspx?dbid=0&cr=1` with a **real browser UA + Accept** (a plain
   client 403s) → sets the `lastSessionAccess` cookie.
2. POST `DocumentService.aspx/GetRepoNameByDbid {dbid:0}` → repo `MONLFPLANNING`.
3. POST `SearchService.aspx/GetSearchListing` with searchSyn
   `{LF:Name~="<ref>"}` → one entry per document, named "<ref> - <Doc Type>".
4. Pick the decision doc by name, in priority order: Chief Executive's Order →
   AI Order → Notification of Decision → Planners Report. Older refs use
   "Chief Executive - Decision".
5. POST `DocumentService.aspx/GetTextHtmlForPage` per page → **server-side
   extracted text** (no local OCR needed — highest-quality source we have).
6. Strip HTML, join pages, reuse the shared `parse_reasons`.

Try several candidate docs in priority order: a council's primary Order can be a
scanned PDF with no server text while a lower-priority doc carries the reasons.

## Donegal — Cloudflare + WebForms (interactive browser only)

Donegal (`eplan.donegalcoco.ie/PlanningDoc.aspx?id=<ref>`) is the hard one and
is **not fully done**. Its portal sits behind a **Cloudflare managed
challenge**, so no standalone HTTP/Playwright scraper can reach it — headless
Chromium, real Chrome with a persistent profile, stealth flags, and
`playwright-stealth` all fail to clear it. **Only the MCP Playwright browser**
(interactive) passes Cloudflare.

Two further constraints:
- **Operating hours.** The portal returns "This system is currently unavailable
  outside of active hours" at night — scrape during Irish business hours.
- **Session-stateful documents.** Once a top-level page is open (Cloudflare
  clearance is then session-wide), the flow is ASP.NET WebForms:
  1. GET `PlanningDoc.aspx?id=<ref>` (top-level nav passes Cloudflare; an
     iframe load does **not** — it re-triggers the challenge).
  2. Postback `__EVENTTARGET=chkAgree, chkAgree=on` (accept copyright) → a
     `btnViewFiles` submit button appears.
  3. Postback `btnViewFiles=View Files` → `#gvResults` document table. Each row
     link has `data-Name` (doc type) + `__doPostBack('gvResults$ctlNN$lnkView')`.
  4. Decision priority: Chief Executive's / Managers Order → Notification of
     Decision → Planners Report (reuse `parse_reasons`).
  5. **PDF delivery needs a real form submit, not fetch.** A raw `fetch` POST
     does not set the session var that `ViewFile.aspx` reads. Working recipe:
     build a real `<form>` targeting a hidden iframe, submit the decision
     postback (browser navigates the iframe → sets session state), then
     `fetch('ViewFile.aspx')` returns the PDF bytes. base64 it and POST to a
     tiny local receiver on `127.0.0.1` (localhost is exempt from the HTTPS
     mixed-content block), which saves the PDF to disk.
  6. Ingest the saved PDFs with `scripts/load-donegal-pdfs.py` (parses + writes
     to the DB, reusing `parse_reasons`).

**Contamination caveat:** `ViewFile.aspx`'s session pointer gets stuck after
~30–40 fetches in one session — it starts returning the same cached PDF for
every ref. So (a) work in small batches and restart the browser session between
them, and (b) ALWAYS verify each PDF strictly contains its own ref (slash form
`26/60542` or the bare ref) before trusting it; discard byte-identical PDFs
across refs (the tell-tale of the stale-cache race). `load-donegal-pdfs.py`
enforces this gate.

The local-receiver script and `load-donegal-pdfs.py` are rebuilt per session
(they were throwaway). Full Donegal coverage (~1,286 refusals) accrues in
verified MCP-driven batches during operating hours; nothing is shipped until the
per-ref integrity check passes.

## Coverage as of 2026-06-01

51,363 reasons across 21,536 applications in 27 councils. Top councils:

| Council | Refusals | With reasons | % | Source |
|---|---|---|---|---|
| Cork County | 13,108 | 5,532 | 42% | agile |
| Dun Laoghaire Rathdown | 5,600 | 2,658 | 47% | agile |
| Galway County | 2,460 | 2,255 | 92% | apps.galwaycoco.ie |
| Wexford | 2,933 | 1,955 | 67% | agile |
| Dublin City | 1,805 | 1,599 | 89% | agile |
| Fingal | 2,538 | 1,422 | 56% | agile |
| South Dublin | 2,632 | 1,232 | 47% | agile |
| Cork City | 1,632 | 1,166 | 71% | agile |
| Kildare | 2,148 | 1,077 | 50% | iDocs |
| Meath | 1,224 | 1,007 | 82% | iDocs |
| Wicklow | 952 | 945 | 99% | iDocs |
| Louth | 1,968 | 926 | 47% | iDocs |
| Waterford | 871 | 760 | 87% | iDocs |
| Limerick | 751 | 694 | 92% | iDocs |
| Kilkenny | 730 | 581 | 80% | iDocs |
| Mayo | 886 | 548 | 62% | iDocs |
| Kerry | 1,059 | 480 | 45% | iDocs (older docs = combined scans) |
| Westmeath | 631 | 406 | 64% | iDocs |
| Roscommon | 462 | 391 | 85% | iDocs |
| Tipperary | 339 | 319 | 94% | iDocs |
| Laois | 257 | 254 | 99% | iDocs |
| Cavan | 293 | 228 | 78% | iDocs |
| Carlow | 238 | 212 | 89% | iDocs |
| Sligo | 214 | 204 | 95% | iDocs (--insecure; 7-col layout) |
| Longford | 209 | 175 | 84% | iDocs |
| Monaghan | 293 | 228 | 78% | Laserfiche WebLink |
| Leitrim | 60 | 56 | 93% | iDocs |
| **Donegal** | **1,286** | **0** | **0%** | Cloudflare — deferred to operating hours |

## Edge cases to watch

- **OCR junk text layers** — some scanned PDFs carry a repeated watermark
  (e.g. "<Authority> - Inspection Purposes Only!") as their only text layer,
  which clears the length threshold so OCR never fires. `_extract_text` detects
  low line-diversity layers and forces OCR (recovered Cavan, Galway City).
- **Combined "Correspondence" bundles** — older Galway City / Kerry apps file
  the decision inside one large multi-document scan with no separate decision
  doc. Yields contaminated multi-doc reasons; the >25-reason over-split guard
  rejects these. Not cost-effective to OCR at scale; accept the lower coverage.
- **LGMA truncates descriptions to 80 chars** (DLR, Fingal). Use
  `backfill-full-descriptions.py` before classifying dev types.
- **Agile search API flakes** — empty results for valid refs; retry with
  exponential backoff.
- **Scanned PDFs** — pre-2020 docs are often raster scans. OCR fallback needs
  Tesseract at `C:\Program Files\Tesseract-OCR\tesseract.exe` and (for DjVu)
  DjVuLibre's `ddjvu.exe`.
- **Two numbered lists in one doc** — Kildare older Decision PDFs put an
  appeal-procedure preamble before the real reasons; anchor on the
  project-restatement line and slice everything before it.
- **Never run classify/categorise concurrently with a fetch** — both write the
  DB and SQLite will throw "database is locked". Fetch first (`--no-followup`),
  then classify, then categorise, as separate passes.
