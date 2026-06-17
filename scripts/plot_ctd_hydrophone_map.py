#!/usr/bin/env python3
"""Build an interactive CTD and hydrophone deployment map.

The script reads Sea-Bird ``.cnv`` headers, extracts CTD event metadata, checks
whether each CTD cast overlaps the available hydrophone recordings, and writes a
Leaflet HTML map plus an optional event CSV.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import mean


HYDROPHONE_LATITUDE = 55.7645
HYDROPHONE_LONGITUDE = 12.7465
HYDROPHONE_LABEL = "Hydrophone array, station 487"
DEFAULT_RECORDING_DURATION_SECONDS = 60.0

MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


@dataclass(frozen=True)
class AudioInterval:
    file_name: str
    start: dt.datetime
    end: dt.datetime


@dataclass(frozen=True)
class CtdEvent:
    file_name: str
    station: int | None
    latitude: float
    longitude: float
    start: dt.datetime
    end: dt.datetime
    duration_seconds: float | None
    depth_min_m: float | None
    depth_max_m: float | None
    salinity_min_psu: float | None
    salinity_max_psu: float | None
    temperature_min_c: float | None
    temperature_max_c: float | None
    distance_to_hydrophone_km: float
    recorded_by_hydrophone: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create an interactive map of CTD casts and the hydrophone array."
    )
    parser.add_argument(
        "--ctd-dir",
        type=Path,
        default=Path("Data/CTD"),
        help="Directory containing Sea-Bird .cnv files.",
    )
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=Path("Data/Recordings"),
        help="Directory containing timestamped WAV recordings.",
    )
    parser.add_argument(
        "--output-html",
        type=Path,
        default=Path("outputs/ctd_hydrophone_map.html"),
        help="HTML map to write.",
    )
    parser.add_argument(
        "--events-csv",
        type=Path,
        default=Path("outputs/ctd_events.csv"),
        help="CSV summary of parsed CTD events. Use an empty value to skip.",
    )
    parser.add_argument(
        "--hydrophone-latitude",
        type=float,
        default=HYDROPHONE_LATITUDE,
        help="Hydrophone latitude in decimal degrees.",
    )
    parser.add_argument(
        "--hydrophone-longitude",
        type=float,
        default=HYDROPHONE_LONGITUDE,
        help="Hydrophone longitude in decimal degrees.",
    )
    parser.add_argument(
        "--recording-duration-seconds",
        type=float,
        default=DEFAULT_RECORDING_DURATION_SECONDS,
        help="Expected duration of each timestamped WAV segment.",
    )
    return parser.parse_args()


def parse_seabird_coordinate(line: str) -> float | None:
    match = re.search(r"=\s*(\d+)\s+([0-9.]+)\s+([NSEW])", line)
    if not match:
        return None
    degrees = int(match.group(1))
    minutes = float(match.group(2))
    hemisphere = match.group(3)
    coordinate = degrees + minutes / 60.0
    if hemisphere in {"S", "W"}:
        coordinate *= -1
    return coordinate


def parse_seabird_datetime(line: str) -> dt.datetime | None:
    match = re.search(
        r"=\s*([A-Z][a-z]{2})\s+(\d+)\s+(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})",
        line,
    )
    if not match:
        return None
    month_name, day, year, hour, minute, second = match.groups()
    return dt.datetime(
        int(year),
        MONTHS[month_name],
        int(day),
        int(hour),
        int(minute),
        int(second),
        tzinfo=dt.timezone.utc,
    )


def parse_span(line: str) -> tuple[float | None, float | None]:
    values = re.findall(r"[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?", line, flags=re.IGNORECASE)
    if len(values) < 3:
        return None, None
    return float(values[-2]), float(values[-1])


def parse_station_from_name(file_name: str) -> int | None:
    match = re.search(r"(\d+)(?=\.cnv$)", file_name, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def parse_ctd_header(path: Path) -> dict[str, object]:
    metadata: dict[str, object] = {
        "file_name": path.name,
        "station": parse_station_from_name(path.name),
        "duration_seconds": None,
        "nvalues": None,
        "sample_interval_seconds": None,
        "depth_min_m": None,
        "depth_max_m": None,
        "salinity_min_psu": None,
        "salinity_max_psu": None,
        "temperature_min_c": None,
        "temperature_max_c": None,
    }

    with path.open("r", encoding="utf-8", errors="replace") as cnv_file:
        for line in cnv_file:
            stripped = line.strip()
            if stripped.startswith("* NMEA Latitude"):
                metadata["latitude"] = parse_seabird_coordinate(stripped)
            elif stripped.startswith("* NMEA Longitude"):
                metadata["longitude"] = parse_seabird_coordinate(stripped)
            elif stripped.startswith("* NMEA UTC (Time)"):
                metadata["start"] = parse_seabird_datetime(stripped)
            elif stripped.startswith("# nvalues"):
                match = re.search(r"=\s*(\d+)", stripped)
                if match:
                    metadata["nvalues"] = int(match.group(1))
            elif stripped.startswith("# interval = seconds:"):
                match = re.search(r"seconds:\s*([0-9.]+)", stripped)
                if match:
                    metadata["sample_interval_seconds"] = float(match.group(1))
            elif stripped.startswith("# span 0"):
                metadata["depth_min_m"], metadata["depth_max_m"] = parse_span(stripped)
            elif stripped.startswith("# span 1"):
                metadata["salinity_min_psu"], metadata["salinity_max_psu"] = parse_span(stripped)
            elif stripped.startswith("# span 2"):
                metadata["temperature_min_c"], metadata["temperature_max_c"] = parse_span(stripped)
            elif stripped == "*END*":
                break

    missing = [
        key
        for key in ("latitude", "longitude", "start")
        if metadata.get(key) is None
    ]
    if missing:
        missing_text = ", ".join(missing)
        raise ValueError(f"{path} is missing required CTD metadata: {missing_text}")

    nvalues = metadata.get("nvalues")
    sample_interval = metadata.get("sample_interval_seconds")
    if isinstance(nvalues, int) and isinstance(sample_interval, float):
        metadata["duration_seconds"] = nvalues * sample_interval
    else:
        metadata["duration_seconds"] = 0.0

    start = metadata["start"]
    duration = metadata["duration_seconds"]
    if not isinstance(start, dt.datetime) or not isinstance(duration, float):
        raise ValueError(f"{path} has invalid CTD timing metadata.")
    metadata["end"] = start + dt.timedelta(seconds=duration)
    return metadata


def parse_audio_start(path: Path) -> dt.datetime | None:
    match = re.search(r"_(\d{10})_(\d+)$", path.stem)
    if not match:
        return None
    seconds = int(match.group(1))
    fractional = float(f"0.{match.group(2)}")
    return dt.datetime.fromtimestamp(seconds + fractional, tz=dt.timezone.utc)


def load_audio_intervals(recordings_dir: Path, duration_seconds: float) -> list[AudioInterval]:
    intervals: list[AudioInterval] = []
    for path in sorted(recordings_dir.glob("*.wav")):
        start = parse_audio_start(path)
        if start is None:
            continue
        intervals.append(
            AudioInterval(
                file_name=path.name,
                start=start,
                end=start + dt.timedelta(seconds=duration_seconds),
            )
        )
    return intervals


def overlaps_audio(start: dt.datetime, end: dt.datetime, intervals: list[AudioInterval]) -> bool:
    for interval in intervals:
        if interval.end < start:
            continue
        if interval.start > end:
            return False
        return True
    return False


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    return radius_km * 2 * math.asin(math.sqrt(a))


def build_ctd_events(
    ctd_dir: Path,
    audio_intervals: list[AudioInterval],
    hydrophone_latitude: float,
    hydrophone_longitude: float,
) -> list[CtdEvent]:
    events: list[CtdEvent] = []
    for path in sorted(ctd_dir.glob("*.cnv")):
        metadata = parse_ctd_header(path)
        latitude = float(metadata["latitude"])
        longitude = float(metadata["longitude"])
        start = metadata["start"]
        end = metadata["end"]
        if not isinstance(start, dt.datetime) or not isinstance(end, dt.datetime):
            raise ValueError(f"{path} has invalid start/end metadata.")

        events.append(
            CtdEvent(
                file_name=str(metadata["file_name"]),
                station=metadata["station"] if isinstance(metadata["station"], int) else None,
                latitude=latitude,
                longitude=longitude,
                start=start,
                end=end,
                duration_seconds=(
                    float(metadata["duration_seconds"])
                    if metadata["duration_seconds"] is not None
                    else None
                ),
                depth_min_m=metadata["depth_min_m"],
                depth_max_m=metadata["depth_max_m"],
                salinity_min_psu=metadata["salinity_min_psu"],
                salinity_max_psu=metadata["salinity_max_psu"],
                temperature_min_c=metadata["temperature_min_c"],
                temperature_max_c=metadata["temperature_max_c"],
                distance_to_hydrophone_km=haversine_km(
                    hydrophone_latitude,
                    hydrophone_longitude,
                    latitude,
                    longitude,
                ),
                recorded_by_hydrophone=overlaps_audio(start, end, audio_intervals),
            )
        )
    if not events:
        raise ValueError(f"No .cnv files found in {ctd_dir}")
    return events


def format_optional(value: float | int | str | None, digits: int = 2) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def popup_html(event: CtdEvent) -> str:
    station = event.station if event.station is not None else event.file_name
    rows = [
        ("File", event.file_name),
        ("Start UTC", event.start.isoformat()),
        ("End UTC", event.end.isoformat()),
        ("Distance to hydrophone", f"{event.distance_to_hydrophone_km:.2f} km"),
        ("Hydrophone recording", "yes" if event.recorded_by_hydrophone else "no"),
        ("Depth range", range_text(event.depth_min_m, event.depth_max_m, "m")),
        ("Temperature range", range_text(event.temperature_min_c, event.temperature_max_c, "deg C")),
        ("Salinity range", range_text(event.salinity_min_psu, event.salinity_max_psu, "PSU")),
    ]
    body = "".join(
        f"<br><b>{html.escape(label)}:</b> {html.escape(value)}"
        for label, value in rows
        if value
    )
    return f"<strong>CTD station {html.escape(str(station))}</strong>{body}"


def range_text(low: float | None, high: float | None, unit: str) -> str:
    if low is None or high is None:
        return ""
    return f"{low:.2f}-{high:.2f} {unit}"


def event_to_geojson_feature(event: CtdEvent) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [event.longitude, event.latitude],
        },
        "properties": {
            "station": event.station,
            "file": event.file_name,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "recorded": event.recorded_by_hydrophone,
            "distance_km": round(event.distance_to_hydrophone_km, 3),
            "popup": popup_html(event),
        },
    }


def render_html(
    events: list[CtdEvent],
    audio_intervals: list[AudioInterval],
    hydrophone_latitude: float,
    hydrophone_longitude: float,
) -> str:
    coordinates = [(event.latitude, event.longitude) for event in events]
    coordinates.append((hydrophone_latitude, hydrophone_longitude))
    center = [mean(lat for lat, _lon in coordinates), mean(lon for _lat, lon in coordinates)]
    recorded_count = sum(event.recorded_by_hydrophone for event in events)
    audio_start = min((interval.start for interval in audio_intervals), default=None)
    audio_end = max((interval.end for interval in audio_intervals), default=None)
    ctd_geojson = {
        "type": "FeatureCollection",
        "features": [event_to_geojson_feature(event) for event in events],
    }

    audio_window = "No recordings found"
    if audio_start and audio_end:
        audio_window = f"{audio_start.isoformat()} to {audio_end.isoformat()}"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CTD Stations and Hydrophone Array</title>
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIINfQmC7gR2atEZOyVBxzDvlw3f5hdnDII="
    crossorigin=""
  >
  <style>
    html, body, #map {{
      height: 100%;
      margin: 0;
    }}
    .map-panel {{
      background: white;
      border-radius: 6px;
      box-shadow: 0 2px 10px rgba(0, 0, 0, 0.16);
      color: #17202a;
      font: 13px/1.35 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      max-width: 320px;
      padding: 10px 12px;
    }}
    .map-panel strong {{
      display: block;
      margin-bottom: 4px;
    }}
    .legend-row {{
      align-items: center;
      display: flex;
      gap: 7px;
      margin-top: 5px;
    }}
    .swatch {{
      border-radius: 999px;
      display: inline-block;
      height: 11px;
      width: 11px;
    }}
  </style>
</head>
<body>
  <div id="map"></div>
  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    crossorigin=""
  ></script>
  <script>
    const ctdStations = {json.dumps(ctd_geojson, indent=6)};
    const hydrophone = {{
      lat: {hydrophone_latitude},
      lon: {hydrophone_longitude},
      label: {json.dumps(HYDROPHONE_LABEL)}
    }};

    const map = L.map("map").setView({json.dumps(center)}, 11);
    L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);

    const ctdLayer = L.geoJSON(ctdStations, {{
      pointToLayer: (feature, latlng) => {{
        const recorded = feature.properties.recorded;
        return L.circleMarker(latlng, {{
          radius: recorded ? 8 : 6,
          color: recorded ? "#075985" : "#6b7280",
          fillColor: recorded ? "#0ea5a8" : "#d1d5db",
          fillOpacity: recorded ? 0.9 : 0.72,
          opacity: 0.95,
          weight: 2
        }});
      }},
      onEachFeature: (feature, layer) => {{
        layer.bindPopup(feature.properties.popup);
        layer.bindTooltip(`CTD ${{feature.properties.station ?? feature.properties.file}}`, {{
          direction: "top",
          offset: [0, -8]
        }});
      }}
    }}).addTo(map);

    const hydrophoneMarker = L.circleMarker([hydrophone.lat, hydrophone.lon], {{
      radius: 10,
      color: "#991b1b",
      fillColor: "#ef4444",
      fillOpacity: 0.95,
      opacity: 1,
      weight: 3
    }})
      .bindPopup(`<strong>${{hydrophone.label}}</strong><br><b>Latitude:</b> ${{hydrophone.lat}}<br><b>Longitude:</b> ${{hydrophone.lon}}`)
      .bindTooltip("Hydrophone array", {{ direction: "top", offset: [0, -10] }})
      .addTo(map);

    const allBounds = L.featureGroup([ctdLayer, hydrophoneMarker]).getBounds();
    map.fitBounds(allBounds.pad(0.2));

    const summary = L.control({{ position: "topright" }});
    summary.onAdd = () => {{
      const div = L.DomUtil.create("div", "map-panel");
      div.innerHTML = `
        <strong>CTD and hydrophone overview</strong>
        CTD casts: {len(events)}<br>
        Overlap with recordings: {recorded_count}<br>
        Audio window: {html.escape(audio_window)}
      `;
      return div;
    }};
    summary.addTo(map);

    const legend = L.control({{ position: "bottomleft" }});
    legend.onAdd = () => {{
      const div = L.DomUtil.create("div", "map-panel");
      div.innerHTML = `
        <strong>Legend</strong>
        <div class="legend-row"><span class="swatch" style="background:#0ea5a8"></span>CTD overlaps audio</div>
        <div class="legend-row"><span class="swatch" style="background:#d1d5db"></span>CTD outside audio</div>
        <div class="legend-row"><span class="swatch" style="background:#ef4444"></span>Hydrophone array</div>
      `;
      return div;
    }};
    legend.addTo(map);
  </script>
</body>
</html>
"""


def write_events_csv(events: list[CtdEvent], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "file_name",
                "station",
                "latitude",
                "longitude",
                "start_utc",
                "end_utc",
                "duration_seconds",
                "distance_to_hydrophone_km",
                "recorded_by_hydrophone",
                "depth_min_m",
                "depth_max_m",
                "temperature_min_c",
                "temperature_max_c",
                "salinity_min_psu",
                "salinity_max_psu",
            ]
        )
        for event in events:
            writer.writerow(
                [
                    event.file_name,
                    event.station,
                    f"{event.latitude:.6f}",
                    f"{event.longitude:.6f}",
                    event.start.isoformat(),
                    event.end.isoformat(),
                    format_optional(event.duration_seconds, digits=2),
                    f"{event.distance_to_hydrophone_km:.3f}",
                    str(event.recorded_by_hydrophone).lower(),
                    format_optional(event.depth_min_m, digits=3),
                    format_optional(event.depth_max_m, digits=3),
                    format_optional(event.temperature_min_c, digits=4),
                    format_optional(event.temperature_max_c, digits=4),
                    format_optional(event.salinity_min_psu, digits=4),
                    format_optional(event.salinity_max_psu, digits=4),
                ]
            )


def main() -> None:
    args = parse_args()
    audio_intervals = load_audio_intervals(
        args.recordings_dir,
        duration_seconds=args.recording_duration_seconds,
    )
    events = build_ctd_events(
        ctd_dir=args.ctd_dir,
        audio_intervals=audio_intervals,
        hydrophone_latitude=args.hydrophone_latitude,
        hydrophone_longitude=args.hydrophone_longitude,
    )
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.write_text(
        render_html(
            events=events,
            audio_intervals=audio_intervals,
            hydrophone_latitude=args.hydrophone_latitude,
            hydrophone_longitude=args.hydrophone_longitude,
        ),
        encoding="utf-8",
    )
    if args.events_csv:
        write_events_csv(events, args.events_csv)

    recorded_count = sum(event.recorded_by_hydrophone for event in events)
    print(f"Wrote {args.output_html}")
    if args.events_csv:
        print(f"Wrote {args.events_csv}")
    print(f"CTD events: {len(events)}; overlapping audio recordings: {recorded_count}")


if __name__ == "__main__":
    main()
