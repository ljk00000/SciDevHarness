<#
.SYNOPSIS
Build the SciDevHarness per-user Windows installer from a staged deployment.

.DESCRIPTION
Requires Inno Setup 6 (ISCC.exe). The script never downloads dependencies,
executes the packaged application, or installs the result. It validates the
deployment, invokes ISCC, optionally signs the setup executable with
sign_release.ps1, and prints the installer path and SHA-256.

.PARAMETER Deployment
Directory produced by pyside6-deploy containing scidev_client.exe.

.PARAMETER Version
Numeric version, e.g. 0.2.0. It becomes the installer filename.

.PARAMETER OutputDirectory
Where the setup executable is written. Defaults to dist beside the checkout.

.PARAMETER IsccPath
Explicit path to ISCC.exe. When omitted, common Inno Setup locations and PATH
are checked.

.PARAMETER SignThumbprint
Optional 40-hex Authenticode certificate thumbprint. Signing happens only after
ISCC succeeds and uses scripts/sign_release.ps1.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Deployment,
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$OutputDirectory = "",
    [string]$IsccPath = "",
    [string]$SignThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$deploymentPath = (Resolve-Path -LiteralPath $Deployment).Path
$executable = Join-Path $deploymentPath "scidev_client.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "deployment does not contain scidev_client.exe: $deploymentPath"
}
if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
    throw "Version must be numeric major.minor.patch, got: $Version"
}
if ($SignThumbprint -and $SignThumbprint -notmatch '^[0-9A-Fa-f]{40}$') {
    throw "SignThumbprint must be a 40-hex SHA-1 certificate thumbprint"
}
if (-not ($TimestampUrl.StartsWith("https://", [System.StringComparison]::OrdinalIgnoreCase) -or
        $TimestampUrl.StartsWith("http://", [System.StringComparison]::OrdinalIgnoreCase))) {
    throw "TimestampUrl must use HTTP or HTTPS"
}

if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $root "dist"
}
$outputPath = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null

if (-not $IsccPath) {
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) {
        $IsccPath = $command.Source
    } else {
        $programFilesX86 = ${env:ProgramFiles(x86)}
        $candidates = @(
            $(if ($programFilesX86) { Join-Path $programFilesX86 "Inno Setup 6\ISCC.exe" }),
            $(if ($env:ProgramFiles) { Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe" })
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
        if ($candidates) {
            $IsccPath = @($candidates)[0]
        }
    }
}
if (-not $IsccPath -or -not (Test-Path -LiteralPath $IsccPath -PathType Leaf)) {
    throw "ISCC.exe was not found. Install Inno Setup 6 or pass -IsccPath explicitly."
}

$iss = Join-Path $root "packaging\windows\scidev.iss"
if (-not (Test-Path -LiteralPath $iss -PathType Leaf)) {
    throw "installer script is missing: $iss"
}

& $IsccPath "/Qp" "/DAppVersion=$Version" "/DSourceDir=$deploymentPath" "/DOutputDir=$outputPath" $iss
if ($LASTEXITCODE -ne 0) {
    throw "ISCC failed with exit code $LASTEXITCODE"
}

$installer = Join-Path $outputPath "SciDevHarness-Setup-$Version.exe"
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw "ISCC succeeded but the expected installer was not found: $installer"
}

if ($SignThumbprint) {
    $signScript = Join-Path $root "scripts\sign_release.ps1"
    & $signScript -Path $installer -Thumbprint $SignThumbprint -TimestampUrl $TimestampUrl
    if ($LASTEXITCODE -ne 0) {
        throw "sign_release.ps1 failed with exit code $LASTEXITCODE"
    }
}

$hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
$size = (Get-Item -LiteralPath $installer).Length
[pscustomobject]@{
    path = $installer
    version = $Version
    bytes = $size
    sha256 = $hash
    signed = [bool]$SignThumbprint
} | ConvertTo-Json -Compress
