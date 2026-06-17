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
Vessel acoustic windows are centered on estimated hydrophone arrival time, using AIS source time plus `distance / sound_speed`. The default sound speed is 1500 m/s.
Each captured profile also includes a compact audio envelope for the selected event window, with the estimated vessel or CTD arrival time marked in the graph.

## Build clickable app

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\build_clickable_app.ps1
```

Open the generated file directly:

- `web/hydrophone_app.html`

The standalone file embeds the compact data JSON, CSS, and JavaScript. It uses Leaflet with OpenStreetMap tiles for the real basemap, so the basemap needs internet access. If the map library is unavailable, the app falls back to a plain local SVG layer.
The final map only shows vessels with a captured per-event acoustic profile. CTD events stay visible so you can compare casts with captured or missing audio windows.
The default vessel view shows the strongest acoustic candidates. Use the `All signals` toggle to show every vessel with a captured profile. AIS track lines are drawn only for the selected vessel.

## Full rebuild from raw processed data

Run the full processing chain when all recordings and AIS rows should be included:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\run_full_processing_and_build_app.ps1
```

This runs AIS preparation, audio feature extraction, AIS/audio joining, per-event acoustic profile generation, and the final standalone HTML build.
The first web-data pass includes all vessels so event profiles can be computed. The final pass filters vessels to `audio.captured == true`.

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
