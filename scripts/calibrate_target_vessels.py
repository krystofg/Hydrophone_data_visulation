#!/usr/bin/env python3
"""Build repeated target-vessel audio windows and calibrate array heading."""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from build_event_audio_profiles import (
    OUTPUT_COLUMNS,
    RecordingCache,
    profile_row,
    recording_index,
)
from hydrophone_pipeline import (
    DEFAULT_BANDS,
    UTC,
    ais_column_map,
    band_column,
    bearing_deg,
    get_field,
    haversine_km,
    parse_datetime,
    parse_float,
)


DEFAULT_TARGETS = ["HAVFISKEN", "FRENCH WARSHIP", "VICTORY", "SALSA"]
DEFAULT_HYDROPHONE_LATITUDE = 55.7645
DEFAULT_HYDROPHONE_LONGITUDE = 12.7465

EXTRA_PROFILE_COLUMNS = [
    "profile_id",
    "target_query",
    "source_lat",
    "source_lon",
    "source_sog_kn",
    "source_cog_deg",
    "source_ship_type",
    "nearby_vessel_count",
    "nearby_vessel_names",
    "loudest_delta_seconds",
    "calibration_candidate",
    "calibration_weight",
]


@dataclass(frozen=True)
class AisPoint:
    vessel_key: str
    vessel_id: str
    name: str
    timestamp: dt.datetime
    latitude: float
    longitude: float
    distance_km: float
    bearing_deg: float
    sog: float | None
    cog: float | None
    ship_type: str


@dataclass(frozen=True)
class TargetEvent:
    profile_id: str
    target_query: str
    point: AisPoint
    event_time: dt.datetime
    propagation_delay_seconds: float
    window_start: dt.datetime
    window_end: dt.datetime
    nearby_names: tuple[str, ...]


@dataclass(frozen=True)
class CalibrationCandidate:
    profile_id: str
    target_query: str
    vessel_key: str
    vessel_id: str
    vessel_name: str
    source_time_utc: str
    source_distance_km: float
    source_bearing_deg: float
    angle_candidates_deg: tuple[float, ...]
    beam_confidence: float
    peak_dbfs: float
    rms_dbfs: float | None
    loudest_delta_seconds: float | None
    nearby_vessel_count: int
    weight: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-data", type=Path, default=Path("web/data/app_data.json"))
    parser.add_argument("--ais-csv", type=Path, default=None)
    parser.add_argument("--recordings-dir", type=Path, default=Path("Data/Recordings"))
    parser.add_argument(
        "--target-vessel",
        action="append",
        default=[],
        help="MMSI or name substring to use as a trusted target. Can be repeated.",
    )
    parser.add_argument("--output-profiles", type=Path, default=Path("outputs/processing/target_vessel_audio_profiles.csv"))
    parser.add_argument("--output-candidates", type=Path, default=Path("outputs/processing/target_vessel_calibration_candidates.csv"))
    parser.add_argument("--output-json", type=Path, default=Path("outputs/processing/target_vessel_calibration.json"))
    parser.add_argument("--window-seconds", type=float, default=20.0)
    parser.add_argument("--sample-step-seconds", type=float, default=20.0)
    parser.add_argument("--max-distance-km", type=float, default=5.0)
    parser.add_argument("--per-vessel-limit", type=int, default=0)
    parser.add_argument("--sound-speed-m-s", type=float, default=1500.0)
    parser.add_argument("--block-seconds", type=float, default=5.0)
    parser.add_argument("--waveform-bins", type=int, default=80)
    parser.add_argument("--beam-window-seconds", type=float, default=8.0)
    parser.add_argument("--beam-fmin-hz", type=float, default=50.0)
    parser.add_argument("--beam-fmax-hz", type=float, default=900.0)
    parser.add_argument("--mic-spacing-m", type=float, default=0.75)
    parser.add_argument("--cache-files", type=int, default=8)
    parser.add_argument("--neighbor-time-seconds", type=float, default=45.0)
    parser.add_argument("--neighbor-radius-km", type=float, default=3.0)
    parser.add_argument("--calibration-max-distance-km", type=float, default=3.0)
    parser.add_argument("--max-nearby-vessels", type=int, default=0)
    parser.add_argument("--min-confidence", type=float, default=0.12)
    parser.add_argument("--min-peak-dbfs", type=float, default=-45.0)
    parser.add_argument("--max-loudest-delta-seconds", type=float, default=8.0)
    parser.add_argument("--grid-step-deg", type=float, default=0.5)
    parser.add_argument("--top-count", type=int, default=12)
    parser.add_argument("--max-ais-rows", type=int, default=0)
    return parser.parse_args()


def normalize(value: object) -> str:
    return str(value or "").strip().lower()


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def finite_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_json_float_list(value: object) -> list[float]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    values: list[float] = []
    for item in parsed:
        number = finite_float(item)
        if number is not None:
            values.append(number)
    return values


def angular_difference_deg(first: float, second: float) -> float:
    return abs((first - second + 180.0) % 360.0 - 180.0)


def first_existing_ais_csv(requested: Path | None) -> Path | None:
    if requested is not None:
        return requested if requested.exists() else None
    for candidate in (
        Path("outputs/processing/ais_near_hydrophone.csv"),
        Path("Data/analysis_example/oresund_ais.csv"),
    ):
        if candidate.exists():
            return candidate
    return None


def load_hydrophone(app_data_path: Path) -> tuple[float, float]:
    if not app_data_path.exists():
        return DEFAULT_HYDROPHONE_LATITUDE, DEFAULT_HYDROPHONE_LONGITUDE
    try:
        data = json.loads(app_data_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DEFAULT_HYDROPHONE_LATITUDE, DEFAULT_HYDROPHONE_LONGITUDE
    hydrophone = data.get("hydrophone") or {}
    latitude = finite_float(hydrophone.get("latitude")) or DEFAULT_HYDROPHONE_LATITUDE
    longitude = finite_float(hydrophone.get("longitude")) or DEFAULT_HYDROPHONE_LONGITUDE
    return latitude, longitude


def read_ais_csv(
    path: Path,
    hydro_lat: float,
    hydro_lon: float,
    max_rows: int,
) -> list[AisPoint]:
    points: list[AisPoint] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        mapping = ais_column_map(reader.fieldnames or [])
        for index, row in enumerate(reader, start=1):
            if max_rows > 0 and index > max_rows:
                break
            try:
                timestamp = parse_datetime(get_field(row, mapping, "timestamp"))
            except ValueError:
                continue
            vessel_id = get_field(row, mapping, "ship_id")
            name = get_field(row, mapping, "name")
            lat = parse_float(get_field(row, mapping, "lat"))
            lon = parse_float(get_field(row, mapping, "lon"))
            if lat is None or lon is None:
                continue
            distance = parse_float(get_field(row, mapping, "distance_km"))
            bearing = parse_float(get_field(row, mapping, "bearing_deg"))
            if distance is None:
                distance = haversine_km(hydro_lat, hydro_lon, lat, lon)
            if bearing is None:
                bearing = bearing_deg(hydro_lat, hydro_lon, lat, lon)
            vessel_key = vessel_id or name
            if not vessel_key:
                continue
            points.append(
                AisPoint(
                    vessel_key=str(vessel_key),
                    vessel_id=str(vessel_id),
                    name=str(name),
                    timestamp=timestamp,
                    latitude=lat,
                    longitude=lon,
                    distance_km=distance,
                    bearing_deg=bearing,
                    sog=parse_float(get_field(row, mapping, "sog")),
                    cog=parse_float(get_field(row, mapping, "cog")),
                    ship_type=get_field(row, mapping, "ship_type"),
                )
            )
    return points


def read_app_data_points(app_data_path: Path, hydro_lat: float, hydro_lon: float) -> list[AisPoint]:
    if not app_data_path.exists():
        return []
    data = json.loads(app_data_path.read_text(encoding="utf-8"))
    points: list[AisPoint] = []
    for vessel in data.get("vessels") or []:
        vessel_id = str(vessel.get("id") or "")
        name = str(vessel.get("name") or "")
        ship_type = str(vessel.get("shipType") or "")
        for track_point in vessel.get("track") or []:
            try:
                timestamp = parse_datetime(track_point.get("timestamp", ""))
            except ValueError:
                continue
            lat = finite_float(track_point.get("latitude")) or finite_float(track_point.get("lat"))
            lon = finite_float(track_point.get("longitude")) or finite_float(track_point.get("lon"))
            if lat is None or lon is None:
                continue
            distance = finite_float(track_point.get("distanceKm"))
            bearing = finite_float(track_point.get("bearingDeg"))
            if distance is None:
                distance = haversine_km(hydro_lat, hydro_lon, lat, lon)
            if bearing is None:
                bearing = bearing_deg(hydro_lat, hydro_lon, lat, lon)
            points.append(
                AisPoint(
                    vessel_key=vessel_id or name,
                    vessel_id=vessel_id,
                    name=str(track_point.get("name") or name),
                    timestamp=timestamp,
                    latitude=lat,
                    longitude=lon,
                    distance_km=distance,
                    bearing_deg=bearing,
                    sog=finite_float(track_point.get("sog")),
                    cog=finite_float(track_point.get("cog")),
                    ship_type=str(track_point.get("shipType") or ship_type),
                )
            )
    return points


def load_ais_points(args: argparse.Namespace, hydro_lat: float, hydro_lon: float) -> tuple[list[AisPoint], str]:
    ais_csv = first_existing_ais_csv(args.ais_csv)
    if ais_csv is not None:
        return read_ais_csv(ais_csv, hydro_lat, hydro_lon, args.max_ais_rows), str(ais_csv)
    return read_app_data_points(args.app_data, hydro_lat, hydro_lon), str(args.app_data)


def matching_target(point: AisPoint, targets: list[str]) -> str | None:
    haystack = normalize(f"{point.vessel_id} {point.name}")
    for target in targets:
        if normalize(target) in haystack:
            return target
    return None


def nearby_vessel_names(
    point: AisPoint,
    all_points: list[AisPoint],
    all_timestamps: list[dt.datetime],
    *,
    time_seconds: float,
    radius_km: float,
) -> tuple[str, ...]:
    start = point.timestamp - dt.timedelta(seconds=time_seconds)
    end = point.timestamp + dt.timedelta(seconds=time_seconds)
    left = bisect.bisect_left(all_timestamps, start)
    right = bisect.bisect_right(all_timestamps, end)
    names: dict[str, str] = {}
    for other in all_points[left:right]:
        if other.vessel_key == point.vessel_key:
            continue
        if other.distance_km > radius_km:
            continue
        names[other.vessel_key] = other.name or other.vessel_id or other.vessel_key
    return tuple(sorted(names.values()))


def build_target_events(
    points: list[AisPoint],
    recordings_start: dt.datetime,
    recordings_end: dt.datetime,
    args: argparse.Namespace,
) -> list[TargetEvent]:
    targets = args.target_vessel or DEFAULT_TARGETS
    half_window = dt.timedelta(seconds=args.window_seconds / 2.0)
    all_points = sorted(points, key=lambda item: item.timestamp)
    all_timestamps = [point.timestamp for point in all_points]
    grouped: dict[str, list[tuple[str, AisPoint]]] = defaultdict(list)
    for point in all_points:
        target = matching_target(point, targets)
        if target is None:
            continue
        if args.max_distance_km > 0 and point.distance_km > args.max_distance_km:
            continue
        grouped[point.vessel_key].append((target, point))

    events: list[TargetEvent] = []
    for vessel_key, rows in sorted(grouped.items()):
        selected_for_vessel = 0
        last_selected_time: dt.datetime | None = None
        for target, point in rows:
            if (
                last_selected_time is not None
                and (point.timestamp - last_selected_time).total_seconds() < args.sample_step_seconds
            ):
                continue
            propagation_delay = (point.distance_km * 1000.0) / args.sound_speed_m_s
            event_time = point.timestamp + dt.timedelta(seconds=propagation_delay)
            window_start = event_time - half_window
            window_end = event_time + half_window
            if window_end < recordings_start or window_start > recordings_end:
                continue
            nearby_names = nearby_vessel_names(
                point,
                all_points,
                all_timestamps,
                time_seconds=args.neighbor_time_seconds,
                radius_km=args.neighbor_radius_km,
            )
            profile_id = f"{vessel_key}_{point.timestamp.strftime('%Y%m%dT%H%M%S')}"
            events.append(
                TargetEvent(
                    profile_id=profile_id,
                    target_query=target,
                    point=point,
                    event_time=event_time,
                    propagation_delay_seconds=propagation_delay,
                    window_start=window_start,
                    window_end=window_end,
                    nearby_names=nearby_names,
                )
            )
            selected_for_vessel += 1
            last_selected_time = point.timestamp
            if args.per_vessel_limit > 0 and selected_for_vessel >= args.per_vessel_limit:
                break
    return events


def loudest_delta_seconds(row: dict[str, str]) -> float | None:
    event_offset = finite_float(row.get("event_offset_seconds"))
    if event_offset is None:
        return None
    times = parse_json_float_list(row.get("waveform_times_seconds"))
    rms_values = parse_json_float_list(row.get("waveform_rms_dbfs"))
    if not times or not rms_values:
        return None
    pairs = list(zip(times, rms_values))
    loudest_time = max(pairs, key=lambda pair: pair[1])[0]
    return abs(loudest_time - event_offset)


def calibration_weight(
    *,
    confidence: float,
    peak_dbfs: float,
    distance_km: float,
    nearby_vessel_count: int,
    loudest_delta: float | None,
) -> float:
    peak_score = max(0.1, min(1.5, (peak_dbfs + 60.0) / 30.0))
    distance_score = 1.0 / max(0.25, distance_km)
    isolation_score = 1.0 / (1.0 + nearby_vessel_count)
    timing_score = 1.0 if loudest_delta is None else 1.0 / (1.0 + loudest_delta / 5.0)
    return max(0.001, confidence * peak_score * min(4.0, distance_score) * isolation_score * timing_score)


def candidate_from_row(row: dict[str, str]) -> CalibrationCandidate | None:
    if str(row.get("captured", "")).lower() != "true":
        return None
    angles = tuple(parse_json_float_list(row.get("beam_angle_candidates_deg")))
    source_bearing = finite_float(row.get("source_bearing_deg"))
    confidence = finite_float(row.get("beam_confidence"))
    peak = finite_float(row.get("peak_dbfs"))
    distance = finite_float(row.get("source_distance_km"))
    if not angles or source_bearing is None or confidence is None or peak is None or distance is None:
        return None
    nearby_count = int(finite_float(row.get("nearby_vessel_count")) or 0)
    loudest_delta = finite_float(row.get("loudest_delta_seconds"))
    return CalibrationCandidate(
        profile_id=str(row.get("profile_id") or ""),
        target_query=str(row.get("target_query") or ""),
        vessel_key=str(row.get("event_id") or row.get("event_label") or ""),
        vessel_id=str(row.get("event_id") or ""),
        vessel_name=str(row.get("event_label") or row.get("event_id") or "Vessel"),
        source_time_utc=str(row.get("source_time_utc") or ""),
        source_distance_km=distance,
        source_bearing_deg=source_bearing,
        angle_candidates_deg=angles,
        beam_confidence=confidence,
        peak_dbfs=peak,
        rms_dbfs=finite_float(row.get("rms_dbfs_mean")),
        loudest_delta_seconds=loudest_delta,
        nearby_vessel_count=nearby_count,
        weight=calibration_weight(
            confidence=confidence,
            peak_dbfs=peak,
            distance_km=distance,
            nearby_vessel_count=nearby_count,
            loudest_delta=loudest_delta,
        ),
    )


def passes_calibration_filters(candidate: CalibrationCandidate, args: argparse.Namespace) -> bool:
    if candidate.source_distance_km > args.calibration_max_distance_km:
        return False
    if candidate.nearby_vessel_count > args.max_nearby_vessels:
        return False
    if candidate.beam_confidence < args.min_confidence:
        return False
    if candidate.peak_dbfs < args.min_peak_dbfs:
        return False
    if (
        candidate.loudest_delta_seconds is not None
        and candidate.loudest_delta_seconds > args.max_loudest_delta_seconds
    ):
        return False
    return True


def best_bearing_and_error(heading_deg: float, angle_sign: int, candidate: CalibrationCandidate) -> tuple[float, float]:
    bearings = [((heading_deg + angle_sign * angle) % 360.0) for angle in candidate.angle_candidates_deg]
    selected = min(bearings, key=lambda bearing: angular_difference_deg(bearing, candidate.source_bearing_deg))
    return selected, angular_difference_deg(selected, candidate.source_bearing_deg)


def score_heading(heading_deg: float, angle_sign: int, candidates: list[CalibrationCandidate]) -> dict[str, float]:
    weighted_sum = 0.0
    weighted_square_sum = 0.0
    weight_sum = 0.0
    errors: list[float] = []
    for candidate in candidates:
        _, error = best_bearing_and_error(heading_deg, angle_sign, candidate)
        weighted_sum += error * candidate.weight
        weighted_square_sum += error * error * candidate.weight
        weight_sum += candidate.weight
        errors.append(error)
    errors.sort()
    median = errors[len(errors) // 2] if errors else float("inf")
    return {
        "headingDeg": heading_deg % 360.0,
        "angleSign": float(angle_sign),
        "meanErrorDeg": weighted_sum / weight_sum if weight_sum else float("inf"),
        "rmseDeg": math.sqrt(weighted_square_sum / weight_sum) if weight_sum else float("inf"),
        "medianErrorDeg": median,
        "within15Deg": float(sum(error <= 15.0 for error in errors)),
        "within30Deg": float(sum(error <= 30.0 for error in errors)),
    }


def estimate_headings(
    candidates: list[CalibrationCandidate],
    grid_step_deg: float,
    top_count: int,
) -> list[dict[str, float]]:
    if not candidates:
        return []
    step = max(0.05, grid_step_deg)
    count = max(1, int(round(360.0 / step)))
    scores = [
        score_heading(index * step, angle_sign, candidates)
        for angle_sign in (1, -1)
        for index in range(count)
    ]
    scores.sort(key=lambda row: (row["meanErrorDeg"], row["rmseDeg"], row["medianErrorDeg"]))
    return scores[: max(1, top_count)]


def calibration_status(best: dict[str, float] | None, candidates: list[CalibrationCandidate]) -> tuple[str, str]:
    if best is None:
        return "no_candidates", "No target-vessel windows passed the calibration filters."
    vessel_count = len({candidate.vessel_key for candidate in candidates})
    if len(candidates) < 6 or vessel_count < 2:
        return "too_few_candidates", "Use more target windows or at least two distinct trusted vessels."
    within30_ratio = best["within30Deg"] / max(1, len(candidates))
    if best["meanErrorDeg"] <= 25.0 and best["medianErrorDeg"] <= 30.0 and within30_ratio >= 0.6:
        return "usable", "Target-vessel beam angles are internally consistent enough to try in the app."
    if best["meanErrorDeg"] <= 45.0 and within30_ratio >= 0.4:
        return "tentative", "Calibration is weak; inspect the candidates before rebuilding the app."
    return "inconsistent", "Target-vessel windows disagree; do not set this heading blindly."


def summarize_by_vessel(
    candidates: list[CalibrationCandidate],
    best: dict[str, float] | None,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[CalibrationCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.vessel_key].append(candidate)
    rows: list[dict[str, Any]] = []
    for vessel_key, vessel_candidates in sorted(grouped.items()):
        errors: list[float] = []
        if best is not None:
            for candidate in vessel_candidates:
                _, error = best_bearing_and_error(best["headingDeg"], int(best["angleSign"]), candidate)
                errors.append(error)
        rows.append(
            {
                "vesselId": vessel_candidates[0].vessel_id,
                "vesselName": vessel_candidates[0].vessel_name,
                "candidateCount": len(vessel_candidates),
                "meanDistanceKm": round(sum(item.source_distance_km for item in vessel_candidates) / len(vessel_candidates), 3),
                "meanBeamConfidence": round(sum(item.beam_confidence for item in vessel_candidates) / len(vessel_candidates), 3),
                "meanBearingErrorDeg": round(sum(errors) / len(errors), 3) if errors else None,
                "maxBearingErrorDeg": round(max(errors), 3) if errors else None,
            }
        )
    return rows


def candidate_csv_row(
    candidate: CalibrationCandidate,
    best: dict[str, float] | None,
) -> dict[str, Any]:
    selected_bearing = None
    error = None
    if best is not None:
        selected_bearing, error = best_bearing_and_error(best["headingDeg"], int(best["angleSign"]), candidate)
    return {
        "profile_id": candidate.profile_id,
        "target_query": candidate.target_query,
        "vessel_id": candidate.vessel_id,
        "vessel_name": candidate.vessel_name,
        "source_time_utc": candidate.source_time_utc,
        "source_distance_km": f"{candidate.source_distance_km:.4f}",
        "source_bearing_deg": f"{candidate.source_bearing_deg:.3f}",
        "beam_angle_candidates_deg": json.dumps([round(value, 3) for value in candidate.angle_candidates_deg], separators=(",", ":")),
        "selected_beam_bearing_deg": f"{selected_bearing:.3f}" if selected_bearing is not None else "",
        "bearing_error_deg": f"{error:.3f}" if error is not None else "",
        "beam_confidence": f"{candidate.beam_confidence:.3f}",
        "peak_dbfs": f"{candidate.peak_dbfs:.3f}",
        "rms_dbfs": f"{candidate.rms_dbfs:.3f}" if candidate.rms_dbfs is not None else "",
        "loudest_delta_seconds": f"{candidate.loudest_delta_seconds:.3f}" if candidate.loudest_delta_seconds is not None else "",
        "nearby_vessel_count": str(candidate.nearby_vessel_count),
        "calibration_weight": f"{candidate.weight:.6f}",
    }


def unique_columns(columns: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for column in columns:
        if column not in seen:
            seen.add(column)
            result.append(column)
    return result


def main() -> None:
    args = parse_args()
    targets = args.target_vessel or DEFAULT_TARGETS
    hydro_lat, hydro_lon = load_hydrophone(args.app_data)
    ais_points, ais_source = load_ais_points(args, hydro_lat, hydro_lon)
    if not ais_points:
        raise FileNotFoundError("No AIS points were available for target-vessel calibration.")

    recordings = recording_index(args.recordings_dir)
    cache = RecordingCache(max_files=args.cache_files)
    events = build_target_events(ais_points, recordings[0].start, recordings[-1].end, args)
    if not events:
        raise RuntimeError(f"No target-vessel AIS points matched {targets!r} inside the recording time range.")

    profile_fieldnames = unique_columns(OUTPUT_COLUMNS + [band_column(*band) for band in DEFAULT_BANDS] + EXTRA_PROFILE_COLUMNS)
    args.output_profiles.parent.mkdir(parents=True, exist_ok=True)
    profile_rows: list[dict[str, str]] = []
    captured_count = 0

    with args.output_profiles.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=profile_fieldnames, extrasaction="ignore")
        writer.writeheader()
        for index, event in enumerate(events, start=1):
            point = event.point
            row = profile_row(
                event_type="vessel",
                event_id=point.vessel_id or point.vessel_key,
                event_label=point.name or point.vessel_id or point.vessel_key,
                source_time=point.timestamp,
                event_time=event.event_time,
                source_distance_km=point.distance_km,
                source_bearing_deg=point.bearing_deg,
                propagation_delay_seconds=event.propagation_delay_seconds,
                window_start=event.window_start,
                window_end=event.window_end,
                recordings=recordings,
                cache=cache,
                block_seconds=args.block_seconds,
                waveform_bins=args.waveform_bins,
                skip_beamforming=False,
                beam_window_seconds=args.beam_window_seconds,
                beam_fmin_hz=args.beam_fmin_hz,
                beam_fmax_hz=args.beam_fmax_hz,
                mic_spacing_m=args.mic_spacing_m,
                sound_speed_m_s=args.sound_speed_m_s,
                array_heading_deg=None,
                array_angle_sign=1,
            )
            loudest_delta = loudest_delta_seconds(row)
            candidate = candidate_from_row(
                {
                    **row,
                    "profile_id": event.profile_id,
                    "target_query": event.target_query,
                    "nearby_vessel_count": str(len(event.nearby_names)),
                    "loudest_delta_seconds": f"{loudest_delta:.3f}" if loudest_delta is not None else "",
                }
            )
            is_candidate = candidate is not None and passes_calibration_filters(candidate, args)
            row.update(
                {
                    "profile_id": event.profile_id,
                    "target_query": event.target_query,
                    "source_lat": f"{point.latitude:.7f}",
                    "source_lon": f"{point.longitude:.7f}",
                    "source_sog_kn": f"{point.sog:.3f}" if point.sog is not None else "",
                    "source_cog_deg": f"{point.cog:.3f}" if point.cog is not None else "",
                    "source_ship_type": point.ship_type,
                    "nearby_vessel_count": str(len(event.nearby_names)),
                    "nearby_vessel_names": ";".join(event.nearby_names),
                    "loudest_delta_seconds": f"{loudest_delta:.3f}" if loudest_delta is not None else "",
                    "calibration_candidate": "true" if is_candidate else "false",
                    "calibration_weight": f"{candidate.weight:.6f}" if candidate is not None else "",
                }
            )
            writer.writerow(row)
            profile_rows.append(row)
            captured_count += row.get("captured") == "true"
            if index % 25 == 0:
                print(f"Built {index}/{len(events)} target-vessel audio windows...")

    all_candidates = [candidate for row in profile_rows if (candidate := candidate_from_row(row)) is not None]
    calibration_candidates = [
        candidate for candidate in all_candidates if passes_calibration_filters(candidate, args)
    ]
    best_headings = estimate_headings(calibration_candidates, args.grid_step_deg, args.top_count)
    best = best_headings[0] if best_headings else None
    status, status_note = calibration_status(best, calibration_candidates)

    args.output_candidates.parent.mkdir(parents=True, exist_ok=True)
    candidate_fieldnames = [
        "profile_id",
        "target_query",
        "vessel_id",
        "vessel_name",
        "source_time_utc",
        "source_distance_km",
        "source_bearing_deg",
        "beam_angle_candidates_deg",
        "selected_beam_bearing_deg",
        "bearing_error_deg",
        "beam_confidence",
        "peak_dbfs",
        "rms_dbfs",
        "loudest_delta_seconds",
        "nearby_vessel_count",
        "calibration_weight",
    ]
    with args.output_candidates.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=candidate_fieldnames)
        writer.writeheader()
        for candidate in calibration_candidates:
            writer.writerow(candidate_csv_row(candidate, best))

    report = {
        "status": status,
        "statusNote": status_note,
        "targets": targets,
        "aisSource": ais_source,
        "hydrophone": {"latitude": hydro_lat, "longitude": hydro_lon},
        "settings": {
            "windowSeconds": args.window_seconds,
            "sampleStepSeconds": args.sample_step_seconds,
            "maxDistanceKm": args.max_distance_km,
            "calibrationMaxDistanceKm": args.calibration_max_distance_km,
            "soundSpeedMS": args.sound_speed_m_s,
            "micSpacingM": args.mic_spacing_m,
            "beamFrequencyMinHz": args.beam_fmin_hz,
            "beamFrequencyMaxHz": args.beam_fmax_hz,
            "maxNearbyVessels": args.max_nearby_vessels,
            "minConfidence": args.min_confidence,
            "minPeakDbfs": args.min_peak_dbfs,
            "maxLoudestDeltaSeconds": args.max_loudest_delta_seconds,
        },
        "profileCount": len(profile_rows),
        "capturedProfileCount": captured_count,
        "beamProfileCount": len(all_candidates),
        "calibrationCandidateCount": len(calibration_candidates),
        "distinctCalibrationVessels": len({candidate.vessel_key for candidate in calibration_candidates}),
        "bestHeading": best,
        "topHeadings": best_headings,
        "suggestedEnvironment": (
            {
                "HYDROPHONE_MIC_SPACING_M": f"{args.mic_spacing_m:g}",
                "HYDROPHONE_SOUND_SPEED_M_S": f"{args.sound_speed_m_s:g}",
                "HYDROPHONE_ARRAY_HEADING_DEG": f"{best['headingDeg']:.3f}",
                "HYDROPHONE_ARRAY_ANGLE_SIGN": str(int(best["angleSign"])),
            }
            if best is not None
            else {}
        ),
        "byVessel": summarize_by_vessel(calibration_candidates, best),
        "outputs": {
            "profilesCsv": str(args.output_profiles),
            "candidatesCsv": str(args.output_candidates),
            "reportJson": str(args.output_json),
        },
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Wrote {args.output_profiles}")
    print(f"Wrote {args.output_candidates}")
    print(f"Wrote {args.output_json}")
    print(f"Profiles: {len(profile_rows)}; captured: {captured_count}; calibration candidates: {len(calibration_candidates)}")
    print(f"Status: {status} - {status_note}")
    if best is not None:
        print(
            "Best heading: "
            f"{best['headingDeg']:.1f} deg, sign {int(best['angleSign']):+d}, "
            f"mean error {best['meanErrorDeg']:.1f} deg, median {best['medianErrorDeg']:.1f} deg"
        )


if __name__ == "__main__":
    main()
