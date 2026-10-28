param([string]$GoPath)
$ErrorActionPreference = 'Stop'
$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if (-not $GoPath) {
    $command = Get-Command go -ErrorAction SilentlyContinue
    if ($command) { $GoPath = $command.Source }
    else { $GoPath = Join-Path $repositoryRoot 'runtime/toolchains/go/bin/go.exe' }
}
if (-not (Test-Path -LiteralPath $GoPath -PathType Leaf)) {
    throw 'Install the Go version required by go.mod, then rerun build.ps1 (or pass -GoPath).'
}
$GoPath = (Resolve-Path -LiteralPath $GoPath).Path
$outputDirectory = Join-Path $repositoryRoot 'runtime/bin'
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
$binaryName = if ($env:OS -eq 'Windows_NT') { 'eidolon-whatsapp-bridge.exe' } else { 'eidolon-whatsapp-bridge' }
$outputPath = Join-Path $outputDirectory $binaryName
Push-Location $PSScriptRoot
try {
    & $GoPath build -mod=readonly -trimpath -o $outputPath .
    if ($LASTEXITCODE -ne 0) { throw 'Whatsmeow bridge build failed.' }
    Write-Output "Built $outputPath. Restart Eidolon to load the bridge."
} finally { Pop-Location }
