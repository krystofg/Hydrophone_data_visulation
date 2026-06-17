# Overnight Processing Workflow

The goal is to prove the pipeline on one recording, then run the heavy work across the full recording set overnight.

## 1. Smoke test one recording

Use the already filtered example AIS file first. This avoids scanning the multi-GB raw AIS files while checking that audio decoding, timestamps, vessel filtering, and output writing work.

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\hydrophone_pipeline.py one-recording --ais-csv Data\analysis_example\oresund_ais.csv
```

Outputs:

- `outputs/processing/single_recording/recording_summary.json`
- `outputs/processing/single_recording/nearby_ais_rows.csv`
- `outputs/processing/single_recording/nearby_vessel_summary.csv`

## 2. Prepare AIS subset from raw unzipped files

This streams the large daily AIS CSV files and keeps only rows near the hydrophone and during the recording window.
If DuckDB is installed in `.codex_pydeps`, the script uses it automatically for this step.

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\hydrophone_pipeline.py prepare-ais
```

Output:

- `outputs/processing/ais_near_hydrophone.csv`

Force a specific engine if needed:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\hydrophone_pipeline.py prepare-ais --engine duckdb
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\hydrophone_pipeline.py prepare-ais --engine python
```

## 3. Smoke test audio feature extraction

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\hydrophone_pipeline.py process-audio --limit 1
```

Output:

- `outputs/processing/audio_features.csv`

## 4. Run all recordings overnight

Use `--resume` so the script skips rows already present in the output CSV if it is interrupted.

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\hydrophone_pipeline.py process-audio --resume
```

## 5. Join audio features with AIS context

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\hydrophone_pipeline.py join-audio-ais
```

Output:

- `outputs/processing/audio_ais_events.csv`

## 6. Build per-event acoustic profiles and the clickable app

After AIS and recording features exist, build the app data and per-event audio windows:

```powershell
$env:HYDROPHONE_MIC_SPACING_M = "0.98"
$env:HYDROPHONE_SOUND_SPEED_M_S = "1445"
# Optional, only after array-heading calibration:
# $env:HYDROPHONE_ARRAY_HEADING_DEG = "0"
powershell.exe -ExecutionPolicy Bypass -File scripts\build_clickable_app.ps1
```

The event profile step aligns each vessel by estimated hydrophone arrival time:

```text
arrival time = AIS source time + distance_to_hydrophone / 1500 m/s
```

The final app data hides vessels without a captured event acoustic profile.
Each captured event profile stores a compact audio envelope, not raw samples, so the web app can draw a quick sound graph without embedding the original WAV data.
The clickable app defaults to likely isolated nearby AIS/audio vessel candidates, draws radial geometry only for the selected vessel or CTD cast, and draws AIS tracks only for the selected vessel.

Stereo event profiles also compute a two-channel TDOA direction check. This is a consistency check, not proof that the selected vessel is the only source. With only two channels there is mirror ambiguity, so `HYDROPHONE_ARRAY_HEADING_DEG` must be calibrated before beam bearings and beam/AIS bearing error can be treated as map evidence. Without that setting, the app still shows array-relative angle candidates.

## 7. Estimate array heading before trusting beam bearings

After building event profiles once without `HYDROPHONE_ARRAY_HEADING_DEG`, estimate the array heading from close, loud, isolated AIS/audio candidates:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\estimate_array_heading.py
```

Outputs:

- `outputs/processing/array_heading_estimate.json`
- `outputs/processing/array_heading_candidates.csv`

Only use the suggested heading if the report status is `usable` or, after manual inspection, `tentative`. If the status is `inconsistent`, the vessels disagree and the heading should not be set blindly.

For a manual calibration pass using only vessels you trust:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\estimate_array_heading.py --include-vessel HAVFISKEN --include-vessel "FRENCH WARSHIP" --include-vessel VICTORY --min-confidence 0.1 --max-distance-km 3
```

If the report says the calibration is usable, rebuild event profiles and the app with both values:

```powershell
$env:HYDROPHONE_ARRAY_HEADING_DEG = "REPORTED_HEADING"
$env:HYDROPHONE_ARRAY_ANGLE_SIGN = "REPORTED_SIGN"
powershell.exe -ExecutionPolicy Bypass -File scripts\build_clickable_app.ps1
```

## 8. Target-vessel calibration from repeated windows

When you have a few vessels you trust visually, run a stronger calibration pass across many AIS-aligned audio windows for those vessels. This is better than calibrating from one event window per vessel because it tests whether the beam direction stays consistent during the pass.

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\calibrate_target_vessels.py --target-vessel HAVFISKEN --target-vessel "FRENCH WARSHIP" --target-vessel VICTORY --target-vessel SALSA --mic-spacing-m 0.98 --sound-speed-m-s 1445 --sample-step-seconds 10 --window-seconds 20
```

Outputs:

- `outputs/processing/target_vessel_audio_profiles.csv`
- `outputs/processing/target_vessel_calibration_candidates.csv`
- `outputs/processing/target_vessel_calibration.json`

Use the JSON `suggestedEnvironment` values only when `status` is `usable`, or after manually inspecting `target_vessel_calibration_candidates.csv` for a `tentative` result.

## One-command overnight run

After the smoke tests pass, run the full sequence with logging:

```powershell
$env:HYDROPHONE_MIC_SPACING_M = "0.98"
$env:HYDROPHONE_SOUND_SPEED_M_S = "1445"
# Optional, only after calibration:
# $env:HYDROPHONE_ARRAY_HEADING_DEG = "0"
powershell.exe -ExecutionPolicy Bypass -File scripts\run_full_processing_and_build_app.ps1
```

Logs are written to:

- `outputs/processing/logs`

Use `scripts\run_overnight_processing.ps1` only when you want the raw processing outputs without rebuilding the clickable app.

## Notes

- The WAV file names contain UTC Unix timestamps, so the pipeline can align audio with AIS without opening every audio file just to discover timing.
- Audio processing reads the WAV payloads and extracts restartable per-recording features: RMS, peak, crest factor, stereo correlation, and broad frequency-band levels.
- AIS preparation is streamed and bounded by a radius around the hydrophone, which keeps memory use low even when the raw AIS CSV files are several gigabytes.
- DuckDB is useful for the AIS preprocessing step because it can scan multi-GB CSV files directly and write the filtered subset without loading the full file into Python memory.
