#requires -Version 5.1
<#
.SYNOPSIS
  Reverse an undo log (CSV with Source,Dest). Dry run by default; pass -Apply.
.DESCRIPTION
  Moves Dest back to Source only when Dest still exists and the original
  Source path is free. Never overwrites, never deletes. Rows are processed
  newest first so chained moves unwind in order.
.EXAMPLE
  .\scripts\undo-manifest.ps1 -Log plans\undo-log-20260110-091200.csv -Apply
#>
param(
    [Parameter(Mandatory = $true)][string]$Log,
    [switch]$Apply
)

Set-StrictMode -Version Latest
if (-not (Test-Path -LiteralPath $Log)) { throw "Undo log not found: $Log" }
$rows = @(Import-Csv -LiteralPath $Log)
if ($rows.Count -eq 0) { Write-Host "Undo log has no rows (a failed apply?). Nothing to do."; exit 0 }
[array]::Reverse($rows)
$restored = 0; $skipped = 0; $errors = 0

foreach ($r in $rows) {
    $src = [string]$r.Source; $dst = [string]$r.Dest
    if (-not (Test-Path -LiteralPath $dst)) { Write-Host "SKIP    $dst (dest missing)"; $skipped++; continue }
    if (Test-Path -LiteralPath $src) { Write-Host "SKIP    $dst (original path already taken)"; $skipped++; continue }
    if (-not $Apply) { Write-Host "WOULD   $dst -> $src"; continue }
    try {
        $parent = Split-Path $src -Parent
        if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
        Move-Item -LiteralPath $dst -Destination $src -ErrorAction Stop
        Write-Host "RESTORED $dst -> $src"
        $restored++
    } catch {
        Write-Host "ERROR   $dst : $($_.Exception.Message)"
        $errors++
    }
}
if ($Apply) { Write-Host "restored=$restored  skipped=$skipped  errors=$errors" }
else { Write-Host ""; Write-Host "Dry run only. Re-run with -Apply to restore." }
