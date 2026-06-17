#!/usr/bin/env python3
"""Estimate hydrophone array heading from AIS bearings and relative beam angles."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

UTC = dt.timezone.utc


@dataclass(frozen=True)
class Candidate:
    vessel_id: str
    vessel_name: str
    event_time_utc: str
    source_bearing_deg: float
    angle_candidates_deg: tuple[float, ...]
    confidence: float
    source_distance_km: float
    peak_dbfs: float
    rms_dbfs: float | None
    loudest_delta_seconds: float | None
    shared_overlap_seconds: float
    weight: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-data", type=Path, default=Path("web/data/app_data.json"))
    parser.add_argument("--output-json", type=Path, default=Path("outputs/processing/array_heading_estimate.json"))
    parser.add_argument("--output-csv", type=Path, default=Path("outputs/processing/array_heading_candidates.csv"))
    parser.add_argument("--grid-step-deg", type=float, default=0.5)
    parser.add_argument("--max-distance-km", type=float, default=3.0)
    parser.add_argument("--min-confidence", type=float, default=0.12)
    parser.add_argument("--min-peak-dbfs", type=float, default=-35.0)
    parser.add_argument("--max-loudest-delta-seconds", type=float, default=10.0)
    parser.add_argument("--max-shared-overlap-seconds", type=float, default=8.0)
    parser.add_argument("--include-shared", action="store_true")
    parser.add_argument(
        "--include-vessel",
        action="append",
        default=[],
        help="Only use vessels whose MMSI or name matches this value. Can be repeated.",
    )
    parser.add_argument(
        "--exclude-vessel",
        action="append",
        default=[],
        help="Skip vessels whose MMSI or name matches this value. Can be repeated.",
    )
    parser.add_argument("--top-count", type=int, default=12)
    return parser.parse_args()


def finite_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def angular_difference_deg(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def window_overlap_seconds(first: dict[str, Any], second: dict[str, Any]) -> float:
    first_start = parse_time(first.get("startUtc"))
    first_end = parse_time(first.get("endUtc"))
    second_start = parse_time(second.get("startUtc"))
    second_end = parse_time(second.get("endUtc"))
    if first_start is None or first_end is None or second_start is None or second_end is None:
        return 0.0
    overlap = min(first_end, second_end) - max(first_start, second_start)
    return max(0.0, overlap.total_seconds())


def loudest_delta_seconds(audio: dict[str, Any]) -> float | None:
    waveform = audio.get("waveform") or {}
    times = waveform.get("timesSeconds") or []
    rms_values = waveform.get("rmsDbfs") or []
    event_offset = finite_float(audio.get("eventOffsetSeconds"))
    if event_offset is None:
        return None

    rows: list[tuple[float, float]] = []
    for time_value, rms_value in zip(times, rms_values):
        time_seconds = finite_float(time_value)
        rms_dbfs = finite_float(rms_value)
        if time_seconds is not None and rms_dbfs is not None:
            rows.append((time_seconds, rms_dbfs))
    if not rows:
        return None

    loudest_time = max(rows, key=lambda row: row[1])[0]
    return abs(loudest_time - event_offset)


def matches_any(vessel: dict[str, Any], values: list[str]) -> bool:
    if not values:
        return False
    haystack = f"{vessel.get('id', '')} {vessel.get('name', '')}".lower()
    return any(value.lower() in haystack for value in values)


def shared_overlap_for_vessel(vessel: dict[str, Any], vessels: list[dict[str, Any]]) -> float:
    audio = vessel.get("audio") or {}
    overlaps = [
        window_overlap_seconds(audio, (other.get("audio") or {}))
        for other in vessels
        if other is not vessel and (other.get("audio") or {}).get("captured") is True
    ]
    return max(overlaps, default=0.0)


def build_candidates(data: dict[str, Any], args: argparse.Namespace) -> list[Candidate]:
    vessels = data.get("vessels") or []
    candidates: list[Candidate] = []

    for vessel in vessels:
        if args.include_vessel and not matches_any(vessel, args.include_vessel):
            continue
        if matches_any(vessel, args.exclude_vessel):
            continue

        audio = vessel.get("audio") or {}
        beam = audio.get("beam") or {}
        if audio.get("captured") is not True:
            continue

        source_bearing = finite_float(audio.get("sourceBearingDeg"))
        confidence = finite_float(beam.get("confidence"))
        distance = finite_float(audio.get("sourceDistanceKm"))
        peak = finite_float(audio.get("peakDbfs"))
        angles = tuple(
            angle
            for value in beam.get("angleCandidatesDeg") or []
            if (angle := finite_float(value)) is not None
        )
        if not angles or source_bearing is None or confidence is None or distance is None or peak is None:
            continue
        if distance > args.max_distance_km or confidence < args.min_confidence or peak < args.min_peak_dbfs:
            continue

        loudest_delta = loudest_delta_seconds(audio)
        if loudest_delta is not None and loudest_delta > args.max_loudest_delta_seconds:
            continue

        shared_overlap = shared_overlap_for_vessel(vessel, vessels)
        if not args.include_shared and shared_overlap > args.max_shared_overlap_seconds:
            continue

        distance_weight = 1.0 / max(0.25, distance)
        weight = max(0.01, confidence * min(4.0, distance_weight))
        candidates.append(
            Candidate(
                vessel_id=str(vessel.get("id") or ""),
                vessel_name=str(vessel.get("name") or vessel.get("id") or "Vessel"),
                event_time_utc=str(audio.get("eventTimeUtc") or ""),
                source_bearing_deg=source_bearing,
                angle_candidates_deg=angles,
                confidence=confidence,
                source_distance_km=distance,
                peak_dbfs=peak,
                rms_dbfs=finite_float(audio.get("rmsDbfsMean")),
                loudest_delta_seconds=loudest_delta,
                shared_overlap_seconds=shared_overlap,
                weight=weight,
            )
        )

    return candidates


def best_error_for_heading(heading_deg: float, angle_sign: int, candidate: Candidate) -> tuple[float, float]:
    bearings = [((heading_deg + angle_sign * angle) % 360.0) for angle in candidate.angle_candidates_deg]
    best_bearing = min(
        bearings,
        key=lambda bearing: angular_difference_deg(bearing, candidate.source_bearing_deg),
    )
    return best_bearing, angular_difference_deg(best_bearing, candidate.source_bearing_deg)


def score_heading(heading_deg: float, angle_sign: int, candidates: list[Candidate]) -> dict[str, float]:
    weighted_sum = 0.0
    weighted_square_sum = 0.0
    weight_sum = 0.0
    errors: list[float] = []
    for candidate in candidates:
        _, error = best_error_for_heading(heading_deg, angle_sign, candidate)
        weighted_sum += error * candidate.weight
        weighted_square_sum += error * error * candidate.weight
        weight_sum += candidate.weight
        errors.append(error)

    errors = sorted(errors)
    median = errors[len(errors) // 2] if errors else float("inf")
    return {
        "headingDeg": heading_deg % 360.0,
        "angleSign": float(angle_sign),
        "meanErrorDeg": weighted_sum / weight_sum if weight_sum else float("inf"),
        "rmseDeg": math.sqrt(weighted_square_sum / weight_sum) if weight_sum else float("inf"),
        "medianErrorDeg": median,
        "within15Deg": sum(error <= 15.0 for error in errors),
        "within30Deg": sum(error <= 30.0 for error in errors),
    }


def estimate_heading(candidates: list[Candidate], grid_step_deg: float, top_count: int) -> list[dict[str, float]]:
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


def calibration_status(best_heading: dict[str, float] | None, candidate_count: int) -> tuple[str, str]:
    if best_heading is None:
        return "no_candidates", "No events matched the calibration filters."
    if candidate_count < 5:
        return "too_few_candidates", "Too few events matched; use this only as a manual hint."
    within30_ratio = best_heading["within30Deg"] / max(1, candidate_count)
    if best_heading["meanErrorDeg"] <= 25.0 and best_heading["medianErrorDeg"] <= 30.0 and within30_ratio >= 0.6:
        return "usable", "Calibration is internally consistent enough to try in the app."
    if best_heading["meanErrorDeg"] <= 45.0 and within30_ratio >= 0.4:
        return "tentative", "Calibration is weak; inspect the CSV before using it."
    return "inconsistent", "Candidate bearings disagree; do not set this heading blindly."


def candidate_row(candidate: Candidate, heading_deg: float | None, angle_sign: int | None) -> dict[str, Any]:
    selected_bearing = None
    error = None
    if heading_deg is not None and angle_sign is not None:
        selected_bearing, error = best_error_for_heading(heading_deg, angle_sign, candidate)
    return {
        "vessel_id": candidate.vessel_id,
        "vessel_name": candidate.vessel_name,
        "event_time_utc": candidate.event_time_utc,
        "source_bearing_deg": round(candidate.source_bearing_deg, 3),
        "angle_candidates_deg": json.dumps([round(angle, 3) for angle in candidate.angle_candidates_deg]),
        "selected_beam_bearing_deg": "" if selected_bearing is None else round(selected_bearing, 3),
        "bearing_error_deg": "" if error is None else round(error, 3),
        "array_angle_sign": "" if angle_sign is None else angle_sign,
        "beam_confidence": round(candidate.confidence, 3),
        "source_distance_km": round(candidate.source_distance_km, 4),
        "peak_dbfs": round(candidate.peak_dbfs, 3),
        "rms_dbfs": "" if candidate.rms_dbfs is None else round(candidate.rms_dbfs, 3),
        "loudest_delta_seconds": ""
        if candidate.loudest_delta_seconds is None
        else round(candidate.loudest_delta_seconds, 3),
        "shared_overlap_seconds": round(candidate.shared_overlap_seconds, 3),
        "weight": round(candidate.weight, 6),
    }


def write_candidate_csv(
    path: Path,
    candidates: list[Candidate],
    heading_deg: float | None,
    angle_sign: int | None,
) -> None:
    fieldnames = list(candidate_row(candidates[0], heading_deg, angle_sign).keys()) if candidates else [
        "vessel_id",
        "vessel_name",
        "event_time_utc",
        "source_bearing_deg",
        "angle_candidates_deg",
        "selected_beam_bearing_deg",
        "bearing_error_deg",
        "array_angle_sign",
        "beam_confidence",
        "source_distance_km",
        "peak_dbfs",
        "rms_dbfs",
        "loudest_delta_seconds",
        "shared_overlap_seconds",
        "weight",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(candidate_row(candidate, heading_deg, angle_sign))


def main() -> None:
    args = parse_args()
    data = json.loads(args.app_data.read_text(encoding="utf-8"))
    candidates = build_candidates(data, args)
    top_headings = estimate_heading(candidates, args.grid_step_deg, args.top_count)
    best_heading = top_headings[0] if top_headings else None
    best_heading_deg = best_heading["headingDeg"] if best_heading else None
    best_angle_sign = int(best_heading["angleSign"]) if best_heading else None
    status, recommendation = calibration_status(best_heading, len(candidates))

    write_candidate_csv(args.output_csv, candidates, best_heading_deg, best_angle_sign)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "generatedAtUtc": dt.datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "appData": str(args.app_data),
        "candidateCount": len(candidates),
        "filters": {
            "maxDistanceKm": args.max_distance_km,
            "minConfidence": args.min_confidence,
            "minPeakDbfs": args.min_peak_dbfs,
            "maxLoudestDeltaSeconds": args.max_loudest_delta_seconds,
            "maxSharedOverlapSeconds": args.max_shared_overlap_seconds,
            "includeShared": args.include_shared,
            "includeVessel": args.include_vessel,
            "excludeVessel": args.exclude_vessel,
            "gridStepDeg": args.grid_step_deg,
        },
        "bestHeadingDeg": None if best_heading_deg is None else round(best_heading_deg, 3),
        "bestAngleSign": best_angle_sign,
        "calibrationStatus": status,
        "recommendation": recommendation,
        "topHeadings": [
            {
                "headingDeg": round(row["headingDeg"], 3),
                "angleSign": int(row["angleSign"]),
                "meanErrorDeg": round(row["meanErrorDeg"], 3),
                "rmseDeg": round(row["rmseDeg"], 3),
                "medianErrorDeg": round(row["medianErrorDeg"], 3),
                "within15Deg": int(row["within15Deg"]),
                "within30Deg": int(row["within30Deg"]),
            }
            for row in top_headings
        ],
        "candidateCsv": str(args.output_csv),
        "note": (
            "This estimates array heading from AIS bearings and two-channel relative beam angles. "
            "It is only a calibration hint; inspect the candidate CSV before trusting the heading."
        ),
    }
    args.output_json.write_text(json.dumps(result, indent=2), encoding="utf-8")

    if best_heading is None:
        print("No calibration candidates matched the filters.")
        print(f"Wrote {args.output_csv}")
        print(f"Wrote {args.output_json}")
        return

    print(f"Candidates: {len(candidates)}")
    print(f"Status: {status} - {recommendation}")
    print(f"Best heading: {best_heading_deg:.1f} deg")
    print(f"Best angle sign: {best_angle_sign:+d}")
    print(
        "Error: "
        f"mean {best_heading['meanErrorDeg']:.1f} deg, "
        f"median {best_heading['medianErrorDeg']:.1f} deg, "
        f"within 30 deg {int(best_heading['within30Deg'])}/{len(candidates)}"
    )
    if status in {"usable", "tentative"}:
        print(f"Set with: $env:HYDROPHONE_ARRAY_HEADING_DEG = \"{best_heading_deg:.1f}\"")
        print(f"          $env:HYDROPHONE_ARRAY_ANGLE_SIGN = \"{best_angle_sign:+d}\"")
    else:
        print("Do not set this automatically; inspect the candidate CSV first.")
    print(f"Wrote {args.output_csv}")
    print(f"Wrote {args.output_json}")


if __name__ == "__main__":
    main()
