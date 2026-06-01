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
| **agileapplications.ie** | Cork County, Cork City, South Dublin, Fingal, Wexford, Dun Laoghaire, Dublin City | Structured JSON API (prescriptionCode='R') — no PDFs. Lower coverage on big councils is older refs the API lacks, not a scraper fault | None | `fetch-council-reasons.py` |

**Cork City quirk:** it's on the agile portal (slug `corkcity`) but (a) its
`link_app_details` points at `planning.corkcity.ie`, not agileapplications.ie —
backfill it to `https://planning.agileapplications.ie/corkcity/` so the
fetcher's filter picks it up; and (b) its `application_number` is stored
slash-stripped (`2543892`) while the portal only matches `25/43892` (slash after
the 2-digit year prefix). `_search_ref()` in `fetch-council-reasons.py` reinserts
the slash for the `corkcity` slug only. The agile search is **flaky** for Cork
City — expect a chunk of "not found via search" on the first pass that clears on
`--retry` (we needed three passes, easing the delay each time: 0.8 → 1.2 → 1.5
→ 2.0s).
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

## Donegal — Cloudflare + WebForms (interactive MCP browser)

Donegal (`eplan.donegalcoco.ie/PlanningDoc.aspx?id=<ref>`) is the hard one and
is **partially done** (262 apps / 431 reasons of ~1,286 refusals as of
2026-06-01; the rest accrue in future MCP-driven sessions). Its portal sits
behind a **Cloudflare managed challenge**, so no standalone HTTP/Playwright
scraper reaches it — headless Chromium, real Chrome + persistent profile,
stealth flags, and `playwright-stealth` all fail. **Only the MCP Playwright
browser** (interactive) passes Cloudflare, and only on a **top-level
navigation** (`browser_navigate`), which auto-solves the challenge in a few
seconds. The clearance is then session-wide for ~280 fetches before the
`cf_clearance` cookie lapses (then in-page fetches 403 with "Just a moment" and
you re-navigate the top tab to re-clear).

Also: the portal is **closed outside Irish business hours** — it returns "This
system is currently unavailable outside of active hours." Scrape during the day.

### The working method (what actually worked — earlier attempts didn't)

The whole per-ref flow runs **inside a nested `<iframe>`** created by an
in-page `browser_evaluate`, NOT a top-level navigation per ref and NOT a
hand-built POST. Two things make it work:

- Once the parent tab is Cloudflare-cleared, **same-origin iframe loads no
  longer get challenged** (they did before the parent was cleared). So one
  cleared tab lets you iterate many refs via iframes.
- The PDF only downloads when the page's **own real form** (with its live, valid
  `__VIEWSTATE`) does the decision-row postback. A synthetic `<form>` rebuilt
  from scraped hidden fields does NOT set the `ViewFile.aspx` session var (it
  returns a 42-byte empty `lblError` stub). The trick: override the page's
  `targetMeBlank()` to retarget its real form at a hidden inner iframe, then
  call the page's own `__doPostBack(pid,'')`. That sets the session var, and a
  subsequent `fetch('ViewFile.aspx')` returns the real PDF bytes.

Per ref, inside the iframe:
1. `iframe.src = PlanningDoc.aspx?id=<ref>`; wait for `#chkAgree`.
2. check `#chkAgree` and eval its `onclick` (the copyright postback) → wait;
   `#btnViewFiles` appears.
3. `#btnViewFiles.click()` → wait; `#gvResults` table loads. Decision priority:
   Chief Executive's / Managers Order → Notification of Decision → Planners
   Report (reuse `parse_reasons`); read the row's `data-Name` + postback id.
4. add an inner iframe, set `targetMeBlank` to retarget the real form at it,
   `__doPostBack(pid,'')` → wait → `fetch('ViewFile.aspx')` → PDF bytes.
5. base64 it and POST to the local sink (`scripts/_pdf_sink.py`, 127.0.0.1:8731
   — localhost is exempt from the HTTPS mixed-content block), which writes
   `data/donegal-pdfs/<ref>.pdf`.

### Driver + queue (keeps MCP-call context small)

`_pdf_sink.py` doubles as a ref queue: `GET /next?n=40` dispenses pending refs
(council refusals minus PDFs already on disk), `GET /status` reports progress.
Install a `window.__don(n)` driver once via `browser_evaluate` (it loops the
above over `/next` refs); then each batch is just `await window.__don(40)` — no
need to re-send the big function. A top-level `browser_navigate` wipes
`window.__don`, so re-install it after each Cloudflare re-clear. Restarting the
sink clears its in-memory `issued` set, so refs stuck "issued" by a failed batch
(but never saved) return to the queue — PDFs on disk are the source of truth.

### Integrity gate (mandatory)

`ViewFile.aspx` can occasionally serve a stale/other ref's PDF. **Always verify
each PDF strictly contains its own ref** (slash form `26/60542` or the bare ref)
before trusting it. `scripts/load-donegal-pdfs.py` enforces this: it parses each
PDF, skips any whose text lacks its ref (logged as `bad_ref`), and only writes
the survivors. In the first 284-PDF batch this caught 9 contaminated files.
(Note: the nested-iframe-per-ref method largely avoids the stale cache that a
single reused session suffers — that batch was 280/280 byte-unique — but the
gate stays the safety net.)

`_pdf_sink.py` and `load-donegal-pdfs.py` live in `scripts/`; the
`data/donegal-pdfs/` working dir is gitignored (local artifacts, re-fetchable).

## Coverage as of 2026-06-01

53,661 reasons across 29,196 applications, 31 councils with reasons. "Refusals"
= applications with a REFUSE decision; "%" = share of those that now have parsed
reasons. Low % on the big agile councils is mostly older refs the agile API has
no reasons for, not scraper failure; Donegal is an in-progress partial.

| Council | Refusals | With reasons | % | Source |
|---|---|---|---|---|
| Cork County | 17,615 | 2,983 | 17% | agile |
| Dun Laoghaire Rathdown | 5,667 | 2,658 | 47% | agile |
| Fingal | 5,658 | 2,558 | 45% | agile |
| Wexford | 5,667 | 2,453 | 43% | agile |
| Galway County | 4,501 | 2,255 | 50% | apps.galwaycoco.ie |
| Kildare | 4,326 | 2,024 | 47% | iDocs |
| South Dublin | 7,590 | 1,597 | 21% | agile |
| Dublin City | 2,468 | 1,523 | 62% | agile |
| Wicklow | 2,089 | 1,417 | 68% | iDocs |
| Meath | 2,096 | 1,345 | 64% | iDocs |
| Louth | 2,254 | 1,127 | 50% | iDocs |
| Cork City | 669 | 580 | 87% | agile (slash-stripped ref; corkcity-only transform + link backfill) |
| Waterford | 1,162 | 760 | 65% | iDocs |
| Limerick | 914 | 694 | 76% | iDocs |
| Kilkenny | 909 | 581 | 64% | iDocs |
| Clare | 962 | 562 | 58% | iDocs |
| Mayo | 911 | 548 | 60% | iDocs |
| Kerry | 1,336 | 480 | 36% | iDocs (older docs = combined scans) |
| Westmeath | 925 | 406 | 44% | iDocs |
| Roscommon | 712 | 391 | 55% | iDocs |
| Tipperary | 473 | 319 | 67% | iDocs |
| Laois | 537 | 254 | 47% | iDocs |
| Offaly | 536 | 235 | 44% | iDocs |
| Monaghan | 455 | 228 | 50% | Laserfiche WebLink |
| Carlow | 351 | 212 | 60% | iDocs |
| Sligo | 337 | 204 | 61% | iDocs (--insecure; 7-col layout) |
| Cavan | 365 | 181 | 50% | iDocs |
| Longford | 299 | 175 | 51% | iDocs |
| Galway City | 481 | 128 | 27% | iDocs (older = combined scans) |
| Leitrim | 98 | 56 | 56% | iDocs |
| Donegal | 1,286 | 262 | 20% | Cloudflare — MCP browser, partial (in progress) |

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
