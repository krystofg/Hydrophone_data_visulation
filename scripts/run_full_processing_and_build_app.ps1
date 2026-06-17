$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Pipeline = Join-Path $RepoRoot "scripts\hydrophone_pipeline.py"
$LogDir = Join-Path $RepoRoot "outputs\processing\logs"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $RepoRoot

function Invoke-Step {
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
    & $Python @Arguments 2>&1 | Tee-Object -FilePath $LogPath
    if ($LASTEXITCODE -ne 0) {
        throw "Step failed: $Name"
    }
    Write-Host "Finished $Name."
}

Invoke-Step -Name "prepare-ais-full" -Arguments @($Pipeline, "prepare-ais", "--engine", "auto")
Invoke-Step -Name "process-audio-full" -Arguments @($Pipeline, "process-audio", "--resume")
Invoke-Step -Name "join-audio-ais-full" -Arguments @($Pipeline, "join-audio-ais")
Invoke-Step -Name "build-web-data-initial" -Arguments @("scripts\build_web_app_data.py", "--include-vessels-without-audio")
Invoke-Step -Name "build-event-audio-profiles-full" -Arguments @("scripts\build_event_audio_profiles.py")
Invoke-Step -Name "build-web-data-final" -Arguments @("scripts\build_web_app_data.py")
Invoke-Step -Name "build-standalone-app" -Arguments @("scripts\build_standalone_app.py")

Write-Host "Full processing and app build complete."
Write-Host (Resolve-Path "web\hydrophone_app.html")
