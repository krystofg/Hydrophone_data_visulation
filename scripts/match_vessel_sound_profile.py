#!/usr/bin/env python3
"""Build vessel acoustic fingerprints and rank similar vessel sound profiles."""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BAND_COLUMNS = [
    "band_20_100_db",
    "band_100_500_db",
    "band_500_2000_db",
    "band_2000_10000_db",
]


@dataclass(frozen=True)
class Fingerprint:
    vessel_id: str
    vessel_name: str
    event_time_utc: str
    source_distance_km: float | None
    source_bearing_deg: float | None
    beam_best_bearing_deg: float | None
    beam_confidence: float | None
    bearing_error_deg: float | None
    rms_dbfs: float | None
    peak_dbfs: float | None
    crest_db: float | None
    stereo_correlation: float | None
    feature_values: dict[str, float]
    raw_row: dict[str, str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event-profiles-csv",
        type=Path,
        default=Path("outputs/processing/event_audio_profiles.csv"),
    )
    parser.add_argument("--query-vessel", default="", help="MMSI/name/event label to match.")
    parser.add_argument("--query-event-id", default="", help="Exact event_id to match.")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--include-self", action="store_true")
    parser.add_argument(
        "--output-matches",
        type=Path,
        default=Path("outputs/processing/vessel_sound_matches.csv"),
    )
    parser.add_argument(
        "--output-fingerprints",
        type=Path,
        default=Path("outputs/processing/vessel_sound_fingerprints.csv"),
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("outputs/processing/vessel_sound_match_report.json"),
    )
    return parser.parse_args()


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_float_list(value: Any) -> list[float]:
    if value is None:
        return []
    text = str(value).strip()
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
        number = parse_float(item)
        if number is not None:
            values.append(number)
    return values


def interpolate(values: list[float], size: int) -> list[float]:
    if not values or size <= 0:
        return []
    if len(values) == 1:
        return [values[0]] * size
    output: list[float] = []
    last = len(values) - 1
    for index in range(size):
        position = (index / max(1, size - 1)) * last
        left = int(math.floor(position))
        right = min(last, left + 1)
        fraction = position - left
        output.append(values[left] * (1 - fraction) + values[right] * fraction)
    return output


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((value - m) ** 2 for value in values) / len(values))


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    left = int(math.floor(position))
    right = min(len(ordered) - 1, left + 1)
    mix = position - left
    return ordered[left] * (1 - mix) + ordered[right] * mix


def waveform_features(row: dict[str, str]) -> dict[str, float]:
    rms_values = parse_float_list(row.get("waveform_rms_dbfs"))
    if len(rms_values) < 4:
        return {}

    envelope = interpolate(rms_values, 32)
    envelope_mean = mean(envelope)
    envelope_std = std(envelope)
    normalized = [
        (value - envelope_mean) / envelope_std if envelope_std > 1e-9 else 0.0
        for value in envelope
    ]

    features: dict[str, float] = {
        "wave_mean_dbfs": envelope_mean,
        "wave_std_db": envelope_std,
        "wave_peak_prominence_db": max(envelope) - envelope_mean,
        "wave_p10_dbfs": percentile(envelope, 0.10) or envelope_mean,
        "wave_p90_dbfs": percentile(envelope, 0.90) or envelope_mean,
    }
    for index, value in enumerate(normalized):
        features[f"wave_shape_{index:02d}"] = value
    return features


def build_feature_values(row: dict[str, str]) -> dict[str, float]:
    values: dict[str, float] = {}

    bands = [parse_float(row.get(column)) for column in BAND_COLUMNS]
    valid_bands = [value for value in bands if value is not None]
    if valid_bands:
        band_mean = mean(valid_bands)
        for column, value in zip(BAND_COLUMNS, bands):
            if value is not None:
                values[f"{column}_shape"] = value - band_mean
                values[column] = value
        values["band_spread_db"] = max(valid_bands) - min(valid_bands)
        values["low_mid_ratio_db"] = (bands[0] or band_mean) - (bands[1] or band_mean)
        values["mid_high_ratio_db"] = (bands[1] or band_mean) - (bands[2] or band_mean)

    for column in ("rms_dbfs_mean", "peak_dbfs", "crest_factor_db", "stereo_correlation"):
        value = parse_float(row.get(column))
        if value is not None:
            values[column] = value

    distance_km = parse_float(row.get("source_distance_km"))
    rms_dbfs = parse_float(row.get("rms_dbfs_mean"))
    peak_dbfs = parse_float(row.get("peak_dbfs"))
    if distance_km is not None and distance_km > 0:
        spreading = 20.0 * math.log10(max(distance_km, 0.05))
        if rms_dbfs is not None:
            values["rms_distance_corrected_db"] = rms_dbfs + spreading
        if peak_dbfs is not None:
            values["peak_distance_corrected_db"] = peak_dbfs + spreading

    beam_confidence = parse_float(row.get("beam_confidence"))
    bearing_error = parse_float(row.get("bearing_error_deg"))
    if beam_confidence is not None:
        values["beam_confidence"] = beam_confidence
    if bearing_error is not None:
        values["beam_bearing_agreement"] = max(0.0, 1.0 - bearing_error / 180.0)

    values.update(waveform_features(row))
    return values


def load_fingerprints(path: Path) -> list[Fingerprint]:
    fingerprints: list[Fingerprint] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as csv_file:
        for row in csv.DictReader(csv_file):
            if row.get("event_type") != "vessel" or row.get("captured") != "true":
                continue
            feature_values = build_feature_values(row)
            if not feature_values:
                continue
            fingerprints.append(
                Fingerprint(
                    vessel_id=row.get("event_id", ""),
                    vessel_name=row.get("event_label", "") or row.get("event_id", ""),
                    event_time_utc=row.get("event_time_utc", ""),
                    source_distance_km=parse_float(row.get("source_distance_km")),
                    source_bearing_deg=parse_float(row.get("source_bearing_deg")),
                    beam_best_bearing_deg=parse_float(row.get("beam_best_bearing_deg")),
                    beam_confidence=parse_float(row.get("beam_confidence")),
                    bearing_error_deg=parse_float(row.get("bearing_error_deg")),
                    rms_dbfs=parse_float(row.get("rms_dbfs_mean")),
                    peak_dbfs=parse_float(row.get("peak_dbfs")),
                    crest_db=parse_float(row.get("crest_factor_db")),
                    stereo_correlation=parse_float(row.get("stereo_correlation")),
                    feature_values=feature_values,
                    raw_row=row,
                )
            )
    return fingerprints


def feature_stats(fingerprints: list[Fingerprint]) -> dict[str, tuple[float, float]]:
    names = sorted({name for fp in fingerprints for name in fp.feature_values})
    stats: dict[str, tuple[float, float]] = {}
    for name in names:
        values = [fp.feature_values[name] for fp in fingerprints if name in fp.feature_values]
        if len(values) < 3:
            continue
        sigma = std(values)
        if sigma <= 1e-9:
            continue
        stats[name] = (mean(values), sigma)
    return stats


def vectorize(fingerprint: Fingerprint, stats: dict[str, tuple[float, float]]) -> dict[str, float]:
    vector: dict[str, float] = {}
    for name, (mu, sigma) in stats.items():
        if name in fingerprint.feature_values:
            vector[name] = (fingerprint.feature_values[name] - mu) / sigma
    return vector


def feature_weight(name: str) -> float:
    if name.endswith("_shape") or "ratio" in name:
        return 1.8
    if name.startswith("wave_shape_"):
        return 0.35
    if name.startswith("wave_"):
        return 0.8
    if "distance_corrected" in name:
        return 0.8
    if name in {"rms_dbfs_mean", "peak_dbfs"}:
        return 0.45
    if name == "beam_bearing_agreement":
        return 0.7
    return 1.0


def compare_vectors(query: dict[str, float], candidate: dict[str, float]) -> tuple[float, list[tuple[str, float]]]:
    shared = sorted(set(query) & set(candidate))
    if not shared:
        return float("inf"), []
    weighted_square_sum = 0.0
    weight_sum = 0.0
    contributions: list[tuple[str, float]] = []
    for name in shared:
        weight = feature_weight(name)
        delta = query[name] - candidate[name]
        contribution = weight * delta * delta
        weighted_square_sum += contribution
        weight_sum += weight
        contributions.append((name, math.sqrt(contribution)))
    distance = math.sqrt(weighted_square_sum / max(weight_sum, 1e-9))
    contributions.sort(key=lambda item: item[1], reverse=True)
    return distance, contributions


def similarity_score(distance: float) -> float:
    if not math.isfinite(distance):
        return 0.0
    return 100.0 * math.exp(-0.85 * distance)


def find_query(fingerprints: list[Fingerprint], args: argparse.Namespace) -> Fingerprint:
    if args.query_event_id:
        for fingerprint in fingerprints:
            if fingerprint.vessel_id == args.query_event_id:
                return fingerprint
        raise SystemExit(f"No vessel event_id matched {args.query_event_id!r}")

    query = args.query_vessel.strip().lower()
    if not query:
        raise SystemExit("Pass --query-vessel or --query-event-id.")
    matches = [
        fingerprint
        for fingerprint in fingerprints
        if query in fingerprint.vessel_id.lower() or query in fingerprint.vessel_name.lower()
    ]
    if not matches:
        raise SystemExit(f"No vessel matched {args.query_vessel!r}")
    matches.sort(key=lambda fp: (fp.source_distance_km is None, fp.source_distance_km or 999.0))
    return matches[0]


def write_fingerprints(path: Path, fingerprints: list[Fingerprint]) -> None:
    columns = [
        "vessel_id",
        "vessel_name",
        "event_time_utc",
        "source_distance_km",
        "source_bearing_deg",
        "beam_best_bearing_deg",
        "beam_confidence",
        "bearing_error_deg",
        "rms_dbfs",
        "peak_dbfs",
        "crest_db",
        "stereo_correlation",
        "feature_count",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns)
        writer.writeheader()
        for fp in fingerprints:
            writer.writerow(
                {
                    "vessel_id": fp.vessel_id,
                    "vessel_name": fp.vessel_name,
                    "event_time_utc": fp.event_time_utc,
                    "source_distance_km": fp.source_distance_km,
                    "source_bearing_deg": fp.source_bearing_deg,
                    "beam_best_bearing_deg": fp.beam_best_bearing_deg,
                    "beam_confidence": fp.beam_confidence,
                    "bearing_error_deg": fp.bearing_error_deg,
                    "rms_dbfs": fp.rms_dbfs,
                    "peak_dbfs": fp.peak_dbfs,
                    "crest_db": fp.crest_db,
                    "stereo_correlation": fp.stereo_correlation,
                    "feature_count": len(fp.feature_values),
                }
            )


def row_for_match(
    rank: int,
    query: Fingerprint,
    candidate: Fingerprint,
    distance: float,
    contributions: list[tuple[str, float]],
) -> dict[str, Any]:
    return {
        "rank": rank,
        "query_id": query.vessel_id,
        "query_name": query.vessel_name,
        "candidate_id": candidate.vessel_id,
        "candidate_name": candidate.vessel_name,
        "similarity_score": round(similarity_score(distance), 2),
        "feature_distance": round(distance, 4),
        "candidate_event_time_utc": candidate.event_time_utc,
        "candidate_distance_km": candidate.source_distance_km,
        "candidate_rms_dbfs": candidate.rms_dbfs,
        "candidate_peak_dbfs": candidate.peak_dbfs,
        "candidate_beam_confidence": candidate.beam_confidence,
        "candidate_bearing_error_deg": candidate.bearing_error_deg,
        "largest_differences": ";".join(name for name, _ in contributions[:5]),
    }


def write_matches(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "rank",
        "query_id",
        "query_name",
        "candidate_id",
        "candidate_name",
        "similarity_score",
        "feature_distance",
        "candidate_event_time_utc",
        "candidate_distance_km",
        "candidate_rms_dbfs",
        "candidate_peak_dbfs",
        "candidate_beam_confidence",
        "candidate_bearing_error_deg",
        "largest_differences",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> None:
    args = parse_args()
    fingerprints = load_fingerprints(args.event_profiles_csv)
    if len(fingerprints) < 2:
        raise SystemExit("Need at least two captured vessel profiles to match.")

    query = find_query(fingerprints, args)
    stats = feature_stats(fingerprints)
    vectors = {fp.vessel_id: vectorize(fp, stats) for fp in fingerprints}
    query_vector = vectors[query.vessel_id]

    ranked: list[tuple[Fingerprint, float, list[tuple[str, float]]]] = []
    for candidate in fingerprints:
        if not args.include_self and candidate.vessel_id == query.vessel_id:
            continue
        distance, contributions = compare_vectors(query_vector, vectors[candidate.vessel_id])
        ranked.append((candidate, distance, contributions))
    ranked.sort(key=lambda item: item[1])
    ranked = ranked[: max(1, args.top)]

    match_rows = [
        row_for_match(index, query, candidate, distance, contributions)
        for index, (candidate, distance, contributions) in enumerate(ranked, start=1)
    ]
    write_fingerprints(args.output_fingerprints, fingerprints)
    write_matches(args.output_matches, match_rows)
    report = {
        "query": {
            "vesselId": query.vessel_id,
            "vesselName": query.vessel_name,
            "eventTimeUtc": query.event_time_utc,
            "sourceDistanceKm": query.source_distance_km,
        },
        "profileCount": len(fingerprints),
        "featureCount": len(stats),
        "matchesCsv": str(args.output_matches),
        "fingerprintsCsv": str(args.output_fingerprints),
        "matches": match_rows,
        "note": (
            "This ranks acoustic similarity between captured event windows. "
            "It is not unique vessel identification unless the same vessel has repeated clean reference profiles."
        ),
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Query: {query.vessel_name} ({query.vessel_id})")
    print(f"Profiles: {len(fingerprints)}; features: {len(stats)}")
    print("Top matches:")
    for row in match_rows[: min(10, len(match_rows))]:
        print(
            f"  {row['rank']:>2}. {row['candidate_name']} ({row['candidate_id']}) "
            f"score={row['similarity_score']:.2f} distance={row['feature_distance']:.3f}"
        )
    print(f"Wrote {args.output_matches}")
    print(f"Wrote {args.output_fingerprints}")
    print(f"Wrote {args.output_json}")


if __name__ == "__main__":
    main()
