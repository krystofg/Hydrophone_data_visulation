$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Pipeline = Join-Path $RepoRoot "scripts\hydrophone_pipeline.py"
$LogDir = Join-Path $RepoRoot "outputs\processing\logs"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $RepoRoot

function Invoke-PipelineStep {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name,

        [Parameter(Mandatory = $true)]
        [string[]] $Arguments
    )

    $Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $LogPath = Join-Path $LogDir "$Timestamp-$Name.log"

    Write-Host "Starting $Name..."
    Write-Host "Log: $LogPath"

    & $Python $Pipeline @Arguments 2>&1 | Tee-Object -FilePath $LogPath
    if ($LASTEXITCODE -ne 0) {
        throw "Pipeline step failed: $Name"
    }

    Write-Host "Finished $Name."
}

Invoke-PipelineStep -Name "prepare-ais" -Arguments @("prepare-ais")
Invoke-PipelineStep -Name "process-audio" -Arguments @("process-audio", "--resume")
Invoke-PipelineStep -Name "join-audio-ais" -Arguments @("join-audio-ais")

Write-Host "Overnight processing complete."
