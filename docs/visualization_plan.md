# Hydrophone Visualization Plan

## Current data layers

- CTD profiles: `Data/CTD/*.cnv`
  - Contains GPS position, UTC cast time, depth range, salinity, temperature, and oxygen profile data.
- Hydrophone recordings: `Data/Recordings/*.wav`
  - Stereo 48 kHz, 24-bit WAV segments.
  - File names contain UTC Unix timestamps and are suitable for building an audio coverage timeline.
- AIS vessel data: `Data/AIS/aisdk-2026-06-10.zip` and `Data/AIS/aisdk-2026-06-11.zip`
  - Danish AIS day dumps with vessel position, MMSI, speed over ground, course, dimensions, draught, destination, and ship type.
- Filtered AIS example: `Data/analysis_example/oresund_ais.csv`
  - A smaller region/time subset that is ready for prototype maps and ship-track plots.
- Existing analysis example: `Data/analysis_example`
  - Includes spectrogram, beamforming, and AIS overlay helpers.

## Recommended visualization path

1. CTD and hydrophone map
   - Plot the hydrophone array and all CTD stations.
   - Color each CTD cast by whether the hydrophone was recording at that time.
   - Show CTD timing, distance to the hydrophone, depth range, temperature, and salinity in popups.

2. Time-linked event view
   - Add a horizontal timeline with CTD casts, audio recording coverage, and AIS vessel passages.
   - Clicking a CTD event should zoom the map and select the matching audio window.
   - Clicking a ship passage should show its track and extract the corresponding spectrogram slice.

3. AIS vessel map
   - Pre-filter raw AIS by radius around the hydrophone, then resample tracks to a practical time step.
   - Plot vessel tracks with color by speed over ground and line width or marker size by ship length.
   - Use popups for MMSI, name, ship type, length, draught, speed, course, and closest point of approach.

4. Acoustic validation layer
   - For each vessel close approach, compute a short spectrogram and broadband/low-frequency energy summary.
   - Compare expected bearing from AIS against beamforming/Capon direction estimates.
   - Mark cases as "likely detected", "not detected", or "uncertain" based on audio energy and bearing agreement.

5. 3D exploratory view
   - A 3D map is possible, but it should be a second-stage exploratory tool rather than the first analysis product.
   - Use vessel length, width, draught, and speed from AIS to render scaled ship blocks or icons.
   - Use vertical exaggeration for CTD depth profiles and, if bathymetry is added later, place profiles and vessels in a depth-aware scene.

## Practical notes

- The raw AIS zip files are several gigabytes when decompressed, so prototype against `Data/analysis_example/oresund_ais.csv` first.
- The generated Leaflet maps use OpenStreetMap web tiles; the HTML file opens locally, but the basemap requires internet access in the browser.
- Keep preprocessing scripts streaming/chunked for AIS and header-only for WAV files to avoid loading the large data set into memory.
