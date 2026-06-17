$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Port = 8787

Set-Location $RepoRoot

& $Python scripts\build_web_app_data.py

$Existing = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
if ($Existing) {
    Write-Host "Local app already has a process on port $Port."
} else {
    Start-Process `
        -FilePath $Python `
        -ArgumentList "-m", "http.server", "$Port", "--bind", "127.0.0.1", "--directory", "web" `
        -WorkingDirectory $RepoRoot `
        -WindowStyle Hidden
}

Write-Host "Hydrophone local app: http://127.0.0.1:$Port/"
