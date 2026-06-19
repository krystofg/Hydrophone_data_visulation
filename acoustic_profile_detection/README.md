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

## Distance and Map Validation

This view aligns each acoustic window with the vessel's AIS source position after
accounting for sound propagation delay. It shows the track on a map, profile score
against true distance, a binned empirical detection range, and the two-channel
direction consistency check:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py validate --template outputs\acoustic_profile_detection\french_warship_template.json --detections outputs\acoustic_profile_detection\french_warship_scan_smoke.csv --ais-csv outputs\processing\ais_near_hydrophone.csv --target-mmsi 228000000 --threshold 72 --output-html outputs\acoustic_profile_detection\french_warship_distance_validation.html
```

The map needs an internet connection for OpenStreetMap background tiles; all
track, score, and chart data are embedded in the HTML file.

## Acoustic-only Track Reconstruction

This experiment reconstructs coordinates before loading AIS. It uses one control
point from the template, multi-bin relative level for range, band-limited stereo
GCC for bearing, array geometry, and track continuity. AIS is loaded afterwards
only to draw the truth track and calculate position error:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py reconstruct-track --template outputs\acoustic_profile_detection\french_warship_template_smoke.json --detections outputs\acoustic_profile_detection\french_warship_scan_full.csv --recordings-dir Data\Recordings --ais-csv outputs\processing\ais_near_hydrophone.csv --target-mmsi 228000000 --threshold 72 --minutes-before 35 --minutes-after 35 --output-html outputs\acoustic_profile_detection\french_warship_acoustic_track.html
```

Omit `--ais-csv` to generate the same acoustic coordinates without loading any
AIS ground truth. The two-element array has mirror ambiguity; the single control
point's bearing and course select the initial branch. Range from sound level is
experimental because vessel source level, aspect, propagation, and harbour noise
are not constant.

## Multi-vessel Profile Report

This report keeps only vessels with enough isolated, moving, non-clipped
reference windows. The vessel buttons update the map, metrics, and both passage
directions in place:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\fleet_profile_report.py --output-html outputs\acoustic_profile_detection\fleet_profile_validation.html
```

The default set is `FRENCH WARSHIP`, `VICTORY`, `SALSA`, and `HAVFISKEN`.
Override it with repeated `--ship NAME` arguments. `HAVFISKEN` uses only its
non-clipped 0.5-3 km moving subset and is marked with a near-field warning.

The fleet page is a preliminary view built from already processed, AIS-aligned
WAV windows. To test unique vessel identification, build a withheld 14-bin
template and run an AIS-free full scan for each additional vessel:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py build-template --ais-csv outputs\processing\ais_near_hydrophone.csv --target-mmsi 219025421 --recordings-dir Data\Recordings --min-distance-km 0.5 --max-distance-km 3 --min-sog 1 --output-template outputs\acoustic_profile_detection\victory_template.json --output-windows outputs\acoustic_profile_detection\victory_template_windows.csv

& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py build-template --ais-csv outputs\processing\ais_near_hydrophone.csv --target-mmsi 265676630 --recordings-dir Data\Recordings --min-distance-km 0.5 --max-distance-km 3 --min-sog 1 --output-template outputs\acoustic_profile_detection\salsa_template.json --output-windows outputs\acoustic_profile_detection\salsa_template_windows.csv

& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py scan --template outputs\acoustic_profile_detection\victory_template.json --recordings-dir Data\Recordings --output-csv outputs\acoustic_profile_detection\victory_scan_full.csv --threshold 72 --keep-all --workers 0

& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py scan --template outputs\acoustic_profile_detection\salsa_template.json --recordings-dir Data\Recordings --output-csv outputs\acoustic_profile_detection\salsa_scan_full.csv --threshold 72 --keep-all --workers 0
```

These two scans are the expensive step. AIS must be joined only afterwards for
range validation and the cross-template confusion matrix.

For `HAVFISKEN`, first build the clean non-clipped reference and then run the
same AIS-free scan:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py build-template --ais-csv outputs\processing\ais_near_hydrophone.csv --target-mmsi 219021235 --recordings-dir Data\Recordings --min-distance-km 0.5 --max-distance-km 3 --min-sog 1 --output-template outputs\acoustic_profile_detection\havfisken_template.json --output-windows outputs\acoustic_profile_detection\havfisken_template_windows.csv

& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py scan --template outputs\acoustic_profile_detection\havfisken_template.json --recordings-dir Data\Recordings --output-csv outputs\acoustic_profile_detection\havfisken_scan_full.csv --threshold 72 --keep-all --workers 0
```

After every full scan exists, build the true winner-takes-all confusion matrix:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\fleet_confusion_matrix.py --ais-csv outputs\processing\ais_near_hydrophone.csv --scan "FRENCH WARSHIP=228000000=outputs\acoustic_profile_detection\french_warship_scan_full.csv" --scan "VICTORY=219025421=outputs\acoustic_profile_detection\victory_scan_full.csv" --scan "SALSA=265676630=outputs\acoustic_profile_detection\salsa_scan_full.csv" --scan "HAVFISKEN=219021235=outputs\acoustic_profile_detection\havfisken_scan_full.csv"
```

The matrix reports correct classifications on the diagonal, vessel confusion
off the diagonal, missed passages under `NO DETECTION`, and false alarms in the
`BACKGROUND` row. Windows containing more than one selected vessel are excluded.

## Full Run

This scans every WAV file and may take a while:

```powershell
& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py scan --template outputs\acoustic_profile_detection\french_warship_template.json --recordings-dir Data\Recordings --output-csv outputs\acoustic_profile_detection\french_warship_scan_full.csv --threshold 72 --keep-all --workers 0

& "C:\Users\kryst\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" acoustic_profile_detection\ship_profile_detector.py validate --template outputs\acoustic_profile_detection\french_warship_template.json --detections outputs\acoustic_profile_detection\french_warship_scan_full.csv --ais-csv outputs\processing\ais_near_hydrophone.csv --target-mmsi 228000000 --threshold 72 --output-html outputs\acoustic_profile_detection\french_warship_distance_validation_full.html
```

## Notes

- Use `outputs/processing/ais_near_hydrophone.csv` first if available. It is much smaller than the raw daily AIS files.
- If you want to use raw AIS directly, pass `Data\AIS\aisdk-2026-06-11\aisdk-2026-06-11.csv` to `--ais-csv`. That will stream the file but can still take time.
- Detection rows with `score >= threshold` are candidates. The `distance_proxy` column is only a relative loudness cue, not a calibrated range estimate.
- `--workers 0` uses most CPU cores. Use `--workers 1` for the old single-process behavior, or set a fixed value such as `--workers 6` if you want to leave the machine more responsive.
