param(
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$stageRoot = [System.IO.Path]::GetFullPath($Destination)
$repoPrefix = $repoRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if ($stageRoot.Equals($repoRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    $stageRoot.StartsWith($repoPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "The release staging directory must be outside the source checkout."
}
if (Test-Path -LiteralPath $stageRoot) {
    throw "The release staging directory already exists; refusing to overwrite it: $stageRoot"
}

New-Item -ItemType Directory -Path $stageRoot | Out-Null
$releaseFiles = @(
    "scidev_client.py",
    "scidev_core.py",
    "scidev_process.py",
    "scidev_acceptance.py",
    "scidev_usage.py",
    "scidev_context.py",
    "scidev_environment.py",
    "scidev_patching.py",
    "scidev_memory.py",
    "scidev_multi_agent.py",
    "scidev_planning.py",
    "scidev_policy.py",
    "scidev_verification.py",
    "scidev_browser.py",
    "scidev_icons.py",
    "scidev_ui.py",
    "scidev_preferences.py",
    "scidev_settings.py",
    "scidev_highlighting.py",
    "scidev_visual.py",
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "pysidedeploy.spec",
    "requirements.txt",
    "requirements-build.txt",
    "start_client.bat",
    "start_qwen_local.bat",
    "scripts\bootstrap.py",
    "scripts\smoke_local_ollama.py",
    "ui\qml\DevelopmentTree.qml",
    "ui\icons\chevron-down.svg",
    "ui\icons\chevron-up.svg"
)

foreach ($relativePath in $releaseFiles) {
    $sourcePath = Join-Path $repoRoot $relativePath
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Required release input is missing: $relativePath"
    }
    $destinationPath = Join-Path $stageRoot $relativePath
    $destinationParent = Split-Path -Parent $destinationPath
    New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
    Copy-Item -LiteralPath $sourcePath -Destination $destinationPath
}

$sampleRelativePath = "examples\tetris"
$sampleSource = Join-Path $repoRoot $sampleRelativePath
$sampleDestination = Join-Path $stageRoot $sampleRelativePath
$requiredSampleFiles = @("index.html", "README.txt")
foreach ($fileName in $requiredSampleFiles) {
    $sampleFile = Join-Path $sampleSource $fileName
    if (-not (Test-Path -LiteralPath $sampleFile -PathType Leaf)) {
        throw "Required Tetris sample input is missing: $sampleRelativePath\$fileName"
    }
}
New-Item -ItemType Directory -Path $sampleDestination -Force | Out-Null
foreach ($fileName in $requiredSampleFiles) {
    Copy-Item -LiteralPath (Join-Path $sampleSource $fileName) -Destination $sampleDestination
}

Write-Output $stageRoot
