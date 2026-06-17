$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

Set-Location $RepoRoot

$EventAudioArgs = @("scripts\build_event_audio_profiles.py")
if ($env:HYDROPHONE_MIC_SPACING_M) {
    $EventAudioArgs += @("--mic-spacing-m", $env:HYDROPHONE_MIC_SPACING_M)
}
if ($env:HYDROPHONE_ARRAY_HEADING_DEG) {
    $EventAudioArgs += @("--array-heading-deg", $env:HYDROPHONE_ARRAY_HEADING_DEG)
}
if ($env:HYDROPHONE_BEAM_FMIN_HZ) {
    $EventAudioArgs += @("--beam-fmin-hz", $env:HYDROPHONE_BEAM_FMIN_HZ)
}
if ($env:HYDROPHONE_BEAM_FMAX_HZ) {
    $EventAudioArgs += @("--beam-fmax-hz", $env:HYDROPHONE_BEAM_FMAX_HZ)
}

& $Python scripts\build_web_app_data.py --include-vessels-without-audio
& $Python @EventAudioArgs
& $Python scripts\build_web_app_data.py
& $Python scripts\build_standalone_app.py

$AppPath = Resolve-Path "web\hydrophone_app.html"
Write-Host "Clickable hydrophone app:"
Write-Host $AppPath
