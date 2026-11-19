#requires -Version 5.1
<#
.SYNOPSIS
  Simple folder-to-folder mover. Dry run by default; pass -Apply to move.
.DESCRIPTION
  Top-level files only. Skips .lnk, .ini and desktop.ini. Never overwrites:
  if the target exists the file is skipped and reported. With -Apply, every
  move is written to plans\undo-log-<yyyyMMdd-HHmmss>.csv (Source,Dest,Time);
  reverse it with undo-manifest.ps1 -Log <csv> -Apply.
.EXAMPLE
  .\scripts\organize.ps1 -Source "$env:USERPROFILE\Desktop\Scans" -Dest "$env:USERPROFILE\Documents\Family\00 Inbox"
#>
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Dest,
    [switch]$Apply
)

$skipExt = @('.lnk', '.ini')
$skipNames = @('desktop.ini')

if (-not (Test-Path -LiteralPath $Source)) {
    throw "Source not found: $Source"
}

$files = @(Get-ChildItem -LiteralPath $Source -File | Where-Object {
    ($skipNames -notcontains $_.Name.ToLower()) -and ($skipExt -notcontains $_.Extension.ToLower())
})

if ($files.Count -eq 0) {
    Write-Host "Nothing to move in $Source"
    exit 0
}

$undoRows = @()
$moved = 0; $skipped = 0; $errors = 0
if ($Apply -and -not (Test-Path -LiteralPath $Dest)) {
    New-Item -ItemType Directory -Path $Dest | Out-Null
}

foreach ($f in $files) {
    $target = Join-Path $Dest $f.Name
    if (Test-Path -LiteralPath $target) {
        Write-Host "SKIP    $($f.Name) (already exists at dest)"
        $skipped++
        continue
    }
    if ($Apply) {
        try {
            Move-Item -LiteralPath $f.FullName -Destination $target -ErrorAction Stop
            Write-Host "MOVED   $($f.FullName) -> $target"
            $undoRows += [pscustomobject]@{ Source = $f.FullName; Dest = $target; Time = (Get-Date).ToString('s') }
            $moved++
        } catch {
            Write-Host "ERROR   $($f.FullName): $($_.Exception.Message)"
            $errors++
        }
    }
    else {
        Write-Host "WOULD   $($f.FullName) -> $target"
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
    Write-Host "Dry run only. Re-run with -Apply to move."
}
