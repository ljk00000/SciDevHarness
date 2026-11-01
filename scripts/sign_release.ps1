<#
.SYNOPSIS
Sign or verify SciDevHarness release binaries with Authenticode.

.DESCRIPTION
Signs PE/MSI artifacts with a certificate already installed in the current
user's certificate store. The thumbprint is an explicit argument; no password
or certificate file is accepted or written. Timestamping is required for a
valid signature after the certificate expires.

.PARAMETER Path
File or directory containing .exe/.dll/.msi artifacts.

.PARAMETER Thumbprint
40-hex SHA-1 thumbprint of the code-signing certificate.

.PARAMETER TimestampUrl
RFC 3161 timestamp endpoint. Must be HTTPS.

.PARAMETER VerifyOnly
Only run signtool verify; do not sign.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$Thumbprint,
    [string]$TimestampUrl = "https://timestamp.digicert.com",
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"
$thumbprintNormalized = ($Thumbprint -replace '\s', '').ToUpperInvariant()
if ($thumbprintNormalized -notmatch '^[0-9A-F]{40}$') {
    throw "Thumbprint must be exactly 40 hexadecimal characters."
}
if (-not $TimestampUrl.StartsWith("https://", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "TimestampUrl must use HTTPS."
}

$target = (Resolve-Path -LiteralPath $Path).Path
$extensions = @(".exe", ".dll", ".msi")
if (Test-Path -LiteralPath $target -PathType Leaf) {
    $targets = @(Get-Item -LiteralPath $target)
} else {
    $targets = @(Get-ChildItem -LiteralPath $target -Recurse -File |
        Where-Object { $extensions -contains $_.Extension.ToLowerInvariant() })
}
if ($targets.Count -eq 0) {
    throw "No .exe/.dll/.msi artifacts found under $target."
}

function Find-SignTool {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (Test-Path -LiteralPath $kits) {
        $candidate = Get-ChildItem -LiteralPath $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($candidate) {
            return $candidate.FullName
        }
    }
    throw "signtool.exe not found. Install the Windows SDK or add signtool to PATH."
}

$signTool = Find-SignTool
$signed = 0
if (-not $VerifyOnly) {
    foreach ($file in $targets) {
        & $signTool sign /fd SHA256 /tr $TimestampUrl /td SHA256 /sha1 $thumbprintNormalized $file.FullName
        if ($LASTEXITCODE -ne 0) {
            throw "Signing failed for $($file.FullName) (exit $LASTEXITCODE)."
        }
        $signed += 1
    }
}

foreach ($file in $targets) {
    & $signTool verify /pa /v $file.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "Signature verification failed for $($file.FullName) (exit $LASTEXITCODE)."
    }
}

[pscustomobject]@{
    ok = $true
    signed = $signed
    verified = $targets.Count
    thumbprint = $thumbprintNormalized
    verify_only = [bool]$VerifyOnly
} | ConvertTo-Json -Compress
