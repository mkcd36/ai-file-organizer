#requires -Version 5.1
<#
.SYNOPSIS
  Naming audit for one cabinet folder. Calls sonar_index.py audit.
.EXAMPLE
  .\scripts\check.ps1 -Folder "40 Money"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Folder,

    # Cabinet root. Default: cabinet_root from config.json (or config.example.json).
    [string]$Root = "",

    # Settings JSON. Default: config.json, else config.example.json.
    [string]$Config = "",

    [string]$Report = "",

    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$py = Join-Path $PSScriptRoot "sonar_index.py"
if (-not $Root) {
    $code = "import sys; sys.path.insert(0, r'$PSScriptRoot'); import sonar_index as s; print(s.load_settings($(if ($Config) { "r'$Config'" } else { 'None' }))['cabinet_root'])"
    $Root = (& python -c $code).Trim()
}

if (-not [IO.Path]::IsPathRooted($Folder)) {
    $Target = Join-Path $Root $Folder
} else {
    $Target = $Folder
}
$Target = [IO.Path]::GetFullPath($Target)
$RootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
if (-not $Target.StartsWith($RootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Folder is outside the cabinet root: $Target"
}
if (-not (Test-Path -LiteralPath $Target)) {
    throw "Folder not found: $Target"
}

$rel = $Target.Substring($RootFull.Length).TrimStart('\', '/')
$argList = @($py)
if ($Config) { $argList += @("--config", $Config) }
$argList += @("audit", "--root", "cabinet")
if ($rel) { $argList += @("--folder", $rel) }
if ($Report) { $argList += @("--report", $Report) }
if ($Json) { $argList += "--json" }
& python @argList
exit $LASTEXITCODE
