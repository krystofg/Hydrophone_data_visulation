$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

Set-Location $RepoRoot

& $Python scripts\build_web_app_data.py --include-vessels-without-audio
& $Python scripts\build_event_audio_profiles.py
& $Python scripts\build_web_app_data.py
& $Python scripts\build_standalone_app.py

$AppPath = Resolve-Path "web\hydrophone_app.html"
Write-Host "Clickable hydrophone app:"
Write-Host $AppPath
