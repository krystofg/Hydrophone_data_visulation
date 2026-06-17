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

## One-command overnight run

After the smoke tests pass, run the full sequence with logging:

```powershell
.\scripts\run_overnight_processing.ps1
```

Logs are written to:

- `outputs/processing/logs`

## Notes

- The WAV file names contain UTC Unix timestamps, so the pipeline can align audio with AIS without opening every audio file just to discover timing.
- Audio processing reads the WAV payloads and extracts restartable per-recording features: RMS, peak, crest factor, stereo correlation, and broad frequency-band levels.
- AIS preparation is streamed and bounded by a radius around the hydrophone, which keeps memory use low even when the raw AIS CSV files are several gigabytes.
- DuckDB is useful for the AIS preprocessing step because it can scan multi-GB CSV files directly and write the filtered subset without loading the full file into Python memory.
