# Per-council refusal-reason scrapers

The LGMA national dataset gives us the binary "REFUSED" status for every Irish
planning application, plus a `link_app_details` URL where one exists. It does
not give us **why** each application was refused — that lives in each council's
own document portal.

This doc records, for each council we've backfilled, where the reasons live
and how the scraper extracts them. Use it before adding another council so the
right script gets reused.

## Portal inventory

Most councils fall into one of four buckets:

| Portal family | Used by | Doc structure | Auth | Script |
|---|---|---|---|---|
| **agileapplications.ie** | Cork County, South Dublin, Fingal, Wexford, Galway *city*, **Dun Laoghaire**, **Dublin City** | Structured JSON API (prescriptionCode='R') | None | `scripts/fetch-council-reasons.py` |
| **apps.galwaycoco.ie/ViewExternalDocuments** | **Galway County** | PDF — schedule of reasons after a "SCHEDULE REFERRED TO" or "for the reason(s) set out hereunder" anchor | None | `scripts/fetch-galway-reasons.py` |
| **idocsweb.kildarecoco.ie/iDocsWebDPSS** | **Kildare** | PDF — modern docs labelled "Notification of Decision" / older as "Schedule of Conditions" or "Decision". Reached via a 4-hop iframe chain | Session cookie set by initial `listFiles.aspx` hit | `scripts/fetch-kildare-reasons.py` + `scripts/fetch-kildare-schedule.py` |
| **eplanning.ie/{Council}CC** | Most "national portal" councils with no own portal | LGMA-managed; no usable structured reasons | None | (not yet scraped — would need PDF extraction from individual planning case pages) |

If a new council's `link_app_details` points at `agileapplications.ie`, you're
already done — `fetch-council-reasons.py` handles it. If the LGMA URL is empty
or points elsewhere, follow the playbook below.

## Adding a new council on the agile portal

The LGMA dataset sometimes lists councils on `agileapplications.ie` without
populating `link_app_details`. To force the fetcher to pick them up:

1. Confirm the council's URL slug — visit `https://planning.agileapplications.ie/{slug}`
   in a browser. Examples: `dunlaoghaire`, `dublincity`, `corkcoco`.
2. Sanity-check via Python:
   ```python
   from acp_decisions.agile_api import AgileApiClient
   api = AgileApiClient()
   api.search_application_id("{slug}", "{a real refusal reference}")
   ```
   You should get a non-None application ID.
3. Backfill `link_app_details` for that council so the fetcher's slug-extraction
   regex matches:
   ```sql
   UPDATE planning_applications
      SET link_app_details = 'https://planning.agileapplications.ie/{slug}/'
    WHERE planning_authority = '{Council Name}'
      AND (link_app_details IS NULL OR link_app_details = '');
   ```
4. Run `fetch-council-reasons.py` — it auto-discovers and processes the new rows.
5. Run classify + categorise (auto-chains unless `--no-followup` is set).

Done this way for: Dun Laoghaire Rathdown, Dublin City.

## Adding a council with its own portal

Each "own portal" council needs a bespoke script because the URL structure,
doc-labelling conventions and decision-PDF templates all vary. Follow this
pattern (it's what we did for Galway and Kildare):

1. **Find the doc-list URL.** Search the council's website for "online planning
   enquiry" / "ePlan". Many councils host on `iDocsWebDPSS` (a Local Government
   Computer Services product, same backend as Kildare). Others have bespoke
   ASP.NET or PHP front-ends.
2. **Map the ref → doc-list URL.** Inspect 2–3 applications by hand. Note the
   query-string parameter name (`?id=`, `?RefNo=`, `?fileNumber=`).
3. **Identify the decision document label.** It varies by era:
   - Modern: "Notification of Decision" (singular, not "to third parties")
   - Mid-era: "Schedule of Conditions" or "Notification of Decision Letters"
   - Old: "Decision" or "Managers Order"
   Build a fallback chain in your `fetch_*_docid()` function.
4. **Walk the iframe chain (if any).** Kildare wraps every PDF in two iframes
   plus a session cookie. Galway serves PDFs straight from a relative URL.
5. **Extract text** via `pdf_parser._extract_text` so OCR fallback kicks in for
   scanned older PDFs (requires Tesseract at the default Windows install path).
6. **Parse reasons.** Find the section anchor ("SCHEDULE REFERRED TO",
   "REFUSED for the following reason(s)", or "Planning Permission is sought
   for…" restatement) and split on `^(\d+)[.)]` line-anchored numbered items.
   Be defensive about:
   - Singular vs plural ("reason" / "reasons")
   - `1.` vs `1)` markers
   - Single-reason docs with no numbered prefix
   - **Two numbered lists in one doc** (an appeal-procedure preamble plus the
     actual reasons — Kildare older Decision docs do this; cut off everything
     before the project-restatement anchor)
7. **Write to `council_refusal_reasons`** and mark `council_reasons_fetch`
   for idempotent resume.
8. **Auto-chain** classify + categorise via `--no-followup` opt-out (default
   on).

## Coverage as of 2026-05-26

| Council | Refusals | With reasons | % | Source |
|---|---|---|---|---|
| Cork County | 13,108 | ~5,500 | 42% | agile portal (`fetch-council-reasons.py`) |
| Dun Laoghaire Rathdown | 5,600 | 2,658 | 47% (99% dev-type mapped) | agile portal |
| Galway County | 2,460 | 2,255 | 92% | apps.galwaycoco.ie (`fetch-galway-reasons.py`) |
| Kildare | 2,109 | 1,077 | 51% | iDocsWebDPSS (`fetch-kildare-reasons.py` + `fetch-kildare-schedule.py`) |
| Dublin City | 1,805 | in progress | tbd | agile portal |
| Fingal | ~2,650 | ~1,000 | 38% | agile portal (link_app_details was sometimes empty — needed backfill) |
| ...other agile councils | | varies | | agile portal |

## Edge cases to watch

- **LGMA truncates descriptions to 80 chars** for some councils (DLR, Fingal).
  Use `scripts/backfill-full-descriptions.py` to pull `fullProposal` from the
  agile portal's `/api/application/{id}` endpoint before classifying dev types.
- **Agile search API flakes** — empty results for valid refs intermittently.
  Retry with exponential backoff (4–8 attempts).
- **Scanned PDFs** — pre-2020 council docs are sometimes raster scans with no
  text layer. The shared `_extract_text` falls back to Tesseract OCR when
  pypdf returns <500 chars, but the binary must be installed at
  `C:\Program Files\Tesseract-OCR\tesseract.exe`.
- **Pre-2011 applications** — agile portal usually has them despite the
  fetcher's default `--from-date=2011-01-01`. Override the date floor when
  doing one-off backfills.
- **Two numbered lists in a single doc** — Kildare older Decision PDFs include
  the appeal-procedure preamble (1. Confirmation of submission… 2. Statutory
  fee…) BEFORE the real refusal reasons. The parser must anchor on the
  project-restatement line and slice everything before it.
