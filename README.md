# acp-decisions

Public archive of An Coimisiún Pleanála appeal decisions. Data feeds [planningcheck.ie/decisions](https://www.planningcheck.ie/decisions).

## What this is

A Python scraper that:
- Walks the public decisions index at <https://www.pleanala.ie>
- Parses each decision page into structured fields
- Classifies each refusal reason against a canonical ~36-category taxonomy using a local AI model (Ollama)
- Stores everything in `data/acp.db` (SQLite, committed to this repo)

The data is consumed at build time by planningcheck.ie. See the design spec in that repo for the full architecture.

## Setup (one-time)

1. Install [uv](https://docs.astral.sh/uv/): `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. Install [Ollama](https://ollama.ai) and pull the default model: `ollama pull llama3.2:3b`
3. From the repo root: `uv sync --extra dev`

## Usage

```bash
# Backfill 5 years (run once, on your laptop, overnight)
uv run acp scrape backfill --from 2021-01-01 --to 2026-04-30

# Scrape only the past week (default in CI)
uv run acp scrape weekly

# Re-scrape a specific decision (fix errors)
uv run acp scrape one --reference PL06F.249482

# Classify any decisions that haven't been classified yet
uv run acp classify

# Re-classify everything (after taxonomy changes)
uv run acp classify --reclassify

# Run tests
uv run pytest
```

## How the data is updated live

1. The weekly GitHub Actions cron runs `scrape weekly` every Thursday.
2. The maintainer periodically runs `classify` locally (Ollama can't run on CI).
3. The maintainer triggers a Vercel rebuild on planningcheck.ie to push the fresh `acp.db` live.

## Methodology

See [docs/methodology.md](docs/methodology.md) for the full transparency notes — source, time window, classifier approach, taxonomy, error handling.

## License

The code is MIT. The data is reproduced from public records published by An Coimisiún Pleanála under the public-records / public-interest legitimate-interest basis.
