#requires -Version 5.1
<#
.SYNOPSIS
  Apply a move/rename manifest (CSV with Source,Dest[,Group,Reason,DateSource]).
  Dry run by default; pass -Apply to move.
.DESCRIPTION
  Never overwrites and never deletes. Skips a row when the source is missing,
  the destination already exists, or the file is locked. Creates parent
  folders. With -Apply, successful moves are written to
  plans\undo-log-<yyyyMMdd-HHmmss>.csv (Source,Dest,Time). No undo log is
  written when nothing moved.
.EXAMPLE
  .\scripts\apply-manifest.ps1 -Manifest plans\40-money-rename-manifest.csv
  .\scripts\apply-manifest.ps1 -Manifest plans\40-money-rename-manifest.csv -Apply
#>
param(
    [Parameter(Mandatory = $true)][string]$Manifest,
    [switch]$Apply
)

Set-StrictMode -Version Latest
if (-not (Test-Path -LiteralPath $Manifest)) { throw "Manifest not found: $Manifest" }
$rows = @(Import-Csv -LiteralPath $Manifest)
$moved = 0; $skipped = 0; $errors = 0
$undoRows = @()

foreach ($r in $rows) {
    $src = [string]$r.Source; $dst = [string]$r.Dest
    if (-not $src -or -not $dst) { Write-Host "SKIP    (blank Source/Dest row)"; $skipped++; continue }
    $name = Split-Path $src -Leaf
    if ($name -ieq 'desktop.ini' -or $src.ToLower().EndsWith('.lnk')) { Write-Host "SKIP    $src (shortcut/system file)"; $skipped++; continue }
    if (-not (Test-Path -LiteralPath $src)) { Write-Host "SKIP    $src (source missing)"; $skipped++; continue }
    if (Test-Path -LiteralPath $dst) { Write-Host "SKIP    $src -> $dst (destination exists)"; $skipped++; continue }
    if (-not $Apply) { Write-Host "WOULD   $src -> $dst"; continue }
    try {
        $parent = Split-Path $dst -Parent
        if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
        Move-Item -LiteralPath $src -Destination $dst -ErrorAction Stop
        $undoRows += [pscustomobject]@{ Source = $src; Dest = $dst; Time = (Get-Date).ToString('s') }
        Write-Host "MOVED   $src -> $dst"
        $moved++
    } catch {
        Write-Host "ERROR   $src : $($_.Exception.Message)"
        $errors++
    }
}

if ($Apply) {
    if ($undoRows.Count -gt 0) {
        $plans = Join-Path (Split-Path $PSScriptRoot -Parent) 'plans'
        if (-not (Test-Path -LiteralPath $plans)) { New-Item -ItemType Directory -Path $plans | Out-Null }
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $log = Join-Path $plans "undo-log-$stamp.csv"
        $n = 2
        while (Test-Path -LiteralPath $log) { $log = Join-Path $plans "undo-log-$stamp-$n.csv"; $n++ }
        $undoRows | Export-Csv -LiteralPath $log -NoTypeInformation -Encoding UTF8
        Write-Host "UNDO LOG $log"
    }
    Write-Host "moved=$moved  skipped=$skipped  errors=$errors"
} else {
    Write-Host ""
    Write-Host "Dry run only ($($rows.Count) rows, $skipped would skip). Re-run with -Apply to move."
}
