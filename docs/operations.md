# Operations: weekly automation

**TL;DR** — every Thursday at 09:00 local time this laptop scrapes new ACP cases,
classifies them with local Gemma 4, and pushes the updated `acp.db` to
[planningcheck.ie](https://www.planningcheck.ie/decisions). Logs are in
`acp-decisions/logs/`. If something looks wrong on the live site, start there.

## Diagram

```
┌─ Windows Task Scheduler (Thursdays 09:00) ───────────────────────────┐
│  Task name: planningcheck-decisions-weekly                           │
│  Runs: powershell -File acp-decisions/scripts/weekly-update.ps1      │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌─ weekly-update.ps1 ──────────────────────────────────────────────────┐
│  1. Verify Ollama is reachable at localhost:11434                    │
│                                                                      │
│  ── ACP appeals half (pleanala.ie) ──                                │
│  2. acp scrape --all --from-year 2021                                │
│       (orchestrator skips pending / decision_date == "" cases)       │
│  3. acp classify   (Gemma 4 e2b via Ollama, only unclassified rows)  │
│                                                                      │
│  ── Council refusals half (LGMA + agile portal) ──                   │
│  4. acp lgma-sync                                                    │
│       (national planning_applications dataset, all 31 LAs)           │
│  5. scripts/fetch-council-reasons.py                                 │
│       (refusal-reason text via agileapplications.ie API)             │
│  6. scripts/classify-council-reasons.py    (Gemma 4 entity extract)  │
│  7. scripts/categorize-council-reasons.py  (Gemma 4 taxonomy assign) │
│                                                                      │
│  8. Copy acp.db → irish-planning-tool/data/acp.db                    │
│  9. git commit + git push origin main      (irish-planning-tool)     │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌─ Vercel ─────────────────────────────────────────────────────────────┐
│  Detects push to main, redeploys planningcheck.ie automatically.     │
│  /decisions surfaces the fresh acp.db in ~30-60 seconds.             │
└──────────────────────────────────────────────────────────────────────┘
```

## The moving pieces

| Component | Location | Role |
|---|---|---|
| **Scraper** | `C:\Users\akhil\acp-decisions` | The Python project that does the actual scraping + classification. |
| **acp.db** (source) | `C:\Users\akhil\acp-decisions\acp.db` | The working DB the scraper writes to. |
| **acp.db** (shipped) | `C:\Users\akhil\irish-planning-tool\data\acp.db` | The DB committed to the website repo. Vercel reads this at build time. |
| **Ollama** | localhost:11434 (system service) | Runs Gemma 4 e2b for classification. Auto-starts with Windows. |
| **Gemma 4 e2b** | `~\.ollama\models\` | The 7.2 GB language model itself. |
| **Task Scheduler entry** | `planningcheck-decisions-weekly` | The Thursday-at-09:00 trigger. |
| **Pipeline script** | `scripts/weekly-update.ps1` | Glues everything together, logs to `logs/`. |
| **Logs** | `acp-decisions/logs/weekly-YYYY-MM-DD-HHMM.log` | One file per run. Gitignored. |
| **Vercel** | planningcheck.ie | Auto-deploys on push to `irish-planning-tool/main`. |

## Verifying it's working

```powershell
# Last and next scheduled runs
Get-ScheduledTaskInfo -TaskName 'planningcheck-decisions-weekly'

# Tail the most recent log
Get-Content (Get-ChildItem C:\Users\akhil\acp-decisions\logs\weekly-*.log |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName -Tail 30

# Confirm the live site reflects the latest push
curl -s https://www.planningcheck.ie/decisions | Select-String "Total decisions" -Context 0,2
```

## Run it manually (without waiting for Thursday)

```powershell
Start-ScheduledTask -TaskName 'planningcheck-decisions-weekly'
```

Or run the script directly to see the live console output:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\akhil\acp-decisions\scripts\weekly-update.ps1
```

## Troubleshooting

### The site didn't update this week

Check the most recent log:

```powershell
Get-Content (Get-ChildItem C:\Users\akhil\acp-decisions\logs\weekly-*.log |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
```

Look for the last `[FAIL ]` line. Common causes:

- **Ollama not reachable** → Open Ollama from the Start menu (or check the
  system tray). The service should be running. If not: reinstall from
  https://ollama.com/download/windows.
- **Model not pulled** → `ollama pull gemma4:e2b`.
- **git push fails** → Check the GitHub PAT in
  `irish-planning-tool/.git/config` hasn't expired. If it has, generate a new
  one at https://github.com/settings/tokens (classic, repo scope).
- **Scrape returns 0 cases / many failures** → ACP may have changed their
  HTML or rate-limited us. Open `https://www.pleanala.ie` in a browser to
  confirm the site is up; check `scrape_errors` table in `acp.db` for
  details. If ACP HTML has changed structure, see
  `docs/discovery-2026-05-02.md` for selectors that may need updating.
- **`The read operation timed out` on pleanala.ie listings** → The cases
  listing pages (`/en-ie/cases?type=...&year=...`) can take 20-30 s per
  response when pleanala is under load. The HTTP client timeout is set to
  90 s in `src/acp_decisions/http_client.py` (`DEFAULT_TIMEOUT_S`); if
  that's still too tight, bump it. The client retries 3x with 5 s / 30 s
  / 5 min backoffs, so transient slowness is usually absorbed. If
  pleanala stays slow for the whole run, use the council-only catchup
  below to refresh just the LGMA + agile data and retry the ACP scrape
  separately when the site recovers.

### Laptop was off all Thursday

`StartWhenAvailable` handles this: the task runs as soon as the laptop wakes
after the scheduled time. Catch-up is automatic. No action needed.

### "I need fresher data right now"

Just run it manually:

```powershell
Start-ScheduledTask -TaskName 'planningcheck-decisions-weekly'
```

About 25 min later, planningcheck.ie has fresh data.

## Council-only catchup (when pleanala is unreachable)

`weekly-update.ps1` runs the steps in order and throws on first failure,
so a pleanala.ie outage blocks the LGMA + council-reason refresh even
though those use different upstreams. When that happens, run the
council half on its own:

```powershell
# From acp-decisions repo
Set-Location C:\Users\akhil\acp-decisions
uv run acp --db acp.db lgma-sync
uv run python scripts/fetch-council-reasons.py --db acp.db --delay 1.0
uv run python scripts/classify-council-reasons.py --db acp.db
uv run python scripts/categorize-council-reasons.py --db acp.db

# Copy + push the DB
Copy-Item acp.db C:\Users\akhil\irish-planning-tool\data\acp.db -Force
Set-Location C:\Users\akhil\irish-planning-tool
git add data/acp.db
git commit -m "data: council catchup ($(Get-Date -Format 'yyyy-MM-dd'))"
git push origin main
```

Once pleanala is responsive again, run the ACP half on its own:

```powershell
Set-Location C:\Users\akhil\acp-decisions
uv run acp --db acp.db scrape --all --from-year 2021
uv run acp --db acp.db classify

Copy-Item acp.db C:\Users\akhil\irish-planning-tool\data\acp.db -Force
Set-Location C:\Users\akhil\irish-planning-tool
git add data/acp.db
git commit -m "data: acp catchup ($(Get-Date -Format 'yyyy-MM-dd'))"
git push origin main
```

## Changing the schedule

Re-running the install script with edits to the trigger line is the simplest
path. Edit `scripts/install-scheduled-task.ps1` line that defines `$Trigger`,
then:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\akhil\acp-decisions\scripts\install-scheduled-task.ps1
```

(The script unregisters the old task before registering the new one, so it's
idempotent.)

## Disabling / re-enabling

```powershell
# Pause indefinitely
Disable-ScheduledTask -TaskName 'planningcheck-decisions-weekly'

# Resume
Enable-ScheduledTask -TaskName 'planningcheck-decisions-weekly'

# Remove entirely
Unregister-ScheduledTask -TaskName 'planningcheck-decisions-weekly' -Confirm:$false
```

## Moving to a new laptop

When you get a new machine, the automation has to be reinstalled from
scratch. The full setup is:

1. **Install Ollama** for Windows (https://ollama.com/download/windows).
2. **Pull the model**: `ollama pull gemma4:e2b` (~7.2 GB).
3. **Install uv**: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or via
   winget / scoop).
4. **Clone both repos** into `C:\Users\<you>\`:
   ```powershell
   git clone git@github.com:JumpMediaIe/acp-decisions.git
   git clone git@github.com:JumpMediaIe/irish-planning-tool.git
   ```
5. **Install scraper deps**: `cd acp-decisions; uv sync --extra dev`.
6. **Install Tesseract** for the OCR fallback path on scanned Order PDFs.
   UB-Mannheim Windows installer accepts defaults; pdf_parser auto-detects
   the standard install path.
7. **Edit `scripts/weekly-update.ps1`** if your username isn't `akhil` (the
   `$AcpRepo` and `$WebRepo` paths are hardcoded).
8. **Register the task**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/install-scheduled-task.ps1
   ```
9. **Run once manually** to verify everything is wired up:
   ```powershell
   Start-ScheduledTask -TaskName 'planningcheck-decisions-weekly'
   ```

## What does NOT happen automatically

- **Adding new ACP type codes** — if ACP introduces a new prefix beyond
  PL/LH/PA/CH/MA/H, we won't see those cases until someone updates
  `DEFAULT_TYPE_CODES` in `cli.py`.
- **Mapping new development types** — unmapped raw `development_type_raw`
  strings stay unmapped until someone adds patterns to `devtype_map.py`.
  Currently ~12% of cases are unmapped (mainly telecoms, quarries, signage).
- **Taxonomy expansion** — if the LLM is putting too many reasons into the
  catch-all `other` category, that signals the taxonomy in `taxonomy.yaml`
  needs new buckets. Re-classify after editing.
- **OCR for corrupted-text-layer PDFs** — ~10 cases per year fall into this
  bucket. The orchestrator records them in `scrape_errors` with class
  `pdf_no_text`. They stay unparsed forever unless we add layout-aware OCR
  or transcribe manually.
- **Date-window filtering** — the cron passes `--from-year 2021`, but the
  cleanup of pre-2021 / pending cases is enforced by the orchestrator's
  skip-when-decision_date-is-empty check. If we ever want to extend back
  before 2021, change `--from-year` in `weekly-update.ps1`.
