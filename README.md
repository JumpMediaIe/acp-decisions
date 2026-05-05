# acp-decisions

Public archive of An Coimisiún Pleanála appeal decisions. Data feeds [planningcheck.ie/decisions](https://www.planningcheck.ie/decisions).

## What this is

A Python scraper that:

- Walks the public case listings at <https://www.pleanala.ie>
- Parses each case page into structured metadata
- For refused cases, downloads the Order PDF and extracts the numbered "Reasons and Considerations"
- Classifies each refusal reason against a 27-category taxonomy using a local LLM (Ollama)
- Stores everything in `acp.db` (SQLite, committed to this repo)

The data is consumed at build time by planningcheck.ie. See [`docs/discovery-2026-05-02.md`](docs/discovery-2026-05-02.md) for the live-site investigation that shaped the parser, and [`docs/backfill-methodology.md`](docs/backfill-methodology.md) for the runbook.

## Setup (one-time)

1. Install [uv](https://docs.astral.sh/uv/): `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. Install [Ollama](https://ollama.ai) and pull the default model: `ollama pull llama3.2:3b`
3. From the repo root: `uv sync --extra dev`

## Usage

```bash
# Scrape one specific case end-to-end
uv run acp scrape --case 315183

# Walk every type listing; scrape every NEW case
uv run acp scrape --all

# Restrict the walk to specific type codes
uv run acp scrape --all --types LH,PA

# Classify any unclassified refusal reasons via Ollama
uv run acp classify

# Use a different model
uv run acp classify --model llama3.1:8b

# Run the test suite
uv run pytest
```

## Architecture

| Module | Role |
|---|---|
| `http_client.py` | Polite httpx wrapper: 1.5 s rate limit, 5xx/429 retries, identifying User-Agent |
| `walker.py` | Discover case IDs from `?type=LH/PA/H` listings |
| `parser.py` | Extract metadata + PDF links from a case page |
| `pdf_parser.py` | Extract ABP reference + numbered refusal reasons from Order PDFs |
| `outcome.py` | Free-text decision strings → canonical bucket (`refused`, `granted`, `granted_with_conditions`, `withdrawn`, `invalid`, `procedural`) |
| `county_map.py` / `devtype_map.py` | Map ACP free text → planningcheck.ie's CountyId / DevelopmentTypeId |
| `orchestrator.py` | Compose the pipeline: HTML → outcome → (Order PDF if refused) → DB |
| `upsert.py` | Idempotent SQLite upserts for decisions, documents, reasons, errors |
| `classifier.py` | Ollama client + reason-to-categories classification |
| `taxonomy.py` / `taxonomy.yaml` | 27-category refusal-reason classification + seeder |
| `cli.py` | `acp scrape` / `acp classify` entry points |

## How the data is updated live

Every Thursday at 09:00 a Windows scheduled task on the maintainer's laptop
runs `scripts/weekly-update.ps1`, which scrapes new cases, classifies them
locally with Gemma 4 e2b via Ollama, copies `acp.db` into the
`irish-planning-tool` repo, and pushes — Vercel auto-deploys planningcheck.ie
with the fresh data.

**Full operational guide: [`docs/operations.md`](docs/operations.md)** —
covers what runs where, log locations, troubleshooting, how to run a refresh
manually, how to disable the schedule, and how to reinstall on a new laptop.

A GitHub Actions cron (`.github/workflows/scrape.yml`) is also configured but
not currently in use — it can scrape, but can't run the local-LLM classifier.
The Windows scheduled task is the active automation.

## License

Code is MIT. Data is reproduced from public records published by An Coimisiún Pleanála under the public-records / legitimate-interest basis. Applicant names are scraped (they're on the public page) but never displayed in the planningcheck.ie UI.
