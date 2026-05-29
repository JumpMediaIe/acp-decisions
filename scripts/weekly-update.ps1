# Weekly update pipeline for the planningcheck.ie decisions archive.
#
# Runs on this laptop (where Ollama + Gemma 4 live), driven by Windows Task
# Scheduler. End-to-end:
#
#   1. Scrape new ACP cases since the last run (year-walked 2021..current)
#   2. Classify any unclassified refusal reasons via local Ollama / gemma4:e2b
#   3. Copy acp.db into irish-planning-tool/data/
#   4. If anything changed: commit and push -> triggers Vercel auto-deploy
#
# Idempotent: re-running with no new ACP cases is a no-op (no commit, no push).
#
# Logs to C:\Users\akhil\acp-decisions\logs\weekly-YYYY-MM-DD-HHMM.log
# (one file per run so old logs survive).

# --- paths -----------------------------------------------------------------
$AcpRepo  = 'C:\Users\akhil\acp-decisions'
$WebRepo  = 'C:\Users\akhil\irish-planning-tool'
$DbSrc    = Join-Path $AcpRepo 'acp.db'
$DataDir  = Join-Path $WebRepo 'data'
$LogDir   = Join-Path $AcpRepo 'logs'
$LogFile  = Join-Path $LogDir ("weekly-" + (Get-Date -Format 'yyyy-MM-dd-HHmm') + '.log')
$OllamaUrl = 'http://localhost:11434'

# --- helpers ---------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Log {
    param([string]$Msg)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $Msg"
    Add-Content -Path $LogFile -Value $line -Encoding utf8
    Write-Host $line
}

# Run a native command, append all output (stdout + stderr) to the log file,
# and throw if it returns non-zero. We avoid `2>&1` on the pipeline because
# PowerShell 5.1 wraps native stderr as ErrorRecord which $ErrorActionPreference
# treats as a fatal error.
function Run-Native {
    # Note: parameter name is `Arguments`, not the reserved `$Args`.
    param([string]$Label, [string]$Exe, [string[]]$Arguments)
    Log "[start] $Label"
    Log "        cmd: $Exe $($Arguments -join ' ')"
    # Capture stdout + stderr via cmd-style redirection into a temp file, then
    # concat into our log.
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        & $Exe $Arguments > $tmp 2>&1
        $exit = $LASTEXITCODE
        $captured = Get-Content -Path $tmp -Raw -Encoding utf8 -ErrorAction SilentlyContinue
        if ($captured) { Add-Content -Path $LogFile -Value $captured -Encoding utf8 }
        if ($exit -ne 0) {
            Log "[FAIL ] $Label (exit $exit)"
            throw "$Label failed with exit $exit (see $LogFile)"
        }
        Log "[ ok  ] $Label"
    } finally {
        Remove-Item -Path $tmp -ErrorAction SilentlyContinue
    }
}

# --- preflight: is Ollama up? ---------------------------------------------
Log '[start] Verify Ollama is reachable'
try {
    $resp = Invoke-WebRequest -Uri "$OllamaUrl/api/tags" -UseBasicParsing -TimeoutSec 10
    if ($resp.StatusCode -ne 200) {
        throw "Ollama at $OllamaUrl returned $($resp.StatusCode)"
    }
    Log '[ ok  ] Verify Ollama is reachable'
} catch {
    Log "[FAIL ] Ollama not reachable at $OllamaUrl : $($_.Exception.Message)"
    throw
}

# --- 1. scrape -------------------------------------------------------------
Set-Location $AcpRepo
Run-Native 'Scrape new ACP cases' 'uv' @('run','acp','--db','acp.db','scrape','--all','--from-year','2021')

# --- 2. classify -----------------------------------------------------------
Set-Location $AcpRepo
Run-Native 'Classify unclassified reasons (gemma4:e2b)' 'uv' @('run','acp','--db','acp.db','classify')

# --- 2b. LGMA sync (council-level refusals + appealed cases) --------------
Set-Location $AcpRepo
Run-Native 'Sync LGMA national planning-applications dataset' 'uv' @('run','acp','--db','acp.db','lgma-sync')

# --- 2c. Fetch council refusal reasons (agileapplications.ie portal) ------
# Picks up new refusals only (incremental via council_reasons_fetch table).
# Fast on weekly runs; full backfill is a one-off.
Set-Location $AcpRepo
Run-Native 'Fetch council refusal reasons' 'uv' @('run','python','scripts/fetch-council-reasons.py','--db','acp.db','--delay','1.0','--no-followup')

# --- 2d. Classify any new council reasons via Gemma 4 ---------------------
Set-Location $AcpRepo
Run-Native 'Classify new council reasons' 'uv' @('run','python','scripts/classify-council-reasons.py','--db','acp.db')

# --- 2e. Categorise new council reasons (taxonomy assignment) -------------
Set-Location $AcpRepo
Run-Native 'Categorise new council reasons' 'uv' @('run','python','scripts/categorize-council-reasons.py','--db','acp.db')

# --- 3. slim + split db ----------------------------------------------------
# The source acp.db carries operational columns + FTS/staging tables the
# website never queries, and at ~120 MB exceeds GitHub's 100 MB per-file
# limit. split-shipped-db.py slims it then writes a small core file plus 3
# balanced shards into the website's data/ dir (each well under the limit).
# The website ATTACHes the shards and unions them via temp views.
Set-Location $AcpRepo
Run-Native 'Slim + split DB into website repo' 'uv' @(
    'run','python','scripts/split-shipped-db.py',
    '--src', $DbSrc,
    '--dst-dir', $DataDir
)
foreach ($f in 'acp-core.db','acp-shard-1.db','acp-shard-2.db','acp-shard-3.db') {
    $p = Join-Path $DataDir $f
    if (Test-Path $p) {
        Log "        $f size: $([math]::Round((Get-Item $p).Length/1MB, 2)) MB"
    }
}

# --- 4. commit + push if changed ------------------------------------------
Set-Location $WebRepo
Log '[start] Commit + push if data changed'

# Pull first so we don't fight remote changes (autostash protects any
# uncommitted local edits, e.g. dev.log).
& git pull --rebase --autostash 2>&1 | Out-Null

# Did any of the shipped DB files change?
& git add data/acp-core.db data/acp-shard-1.db data/acp-shard-2.db data/acp-shard-3.db
& git diff --cached --quiet
$hasChanges = ($LASTEXITCODE -ne 0)
if (-not $hasChanges) {
    Log '        no DB changes since last run; skipping commit'
} else {
    $msg = "weekly: refresh decisions data ($(Get-Date -Format 'yyyy-MM-dd'))"
    & git commit -m $msg | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "git commit failed (exit $LASTEXITCODE)" }

    & git push origin main | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "git push failed (exit $LASTEXITCODE)" }
    Log "        pushed: $msg"
}
Log '[ ok  ] Commit + push if data changed'
Log '[done ] Weekly update complete'
