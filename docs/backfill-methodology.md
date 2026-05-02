# Initial backfill — methodology

The weekly cron fetches deltas only; the first run still has to populate the
archive end-to-end. That's a manual job because (a) it takes 8–24 hours of
polite scraping plus 15–25 hours of local LLM inference, and (b) Ollama can't
run on the GitHub-hosted runner.

## Prerequisites

- **Python 3.12+** (3.13 also works; matches `requires-python` in pyproject)
- **[uv](https://github.com/astral-sh/uv)** for dependency + venv management
- **[Ollama](https://ollama.ai)** running locally on `http://localhost:11434`
- **The `llama3.2:3b` model**, pulled via `ollama pull llama3.2:3b`
  (other models work — pass `--model` to `acp classify` to override)
- A laptop you can leave plugged in overnight, or a cloud workstation

## Step 1 — install the project

```bash
git clone git@github.com:JumpMediaIe/acp-decisions.git
cd acp-decisions
uv sync --extra dev
uv run pytest               # sanity: should be 100+ tests passing
```

## Step 2 — full backfill scrape (8–24 h)

This walks every type-filtered listing on pleanala.ie, fetches every case page
that isn't already in the DB, and downloads the Order PDF for every refused
case.

```bash
uv run acp scrape --all --db acp.db
```

Polite settings (in `http_client.py`): 1.5 s minimum interval between requests,
3 retries on 5xx/429 with 5 s / 30 s / 5 min backoff. Total wall-clock time
depends on how many cases ACP currently lists across LH + PA + H type filters
— at ~1.5 s/request and a typical ~10 k cases that's around 4 hours just for
the case pages, plus ~1.5 s × number-of-refusals for Order PDFs.

If the run is interrupted, re-run the same command — `scrape --all` skips
cases already in the DB.

### Verifying the scrape

```bash
sqlite3 acp.db <<'SQL'
SELECT decision_outcome, COUNT(*)
FROM decisions
GROUP BY decision_outcome
ORDER BY 2 DESC;

SELECT COUNT(*) AS reasons FROM refusal_reasons;
SELECT COUNT(*) AS errors  FROM scrape_errors;
SQL
```

A healthy scrape has:

- thousands of `decisions` rows
- a `refusal_reasons` count roughly proportional to `refused` decisions × 2-3
- `scrape_errors` near zero (a few transient ones are normal; investigate any
  parse_error rows by looking at `case_id_url` and re-fetching by hand)

## Step 3 — classify (15–25 h)

Make sure Ollama is running and the model is pulled:

```bash
ollama serve &                 # if not already running as a service
ollama pull llama3.2:3b
```

Then run the classifier. It iterates every `refusal_reasons` row that doesn't
yet have a `reason_categories` entry:

```bash
uv run acp classify --db acp.db
```

A 3B-parameter model on a modern laptop classifies a few reasons per second.
Total runtime ≈ `total_reasons / 1-3 per second`. The classifier is
restartable: re-running picks up where it left off.

If classification quality is off, override the model:

```bash
uv run acp classify --db acp.db --model llama3.1:8b
```

(Bigger models are more accurate but slower. Check `categories` table for the
taxonomy; revise the YAML if you see a high `other` rate.)

## Step 4 — push the DB

The DB is committed to the repo so downstream consumers (e.g. the
planningcheck.ie `/decisions` UI) can pull it deterministically at build time.

```bash
git add acp.db
git commit -m "backfill: initial archive of N decisions"
git push origin main
```

Don't be alarmed by the file size — SQLite + FTS for ~10 k cases comfortably
fits under the GitHub 100 MB single-file ceiling. If it ever crosses 100 MB
we'll switch to splitting by year or hosting via Releases.

## Troubleshooting

- **HTTP 429 / RateLimitedError**: someone (or you) is hitting pleanala.ie too
  hard. Stop, wait an hour, restart. The 1.5 s interval is conservative; don't
  shorten it.
- **Order PDF parse errors**: ACP occasionally publishes scanned PDFs that
  pypdf can't extract text from. Those land in `scrape_errors` with class
  `parse_error` — they're rare and worth investigating manually rather than
  patching the parser blindly.
- **Mojibake in reason text**: pypdf's text extractor mangles smart quotes /
  Irish-language characters in some PDFs. The classifier handles this fine
  (Ollama is robust to noise), but for the UI we may want a normalisation
  pass — see backlog.
- **Schema drift**: `db.open_db` re-applies `schema.sql` on every connection
  with `IF NOT EXISTS`, so adding columns or tables in YAML/SQL works
  forwards-compatibly. Renames or drops require a real migration.
