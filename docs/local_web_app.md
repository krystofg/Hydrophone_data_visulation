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
For stereo WAV files, the event profile step also computes a two-channel time-difference direction check. This is not source separation; it is a consistency test between the AIS/CTD bearing and the strongest left/right arrival delay in the event audio window.

Useful optional settings:

```powershell
$env:HYDROPHONE_MIC_SPACING_M = "0.98"
$env:HYDROPHONE_SOUND_SPEED_M_S = "1445"
# Optional, only after calibration:
# $env:HYDROPHONE_ARRAY_HEADING_DEG = "0"
# $env:HYDROPHONE_ARRAY_ANGLE_SIGN = "1"
# Optional frequency band for the direction check:
# $env:HYDROPHONE_BEAM_FMIN_HZ = "50"
# $env:HYDROPHONE_BEAM_FMAX_HZ = "900"
```

If `HYDROPHONE_ARRAY_HEADING_DEG` is not set, the app shows array-relative angle candidates only. With a calibrated heading, the app also shows beam bearing candidates, best beam/AIS match, bearing error, and dashed beam rays on the map.
Use `scripts\estimate_array_heading.py` to estimate both `HYDROPHONE_ARRAY_HEADING_DEG` and `HYDROPHONE_ARRAY_ANGLE_SIGN` from already-built event profiles before trusting beam bearings.

## Target-vessel calibration

For a better calibration pass, build many small audio windows around a few trusted vessels instead of using one event per vessel. The script samples AIS points for the selected target ships, shifts each point by acoustic propagation delay, extracts the matching hydrophone window, rejects weak/ambiguous windows, and estimates the array heading/sign from the surviving beam angles.

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\calibrate_target_vessels.py --target-vessel HAVFISKEN --target-vessel "FRENCH WARSHIP" --target-vessel VICTORY --target-vessel SALSA --mic-spacing-m 0.98 --sound-speed-m-s 1445 --sample-step-seconds 10 --window-seconds 20
```

Outputs:

- `outputs/processing/target_vessel_audio_profiles.csv`
- `outputs/processing/target_vessel_calibration_candidates.csv`
- `outputs/processing/target_vessel_calibration.json`

Only apply the reported `suggestedEnvironment` values when the JSON status is `usable`, or after manually inspecting a `tentative` result. If the status is `inconsistent`, the selected target windows disagree and the app should not use that heading.

## Build clickable app

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\build_clickable_app.ps1
```

Open the generated file directly:

- `web/hydrophone_app.html`

The standalone file embeds the compact data JSON, CSS, and JavaScript. It uses Leaflet with CARTO basemap tiles first and Esri tiles as a fallback, so the real basemap needs internet access. If the map library is unavailable, the app falls back to a plain local SVG layer.
The final map only shows vessels with a captured per-event acoustic profile. CTD events stay visible so you can compare casts with captured or missing audio windows.
The default vessel view shows likely isolated AIS/audio candidates within 2.5 km. Use the `All signals` toggle to show every vessel with a captured profile, including ambiguous shared audio windows. AIS track lines, radial bearing lines, distance rings, and calibrated beam rays are drawn only for the selected vessel or CTD cast.
CTD detection is based on the captured event audio profile, not only on raw recording time overlap.

## Vessel sound profile matching

After event profiles have been built, rank captured vessel windows by acoustic similarity:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\match_vessel_sound_profile.py --query-vessel SALSA --top 20
```

Outputs:

- `outputs/processing/vessel_sound_matches.csv`
- `outputs/processing/vessel_sound_fingerprints.csv`
- `outputs/processing/vessel_sound_match_report.json`

The matcher uses compact event-profile features: frequency-band shape, RMS/peak/crest, rough distance-corrected levels, waveform envelope shape, stereo correlation, and calibrated beam agreement when available. It ranks acoustic similarity between captured windows; it is not unique vessel identification unless the same vessel has repeated clean reference profiles.

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
