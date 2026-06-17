#!/usr/bin/env python3
"""Build a compact JSON data set for the local hydrophone web app."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path


HYDROPHONE = {
    "id": "hydrophone-array-487",
    "label": "Hydrophone array 487",
    "latitude": 55.7645,
    "longitude": 12.7465,
}
UTC = dt.timezone.utc

AIS_CANDIDATES = [
    Path("outputs/processing/ais_near_hydrophone.csv"),
    Path("outputs/processing/ais_prepare_duckdb_raw_smoke.csv"),
    Path("outputs/processing/single_recording_smoke/nearby_ais_rows.csv"),
    Path("outputs/processing/ais_prepare_duckdb_smoke.csv"),
    Path("outputs/processing/ais_prepare_python_smoke.csv"),
    Path("Data/analysis_example/oresund_ais.csv"),
]
AUDIO_EVENT_CANDIDATES = [
    Path("outputs/processing/audio_ais_events.csv"),
    Path("outputs/processing/audio_ais_events_smoke.csv"),
]
AUDIO_FEATURE_CANDIDATES = [
    Path("outputs/processing/audio_features.csv"),
    Path("outputs/processing/audio_features_5_smoke.csv"),
    Path("outputs/processing/audio_features_smoke.csv"),
]
CTD_CANDIDATES = [
    Path("outputs/ctd_events.csv"),
]


ALIASES = {
    "timestamp": ("timestamp", "# timestamp", "datetime", "base datetime"),
    "ship_id": ("ship_id", "mmsi"),
    "lat": ("lat", "latitude"),
    "lon": ("lon", "longitude", "long"),
    "distance_km": ("distance_km", "distance km"),
    "bearing_deg": ("bearing_deg", "bearing deg"),
    "name": ("name", "shipname", "vesselname"),
    "sog": ("sog",),
    "cog": ("cog",),
    "heading": ("heading",),
    "width": ("width",),
    "length": ("length",),
    "draught": ("draught", "draft"),
    "ship_type": ("ship_type", "ship type"),
    "type_of_mobile": ("type_of_mobile", "type of mobile"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ais-csv", type=Path, default=None)
    parser.add_argument("--audio-events-csv", type=Path, default=None)
    parser.add_argument("--audio-features-csv", type=Path, default=None)
    parser.add_argument("--ctd-events-csv", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=Path("web/data/app_data.json"))
    parser.add_argument("--max-vessels", type=int, default=160)
    parser.add_argument("--max-track-points-per-vessel", type=int, default=80)
    parser.add_argument("--max-ais-rows", type=int, default=250_000)
    return parser.parse_args()


def first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def normalize_column(value: object) -> str:
    return str(value).strip().lstrip("#").strip().lower().replace("_", " ")


def column_map(fieldnames: list[str] | None) -> dict[str, str]:
    if not fieldnames:
        return {}
    normalized = {normalize_column(name): name for name in fieldnames}
    mapping: dict[str, str] = {}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            key = normalize_column(alias)
            if key in normalized:
                mapping[canonical] = normalized[key]
                break
    return mapping


def get_field(row: dict[str, str], mapping: dict[str, str], canonical: str) -> str:
    source = mapping.get(canonical)
    return row.get(source, "").strip() if source else ""


def parse_float(value: object) -> float | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_datetime(value: object) -> dt.datetime | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def iso(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


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
    return radius_km * 2.0 * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    lon_delta = math.radians(lon2 - lon1)
    x = math.sin(lon_delta) * math.cos(lat2_rad)
    y = (
        math.cos(lat1_rad) * math.sin(lat2_rad)
        - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(lon_delta)
    )
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def sample_evenly(items: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
    if len(items) <= limit:
        return items
    if limit <= 1:
        return [items[0]]
    step = (len(items) - 1) / (limit - 1)
    return [items[round(i * step)] for i in range(limit)]


def read_audio_profiles(path: Path | None) -> list[dict[str, object]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as csv_file:
        for row in csv.DictReader(csv_file):
            rows.append(
                {
                    "fileName": row.get("file_name", ""),
                    "startUtc": row.get("start_utc", ""),
                    "endUtc": row.get("end_utc", ""),
                    "rmsDbfsMean": parse_float(row.get("rms_dbfs_mean")),
                    "peakDbfs": parse_float(row.get("peak_dbfs")),
                    "crestFactorDb": parse_float(row.get("crest_factor_db")),
                    "stereoCorrelation": parse_float(row.get("stereo_correlation")),
                    "bands": {
                        "20-100 Hz": parse_float(row.get("band_20_100_db")),
                        "100-500 Hz": parse_float(row.get("band_100_500_db")),
                        "500-2000 Hz": parse_float(row.get("band_500_2000_db")),
                        "2000-10000 Hz": parse_float(row.get("band_2000_10000_db")),
                    },
                    "nearbyVesselCount": parse_float(row.get("nearby_vessel_count")),
                    "closestShipId": row.get("closest_ship_id", ""),
                    "closestName": row.get("closest_name", ""),
                    "closestDistanceKm": parse_float(row.get("closest_distance_km")),
                }
            )
    return rows


def audio_for_vessel(vessel: dict[str, object], profiles: list[dict[str, object]]) -> dict[str, object] | None:
    if not profiles:
        return None
    ship_id = str(vessel.get("id", ""))
    exact = [profile for profile in profiles if profile.get("closestShipId") == ship_id]
    if exact:
        return exact[0]

    closest_time = parse_datetime(vessel.get("closestTimestamp"))
    if closest_time is None:
        return profiles[0]

    best_profile = profiles[0]
    best_delta = float("inf")
    for profile in profiles:
        start = parse_datetime(profile.get("startUtc"))
        end = parse_datetime(profile.get("endUtc"))
        if start is None or end is None:
            continue
        if start <= closest_time <= end:
            return profile
        midpoint = start + (end - start) / 2
        delta = abs((midpoint - closest_time).total_seconds())
        if delta < best_delta:
            best_delta = delta
            best_profile = profile
    return best_profile


def read_vessels(
    path: Path | None,
    profiles: list[dict[str, object]],
    max_vessels: int,
    max_track_points_per_vessel: int,
    max_ais_rows: int,
) -> list[dict[str, object]]:
    if path is None or not path.exists():
        return []

    grouped: dict[str, list[dict[str, object]]] = {}
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        mapping = column_map(reader.fieldnames)
        for index, row in enumerate(reader):
            if index >= max_ais_rows:
                break
            lat = parse_float(get_field(row, mapping, "lat"))
            lon = parse_float(get_field(row, mapping, "lon"))
            ship_id = get_field(row, mapping, "ship_id")
            if lat is None or lon is None or not ship_id:
                continue
            timestamp = parse_datetime(get_field(row, mapping, "timestamp"))
            distance = parse_float(get_field(row, mapping, "distance_km"))
            if distance is None:
                distance = haversine_km(HYDROPHONE["latitude"], HYDROPHONE["longitude"], lat, lon)
            bearing = parse_float(get_field(row, mapping, "bearing_deg"))
            if bearing is None:
                bearing = bearing_deg(HYDROPHONE["latitude"], HYDROPHONE["longitude"], lat, lon)
            point = {
                "timestamp": iso(timestamp),
                "latitude": lat,
                "longitude": lon,
                "distanceKm": distance,
                "bearingDeg": bearing,
                "sog": parse_float(get_field(row, mapping, "sog")),
                "cog": parse_float(get_field(row, mapping, "cog")),
                "heading": parse_float(get_field(row, mapping, "heading")),
                "name": get_field(row, mapping, "name"),
                "shipType": get_field(row, mapping, "ship_type"),
                "typeOfMobile": get_field(row, mapping, "type_of_mobile"),
                "length": parse_float(get_field(row, mapping, "length")),
                "width": parse_float(get_field(row, mapping, "width")),
                "draught": parse_float(get_field(row, mapping, "draught")),
            }
            grouped.setdefault(ship_id, []).append(point)

    vessels: list[dict[str, object]] = []
    for ship_id, points in grouped.items():
        points.sort(key=lambda point: str(point.get("timestamp", "")))
        closest = min(points, key=lambda point: point.get("distanceKm") or float("inf"))
        sog_values = [point["sog"] for point in points if point.get("sog") is not None]
        length_values = [point["length"] for point in points if point.get("length") is not None]
        draught_values = [point["draught"] for point in points if point.get("draught") is not None]
        name = str(closest.get("name") or ship_id)
        vessel = {
            "id": ship_id,
            "name": name,
            "shipType": str(closest.get("shipType") or "Unknown"),
            "typeOfMobile": str(closest.get("typeOfMobile") or ""),
            "track": sample_evenly(points, max_track_points_per_vessel),
            "rowCount": len(points),
            "firstTimestamp": points[0].get("timestamp", ""),
            "lastTimestamp": points[-1].get("timestamp", ""),
            "closestTimestamp": closest.get("timestamp", ""),
            "closestLatitude": closest.get("latitude"),
            "closestLongitude": closest.get("longitude"),
            "closestDistanceKm": closest.get("distanceKm"),
            "closestBearingDeg": closest.get("bearingDeg"),
            "meanSog": sum(sog_values) / len(sog_values) if sog_values else None,
            "maxSog": max(sog_values) if sog_values else None,
            "maxLength": max(length_values) if length_values else None,
            "maxDraught": max(draught_values) if draught_values else None,
        }
        vessel["audio"] = audio_for_vessel(vessel, profiles)
        vessels.append(vessel)

    vessels.sort(
        key=lambda vessel: (
            float(vessel.get("closestDistanceKm") or float("inf")),
            -float(vessel.get("maxLength") or 0.0),
        )
    )
    return vessels[:max_vessels]


def read_ctd_events(path: Path | None) -> list[dict[str, object]]:
    if path is None or not path.exists():
        return []
    events: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as csv_file:
        for row in csv.DictReader(csv_file):
            lat = parse_float(row.get("latitude"))
            lon = parse_float(row.get("longitude"))
            if lat is None or lon is None:
                continue
            events.append(
                {
                    "id": row.get("station") or row.get("file_name", ""),
                    "fileName": row.get("file_name", ""),
                    "station": row.get("station", ""),
                    "latitude": lat,
                    "longitude": lon,
                    "startUtc": row.get("start_utc", ""),
                    "endUtc": row.get("end_utc", ""),
                    "distanceToHydrophoneKm": parse_float(row.get("distance_to_hydrophone_km")),
                    "recordedByHydrophone": str(row.get("recorded_by_hydrophone", "")).lower() == "true",
                    "depthMinM": parse_float(row.get("depth_min_m")),
                    "depthMaxM": parse_float(row.get("depth_max_m")),
                    "temperatureMinC": parse_float(row.get("temperature_min_c")),
                    "temperatureMaxC": parse_float(row.get("temperature_max_c")),
                    "salinityMinPsu": parse_float(row.get("salinity_min_psu")),
                    "salinityMaxPsu": parse_float(row.get("salinity_max_psu")),
                }
            )
    events.sort(key=lambda event: str(event.get("startUtc", "")))
    return events


def bounds_for(vessels: list[dict[str, object]], ctd_events: list[dict[str, object]]) -> dict[str, float]:
    latitudes = [HYDROPHONE["latitude"]]
    longitudes = [HYDROPHONE["longitude"]]
    for vessel in vessels:
        for point in vessel.get("track", []):
            latitudes.append(float(point["latitude"]))
            longitudes.append(float(point["longitude"]))
    for event in ctd_events:
        latitudes.append(float(event["latitude"]))
        longitudes.append(float(event["longitude"]))
    pad_lat = max((max(latitudes) - min(latitudes)) * 0.08, 0.005)
    pad_lon = max((max(longitudes) - min(longitudes)) * 0.08, 0.005)
    return {
        "minLat": min(latitudes) - pad_lat,
        "maxLat": max(latitudes) + pad_lat,
        "minLon": min(longitudes) - pad_lon,
        "maxLon": max(longitudes) + pad_lon,
    }


def main() -> None:
    args = parse_args()
    ais_csv = args.ais_csv or first_existing(AIS_CANDIDATES)
    audio_events_csv = args.audio_events_csv or first_existing(AUDIO_EVENT_CANDIDATES)
    audio_features_csv = args.audio_features_csv or first_existing(AUDIO_FEATURE_CANDIDATES)
    ctd_events_csv = args.ctd_events_csv or first_existing(CTD_CANDIDATES)

    profiles = read_audio_profiles(audio_events_csv) or read_audio_profiles(audio_features_csv)
    vessels = read_vessels(
        ais_csv,
        profiles,
        max_vessels=args.max_vessels,
        max_track_points_per_vessel=args.max_track_points_per_vessel,
        max_ais_rows=args.max_ais_rows,
    )
    ctd_events = read_ctd_events(ctd_events_csv)
    data = {
        "metadata": {
            "generatedAtUtc": iso(dt.datetime.now(tz=UTC)),
            "sources": {
                "aisCsv": str(ais_csv) if ais_csv else "",
                "audioEventsCsv": str(audio_events_csv) if audio_events_csv else "",
                "audioFeaturesCsv": str(audio_features_csv) if audio_features_csv else "",
                "ctdEventsCsv": str(ctd_events_csv) if ctd_events_csv else "",
            },
            "vesselCount": len(vessels),
            "ctdCount": len(ctd_events),
            "audioProfileCount": len(profiles),
        },
        "hydrophone": HYDROPHONE,
        "bounds": bounds_for(vessels, ctd_events),
        "vessels": vessels,
        "ctdEvents": ctd_events,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {args.output}")
    print(f"Vessels: {len(vessels)}; CTD events: {len(ctd_events)}; audio profiles: {len(profiles)}")


if __name__ == "__main__":
    main()
