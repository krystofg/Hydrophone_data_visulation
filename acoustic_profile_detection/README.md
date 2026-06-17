# Acoustic Ship Profile Detector

This is a separate experiment for detecting a vessel from hydrophone audio without using AIS during detection.

The intended workflow is:

1. Use AIS only once to find a clean reference window for one target vessel, such as `FRENCH WARSHIP` / MMSI `228000000`.
2. Build an acoustic template from the reference audio window.
3. Scan all recordings with only the acoustic template.
4. Open a standalone HTML report with the candidate detections.

The detector uses multi-bin spectral shape, time-envelope shape, stereo correlation, and a simple two-channel delay feature. It is an exploratory matcher, not a proof of vessel identity.

## Quick Smoke Test

These commands are intentionally small and should finish quickly:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py inspect --recordings-dir Data\Recordings

& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py ais-candidates --ais-csv outputs\processing\ais_near_hydrophone.csv --target "FRENCH WARSHIP" --max-distance-km 8 --output-csv outputs\acoustic_profile_detection\french_warship_candidates.csv

& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py build-template --ais-csv outputs\processing\ais_near_hydrophone.csv --target "FRENCH WARSHIP" --recordings-dir Data\Recordings --output-template outputs\acoustic_profile_detection\french_warship_template.json --output-windows outputs\acoustic_profile_detection\french_warship_template_windows.csv --window-seconds 90 --step-seconds 10 --max-distance-km 5

& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py scan --template outputs\acoustic_profile_detection\french_warship_template.json --recordings-dir Data\Recordings --output-csv outputs\acoustic_profile_detection\french_warship_scan_smoke.csv --from-utc 2026-06-10T11:50:00Z --to-utc 2026-06-10T12:05:00Z --keep-all --workers 0

& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py view --template outputs\acoustic_profile_detection\french_warship_template.json --detections outputs\acoustic_profile_detection\french_warship_scan_smoke.csv --output-html outputs\acoustic_profile_detection\french_warship_scan_smoke.html
```

Open `outputs/acoustic_profile_detection/french_warship_scan_smoke.html`.

## Full Run

This scans every WAV file and may take a while:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py scan --template outputs\acoustic_profile_detection\french_warship_template.json --recordings-dir Data\Recordings --output-csv outputs\acoustic_profile_detection\french_warship_scan_full.csv --threshold 72 --workers 0

& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py view --template outputs\acoustic_profile_detection\french_warship_template.json --detections outputs\acoustic_profile_detection\french_warship_scan_full.csv --output-html outputs\acoustic_profile_detection\french_warship_scan_full.html
```

## Notes

- Use `outputs/processing/ais_near_hydrophone.csv` first if available. It is much smaller than the raw daily AIS files.
- If you want to use raw AIS directly, pass `Data\AIS\aisdk-2026-06-11\aisdk-2026-06-11.csv` to `--ais-csv`. That will stream the file but can still take time.
- Detection rows with `score >= threshold` are candidates. The `distance_proxy` column is only a relative loudness cue, not a calibrated range estimate.
- `--workers 0` uses most CPU cores. Use `--workers 1` for the old single-process behavior, or set a fixed value such as `--workers 6` if you want to leave the machine more responsive.
