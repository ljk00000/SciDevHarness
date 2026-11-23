<#
.SYNOPSIS
Publish the self-contained x64 C# preview and build its separate installer.

.PARAMETER OutputDirectory
New directory for the payload and installer.

.PARAMETER Restore
Allow .NET to restore missing dependencies and the Windows x64 runtime packs.
Without this switch, publish uses the existing restore assets and local cache.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')]
    [string]$Version,
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,
    [switch]$Restore,
    [string]$IsccPath = '',
    [string]$SignThumbprint = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)
if (Test-Path -LiteralPath $output) {
    throw "Choose a new output directory: $output"
}
$payload = Join-Path $output 'payload'
New-Item -ItemType Directory -Path $payload | Out-Null
$publishArguments = @('-c', 'Release', '-r', 'win-x64', '--self-contained', 'true', "-p:Version=$Version")
if (-not $Restore) { $publishArguments += '--no-restore' }

Push-Location -LiteralPath (Join-Path $root 'dotnet')
try {
    dotnet publish 'src/SciDev.Desktop/SciDev.Desktop.csproj' @publishArguments -o (Join-Path $payload 'Workbench') | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "C# desktop publish failed (exit $LASTEXITCODE)." }
    dotnet publish 'src/SciDev.Cli/SciDev.Cli.csproj' @publishArguments -o (Join-Path $payload 'Cli') | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "C# CLI publish failed (exit $LASTEXITCODE)." }
}
finally {
    Pop-Location
}

$notices = Join-Path $payload 'Licenses'
New-Item -ItemType Directory -Path $notices | Out-Null
Copy-Item -LiteralPath (Join-Path $root 'LICENSE') -Destination (Join-Path $notices 'SciDevHarness-Apache-2.0.txt')
Copy-Item -Path (Join-Path $root 'packaging/windows/licenses/*.txt') -Destination $notices

$desktopPayload = Join-Path $payload 'Workbench'
$runtime = (Get-Content -LiteralPath (Join-Path $desktopPayload 'SciDev.Desktop.runtimeconfig.json') -Raw | ConvertFrom-Json).runtimeOptions
$assets = Get-Content -LiteralPath (Join-Path $root 'dotnet/src/SciDev.Desktop/obj/project.assets.json') -Raw | ConvertFrom-Json
foreach ($framework in $runtime.includedFrameworks) {
    $relative = "$($framework.name.ToLowerInvariant()).runtime.win-x64/$($framework.version)"
    $pack = $assets.packageFolders.PSObject.Properties.Name |
        ForEach-Object { Join-Path $_ $relative } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Container } |
        Select-Object -First 1
    if (-not $pack) { throw "Could not locate the license for $($framework.name) $($framework.version)." }
    $documents = Get-ChildItem -LiteralPath $pack -File | Where-Object { $_.Name -match '^(LICENSE|THIRD.PARTY.NOTICES)(\.|$)' }
    if (-not $documents) { throw "No licensing documents were found in $pack." }
    foreach ($document in $documents) {
        Copy-Item -LiteralPath $document.FullName -Destination (Join-Path $notices "$($framework.name)-$($document.Name)")
    }
}

& (Join-Path $PSScriptRoot 'build_installer.ps1') -CSharpPreview -Deployment $payload -Version $Version `
    -OutputDirectory $output -IsccPath $IsccPath -SignThumbprint $SignThumbprint
