param(
    [Parameter(Mandatory = $true)]
    [string]$ApplicationDirectory
)

$ErrorActionPreference = "Stop"
$applicationRoot = [System.IO.Path]::GetFullPath($ApplicationDirectory)
if (-not (Test-Path -LiteralPath $applicationRoot -PathType Container)) {
    throw "The application directory does not exist: $applicationRoot"
}

$sampleSource = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\examples\tetris"))
$sampleDestination = Join-Path $applicationRoot "examples\tetris"
$requiredFiles = @("index.html", "README.txt")
foreach ($fileName in $requiredFiles) {
    $sourceFile = Join-Path $sampleSource $fileName
    if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
        throw "Required default Tetris project file is missing: $sourceFile"
    }
}

New-Item -ItemType Directory -Path $sampleDestination -Force | Out-Null
foreach ($fileName in $requiredFiles) {
    Copy-Item -LiteralPath (Join-Path $sampleSource $fileName) -Destination $sampleDestination -Force
}
