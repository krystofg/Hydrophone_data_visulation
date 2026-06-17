# Local Web App

The local web app gives a fast visual view of the processed hydrophone, AIS, and CTD data.

## Build app data

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\build_web_app_data.py
```

This writes:

- `web/data/app_data.json`

The JSON is generated from processed outputs, not raw 30 GB recordings or raw AIS dumps. It is ignored by Git.
The clickable build also creates `outputs/processing/event_audio_profiles.csv`, which contains separate acoustic windows for selected vessels and CTD events.

## Build clickable app

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\build_clickable_app.ps1
```

Open the generated file directly:

- `web/hydrophone_app.html`

The standalone file embeds the compact data JSON, CSS, and JavaScript. It does not use OpenStreetMap, Leaflet, remote map tiles, or a localhost server.

## Full rebuild from raw processed data

Run the full processing chain when all recordings and AIS rows should be included:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\run_full_processing_and_build_app.ps1
```

This runs AIS preparation, audio feature extraction, AIS/audio joining, per-event acoustic profile generation, and the final standalone HTML build.

## Data flow

The app prefers these processed files when they exist:

- `outputs/processing/ais_near_hydrophone.csv`
- `outputs/processing/audio_ais_events.csv`
- `outputs/ctd_events.csv`

If the full overnight outputs do not exist yet, it falls back to smoke-test outputs so the interface can be developed immediately.

## Optional dev server

For iterative web development, `web/index.html` can still be served from a local web server:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\run_local_app.ps1
```

## Git boundary

Keep raw and generated data out of Git:

- `Data/`
- `outputs/`
- `web/data/*.json`

Commit the scripts and web UI only.
