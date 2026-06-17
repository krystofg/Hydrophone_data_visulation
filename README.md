# Hydrophone Data Visualization

This repository contains hydrophone recordings, CTD casts, AIS vessel data, and early visualization scripts for the Oresund hydrophone analysis.

## Data layout

- `Data/Recordings`: timestamped WAV recordings from the hydrophone array.
- `Data/CTD`: Sea-Bird `.cnv` CTD profiles with GPS and UTC metadata in the headers.
- `Data/AIS`: raw Danish AIS day dumps for 2026-06-10 and 2026-06-11.
- `Data/analysis_example`: existing prototype notebooks, AIS subsets, spectrogram helpers, and beamforming helpers.
- `scripts`: small reusable scripts for maps and summaries.
- `docs`: planning notes for visualization work.

## Create the CTD and hydrophone map

Use the bundled Codex Python runtime or any Python 3.11+ installation:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\plot_ctd_hydrophone_map.py
```

This writes:

- `outputs/ctd_hydrophone_map.html`: interactive Leaflet map.
- `outputs/ctd_events.csv`: parsed CTD event table with recording-overlap flags.

Open `outputs/ctd_hydrophone_map.html` in a browser. The generated map uses Leaflet and OpenStreetMap tiles, so the basemap appears when the browser has internet access.

## Visualization direction

See `docs/visualization_plan.md` for the recommended path:

1. CTD and hydrophone map.
2. Time-linked CTD, AIS, and audio event view.
3. AIS vessel-track map.
4. Acoustic validation against spectrogram and beamforming output.
5. Optional 3D scene for exploratory ship size, speed, depth, and CTD profile views.

## Overnight processing

Use `scripts/hydrophone_pipeline.py` for the one-recording smoke test and overnight batch processing.
The AIS preprocessing step uses DuckDB automatically when it is available in `.codex_pydeps`, with a pure-Python fallback.

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\hydrophone_pipeline.py one-recording --ais-csv Data\analysis_example\oresund_ais.csv
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\hydrophone_pipeline.py prepare-ais
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\hydrophone_pipeline.py process-audio --resume
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\hydrophone_pipeline.py join-audio-ais
```

Or run the complete sequence with logs:

```powershell
.\scripts\run_overnight_processing.ps1
```

See `docs/overnight_processing.md` for the step-by-step workflow.

## Clickable local web app

Build a compact, standalone HTML app:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\build_clickable_app.ps1
```

Open `web/hydrophone_app.html` in a browser.

The app shows CTD casts, the hydrophone location, and only vessels with captured per-event acoustic profiles on a Leaflet basemap. Vessel profiles are aligned by estimated hydrophone arrival time, using AIS source time plus distance divided by sound speed in water, and each captured profile includes a waveform plus sound-intensity gradient. The default view shows likely isolated nearby AIS/audio candidates; `All signals` reveals every captured vessel, including ambiguous shared audio windows. See `docs/local_web_app.md`.

Event profiles also compute a two-channel TDOA direction check from the stereo WAV data. Without array heading calibration this is only array-relative; set `HYDROPHONE_ARRAY_HEADING_DEG` to convert it into map bearings.
Use `scripts\estimate_array_heading.py` after the first app build to estimate the heading and `HYDROPHONE_ARRAY_ANGLE_SIGN` from trusted AIS/audio events.

Build repeated calibration windows for trusted anchor vessels:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\calibrate_target_vessels.py --target-vessel HAVFISKEN --target-vessel "FRENCH WARSHIP" --target-vessel VICTORY --target-vessel SALSA --mic-spacing-m 0.98 --sound-speed-m-s 1445 --sample-step-seconds 10 --window-seconds 20
```

This writes `outputs/processing/target_vessel_calibration.json`. Use the reported `suggestedEnvironment` values only when the status is `usable` or after manually checking a `tentative` result.

Rank similar captured vessel sound profiles:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\match_vessel_sound_profile.py --query-vessel SALSA --top 20
```

For full processing from raw AIS/audio outputs through the final app:

```powershell
$env:HYDROPHONE_MIC_SPACING_M = "0.98"
$env:HYDROPHONE_SOUND_SPEED_M_S = "1445"
# Optional, replace with the calibrated geographic heading of array angle 0:
# $env:HYDROPHONE_ARRAY_HEADING_DEG = "0"
powershell.exe -ExecutionPolicy Bypass -File scripts\run_full_processing_and_build_app.ps1
```
