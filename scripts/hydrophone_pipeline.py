#!/usr/bin/env python3
"""Hydrophone overnight processing pipeline.

This script is intentionally file-based and restartable:

1. ``one-recording`` checks one WAV file and its nearby AIS rows.
2. ``prepare-ais`` streams large AIS CSV files into a compact local subset.
3. ``process-audio`` extracts acoustic features from timestamped WAV files.
4. ``join-audio-ais`` attaches vessel context to each audio segment.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import sys
import wave
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_DEPS = REPO_ROOT / ".codex_pydeps"
if LOCAL_DEPS.exists():
    sys.path.insert(0, str(LOCAL_DEPS))

import numpy as np


HYDROPHONE_LATITUDE = 55.7645
HYDROPHONE_LONGITUDE = 12.7465
DEFAULT_RADIUS_KM = 15.0
DEFAULT_TIME_PADDING_MINUTES = 2.0
DEFAULT_AUDIO_BLOCK_SECONDS = 5.0
DEFAULT_BANDS = [(20.0, 100.0), (100.0, 500.0), (500.0, 2000.0), (2000.0, 10000.0)]
UTC = dt.timezone.utc

AIS_ALIASES = {
    "timestamp": ("timestamp", "bs_ts", "basedatetime", "datetime", "t"),
    "ship_id": ("ship_id", "mmsi"),
    "lat": ("lat", "latitude", "y"),
    "lon": ("lon", "long", "longitude", "x"),
    "name": ("name", "shipname", "vesselname"),
    "sog": ("sog",),
    "cog": ("cog",),
    "heading": ("heading",),
    "rot": ("rot",),
    "width": ("width",),
    "length": ("length",),
    "draught": ("draught", "draft"),
    "ship_type": ("ship type", "ship_type"),
    "type_of_mobile": ("type of mobile", "type_of_mobile"),
    "navigational_status": ("navigational status", "navigational_status"),
    "imo": ("imo",),
    "callsign": ("callsign", "call sign"),
    "destination": ("destination",),
    "eta": ("eta",),
    "data_source_type": ("data source type", "data_source_type"),
    "distance_km": ("distance km", "distance_km"),
    "bearing_deg": ("bearing deg", "bearing_deg"),
}

AIS_OUTPUT_COLUMNS = [
    "ship_id",
    "timestamp",
    "lat",
    "lon",
    "distance_km",
    "bearing_deg",
    "name",
    "sog",
    "cog",
    "heading",
    "rot",
    "width",
    "length",
    "draught",
    "ship_type",
    "type_of_mobile",
    "navigational_status",
    "imo",
    "callsign",
    "destination",
    "eta",
    "data_source_type",
]

AUDIO_FEATURE_COLUMNS = [
    "file_name",
    "path",
    "start_utc",
    "end_utc",
    "duration_seconds",
    "sample_rate_hz",
    "channels",
    "sample_width_bits",
    "frames",
    "rms_dbfs_ch1",
    "rms_dbfs_ch2",
    "rms_dbfs_mean",
    "peak_dbfs",
    "crest_factor_db",
    "stereo_correlation",
]


def iso_utc(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str | dt.datetime) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value.astimezone(UTC)
    text = str(value).strip()
    if not text:
        raise ValueError("empty datetime")

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        pass

    formats = [
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
    ]
    for fmt in formats:
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"unsupported datetime format: {value!r}")


def parse_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalize_column_name(value: object) -> str:
    return str(value).strip().lstrip("#").strip().lower().replace("_", " ")


def ais_column_map(columns: Iterable[str]) -> dict[str, str]:
    normalized = {normalize_column_name(column): column for column in columns}
    mapping: dict[str, str] = {}
    for canonical, aliases in AIS_ALIASES.items():
        for alias in aliases:
            normalized_alias = normalize_column_name(alias)
            if normalized_alias in normalized:
                mapping[canonical] = normalized[normalized_alias]
                break
    return mapping


def get_field(row: dict[str, str], mapping: dict[str, str], canonical: str) -> str:
    source = mapping.get(canonical)
    return row.get(source, "").strip() if source else ""


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


def parse_recording_start(path: Path) -> dt.datetime:
    parts = path.stem.split("_")
    if len(parts) < 3 or not parts[-2].isdigit() or not parts[-1].isdigit():
        raise ValueError(f"Cannot parse timestamp from recording name: {path.name}")
    seconds = int(parts[-2])
    fraction = int(parts[-1]) / (10 ** len(parts[-1]))
    return dt.datetime.fromtimestamp(seconds + fraction, tz=UTC)


def wav_info(path: Path) -> dict[str, float | int | dt.datetime]:
    with wave.open(str(path), "rb") as wav_file:
        frames = wav_file.getnframes()
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
    start = parse_recording_start(path)
    duration = frames / sample_rate
    return {
        "start": start,
        "end": start + dt.timedelta(seconds=duration),
        "duration_seconds": duration,
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "sample_width_bits": sample_width * 8,
        "frames": frames,
    }


def default_raw_ais_paths() -> list[Path]:
    paths = sorted(Path("Data/AIS").glob("aisdk-*/aisdk-*.csv"))
    return paths


def preferred_ais_paths() -> list[Path]:
    candidates = [
        Path("outputs/processing/ais_near_hydrophone.csv"),
        Path("Data/analysis_example/oresund_ais.csv"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return [candidate]
    raw = default_raw_ais_paths()
    if raw:
        return raw
    raise FileNotFoundError("No AIS CSV files found.")


def recording_paths(recordings_dir: Path) -> list[Path]:
    return sorted(recordings_dir.glob("*.wav"))


def audio_time_range(recordings_dir: Path) -> tuple[dt.datetime, dt.datetime]:
    paths = recording_paths(recordings_dir)
    if not paths:
        raise FileNotFoundError(f"No WAV files found in {recordings_dir}")
    first_info = wav_info(paths[0])
    last_info = wav_info(paths[-1])
    return first_info["start"], last_info["end"]  # type: ignore[return-value]


def iter_ais_rows(
    paths: Iterable[Path],
    *,
    hydrophone_latitude: float,
    hydrophone_longitude: float,
    radius_km: float,
    start_utc: dt.datetime | None = None,
    end_utc: dt.datetime | None = None,
    progress_every: int = 1_000_000,
    limit_input_rows: int | None = None,
) -> Iterable[dict[str, str]]:
    bbox_slack = 1.15
    lat_delta = (radius_km * bbox_slack) / 111.32
    lon_delta = (radius_km * bbox_slack) / (
        111.32 * math.cos(math.radians(hydrophone_latitude))
    )
    lat_min = hydrophone_latitude - lat_delta
    lat_max = hydrophone_latitude + lat_delta
    lon_min = hydrophone_longitude - lon_delta
    lon_max = hydrophone_longitude + lon_delta

    processed = 0
    for path in paths:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as ais_file:
            reader = csv.DictReader(ais_file)
            if not reader.fieldnames:
                continue
            mapping = ais_column_map(reader.fieldnames)
            for required in ("timestamp", "ship_id", "lat", "lon"):
                if required not in mapping:
                    raise ValueError(f"{path} is missing AIS column {required!r}")

            for row in reader:
                processed += 1
                if limit_input_rows is not None and processed > limit_input_rows:
                    return
                if progress_every and processed % progress_every == 0:
                    print(f"Read {processed:,} AIS rows...", file=sys.stderr)

                lat = parse_float(get_field(row, mapping, "lat"))
                lon = parse_float(get_field(row, mapping, "lon"))
                if lat is None or lon is None:
                    continue
                if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
                    continue

                try:
                    timestamp = parse_datetime(get_field(row, mapping, "timestamp"))
                except ValueError:
                    continue
                if start_utc and timestamp < start_utc:
                    continue
                if end_utc and timestamp > end_utc:
                    continue

                distance = haversine_km(hydrophone_latitude, hydrophone_longitude, lat, lon)
                if distance > radius_km:
                    continue

                output = {column: "" for column in AIS_OUTPUT_COLUMNS}
                output.update(
                    {
                        "ship_id": get_field(row, mapping, "ship_id"),
                        "timestamp": iso_utc(timestamp),
                        "lat": f"{lat:.6f}",
                        "lon": f"{lon:.6f}",
                        "distance_km": f"{distance:.4f}",
                        "bearing_deg": f"{bearing_deg(hydrophone_latitude, hydrophone_longitude, lat, lon):.2f}",
                    }
                )
                for column in AIS_OUTPUT_COLUMNS:
                    if column in output and output[column]:
                        continue
                    output[column] = get_field(row, mapping, column)
                yield output


def decode_pcm_bytes(data: bytes, channels: int, sample_width_bytes: int) -> np.ndarray:
    if not data:
        return np.empty((0, channels), dtype=np.float32)

    if sample_width_bytes == 3:
        raw = np.frombuffer(data, dtype=np.uint8)
        usable = (raw.size // 3) * 3
        raw = raw[:usable].reshape(-1, 3)
        values = (
            raw[:, 0].astype(np.int32)
            | (raw[:, 1].astype(np.int32) << 8)
            | (raw[:, 2].astype(np.int32) << 16)
        )
        values = (values ^ 0x800000) - 0x800000
        scale = float(2**23)
    elif sample_width_bytes == 2:
        values = np.frombuffer(data, dtype="<i2").astype(np.int32)
        scale = float(2**15)
    elif sample_width_bytes == 4:
        values = np.frombuffer(data, dtype="<i4")
        scale = float(2**31)
    elif sample_width_bytes == 1:
        values = np.frombuffer(data, dtype=np.uint8).astype(np.int16) - 128
        scale = float(2**7)
    else:
        raise ValueError(f"Unsupported sample width: {sample_width_bytes} bytes")

    frame_count = values.size // channels
    values = values[: frame_count * channels].reshape(frame_count, channels)
    return (values.astype(np.float32) / scale).clip(-1.0, 1.0)


def db_from_amplitude(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-12))


def db_from_power(value: float) -> float:
    return 10.0 * math.log10(max(value, 1e-24))


def band_column(low: float, high: float) -> str:
    return f"band_{int(low)}_{int(high)}_db"


def parse_bands(text: str | None) -> list[tuple[float, float]]:
    if not text:
        return DEFAULT_BANDS
    bands: list[tuple[float, float]] = []
    for part in text.split(","):
        low_text, high_text = part.split("-", maxsplit=1)
        low = float(low_text)
        high = float(high_text)
        if high <= low:
            raise ValueError(f"Invalid band: {part}")
        bands.append((low, high))
    return bands


def analyze_wav(
    path: Path,
    *,
    block_seconds: float = DEFAULT_AUDIO_BLOCK_SECONDS,
    bands: list[tuple[float, float]] | None = None,
) -> dict[str, str]:
    bands = bands or DEFAULT_BANDS
    info = wav_info(path)
    sample_rate = int(info["sample_rate_hz"])
    channels = int(info["channels"])
    sample_width_bytes = int(info["sample_width_bits"]) // 8
    block_frames = max(1, int(round(sample_rate * block_seconds)))

    sumsq = np.zeros(channels, dtype=np.float64)
    peak = 0.0
    total_frames = 0
    stereo_cross_sum = 0.0
    band_power_sum = {band: 0.0 for band in bands}
    band_block_count = {band: 0 for band in bands}
    mask_cache: dict[int, dict[tuple[float, float], np.ndarray]] = {}

    with wave.open(str(path), "rb") as wav_file:
        while True:
            data = wav_file.readframes(block_frames)
            if not data:
                break
            samples = decode_pcm_bytes(data, channels, sample_width_bytes)
            if samples.size == 0:
                continue
            total_frames += samples.shape[0]
            sumsq += np.sum(samples.astype(np.float64) ** 2, axis=0)
            peak = max(peak, float(np.max(np.abs(samples))))
            if channels >= 2:
                stereo_cross_sum += float(np.sum(samples[:, 0] * samples[:, 1]))

            mono = samples.mean(axis=1)
            if mono.size < 2:
                continue
            if mono.size not in mask_cache:
                freqs = np.fft.rfftfreq(mono.size, d=1.0 / sample_rate)
                mask_cache[mono.size] = {
                    band: (freqs >= band[0]) & (freqs < band[1]) for band in bands
                }
            window = np.hanning(mono.size).astype(np.float32)
            spectrum = np.fft.rfft(mono * window)
            power = (np.abs(spectrum) ** 2) / max(float(np.sum(window**2)), 1e-12)
            for band in bands:
                mask = mask_cache[mono.size][band]
                if np.any(mask):
                    band_power_sum[band] += float(np.mean(power[mask]))
                    band_block_count[band] += 1

    if total_frames == 0:
        raise ValueError(f"No audio frames read from {path}")

    rms = np.sqrt(sumsq / total_frames)
    rms_mean = float(np.mean(rms))
    peak_dbfs = db_from_amplitude(peak)
    rms_mean_dbfs = db_from_amplitude(rms_mean)
    result = {
        "file_name": path.name,
        "path": str(path),
        "start_utc": iso_utc(info["start"]),  # type: ignore[arg-type]
        "end_utc": iso_utc(info["end"]),  # type: ignore[arg-type]
        "duration_seconds": f"{float(info['duration_seconds']):.3f}",
        "sample_rate_hz": str(sample_rate),
        "channels": str(channels),
        "sample_width_bits": str(info["sample_width_bits"]),
        "frames": str(total_frames),
        "rms_dbfs_ch1": f"{db_from_amplitude(float(rms[0])):.3f}",
        "rms_dbfs_ch2": f"{db_from_amplitude(float(rms[1])):.3f}" if channels >= 2 else "",
        "rms_dbfs_mean": f"{rms_mean_dbfs:.3f}",
        "peak_dbfs": f"{peak_dbfs:.3f}",
        "crest_factor_db": f"{peak_dbfs - rms_mean_dbfs:.3f}",
        "stereo_correlation": "",
    }
    if channels >= 2 and sumsq[0] > 0 and sumsq[1] > 0:
        correlation = stereo_cross_sum / math.sqrt(float(sumsq[0] * sumsq[1]))
        result["stereo_correlation"] = f"{correlation:.6f}"
    for band in bands:
        count = band_block_count[band]
        average_power = band_power_sum[band] / count if count else 0.0
        result[band_column(*band)] = f"{db_from_power(average_power):.3f}"
    return result


def summarize_vessels(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row.get("ship_id", "")].append(row)

    summaries: list[dict[str, str]] = []
    for ship_id, ship_rows in grouped.items():
        closest = min(ship_rows, key=lambda r: parse_float(r.get("distance_km")) or float("inf"))
        sog_values = [parse_float(row.get("sog")) for row in ship_rows]
        length_values = [parse_float(row.get("length")) for row in ship_rows]
        draught_values = [parse_float(row.get("draught")) for row in ship_rows]
        valid_sog = [value for value in sog_values if value is not None]
        valid_length = [value for value in length_values if value is not None]
        valid_draught = [value for value in draught_values if value is not None]
        summaries.append(
            {
                "ship_id": ship_id,
                "name": closest.get("name", ""),
                "row_count": str(len(ship_rows)),
                "first_timestamp": min(row["timestamp"] for row in ship_rows),
                "last_timestamp": max(row["timestamp"] for row in ship_rows),
                "closest_timestamp": closest.get("timestamp", ""),
                "closest_distance_km": closest.get("distance_km", ""),
                "closest_bearing_deg": closest.get("bearing_deg", ""),
                "max_sog": f"{max(valid_sog):.2f}" if valid_sog else "",
                "mean_sog": f"{sum(valid_sog) / len(valid_sog):.2f}" if valid_sog else "",
                "max_length": f"{max(valid_length):.1f}" if valid_length else "",
                "max_draught": f"{max(valid_draught):.1f}" if valid_draught else "",
                "ship_type": closest.get("ship_type", ""),
            }
        )
    summaries.sort(key=lambda row: parse_float(row.get("closest_distance_km")) or float("inf"))
    return summaries


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def command_one_recording(args: argparse.Namespace) -> None:
    recording = args.recording
    if recording is None:
        paths = recording_paths(args.recordings_dir)
        if not paths:
            raise FileNotFoundError(f"No WAV files found in {args.recordings_dir}")
        recording = paths[0]

    bands = parse_bands(args.bands)
    audio = analyze_wav(recording, block_seconds=args.block_seconds, bands=bands)
    start = parse_datetime(audio["start_utc"]) - dt.timedelta(minutes=args.time_padding_minutes)
    end = parse_datetime(audio["end_utc"]) + dt.timedelta(minutes=args.time_padding_minutes)
    ais_paths = args.ais_csv or preferred_ais_paths()

    ais_rows = list(
        iter_ais_rows(
            ais_paths,
            hydrophone_latitude=args.hydrophone_latitude,
            hydrophone_longitude=args.hydrophone_longitude,
            radius_km=args.radius_km,
            start_utc=start,
            end_utc=end,
            progress_every=args.progress_every,
            limit_input_rows=args.limit_input_rows,
        )
    )
    vessel_summary = summarize_vessels(ais_rows)

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "nearby_ais_rows.csv", ais_rows, AIS_OUTPUT_COLUMNS)
    write_csv(
        output_dir / "nearby_vessel_summary.csv",
        vessel_summary,
        [
            "ship_id",
            "name",
            "row_count",
            "first_timestamp",
            "last_timestamp",
            "closest_timestamp",
            "closest_distance_km",
            "closest_bearing_deg",
            "max_sog",
            "mean_sog",
            "max_length",
            "max_draught",
            "ship_type",
        ],
    )

    closest = vessel_summary[0] if vessel_summary else None
    summary = {
        "recording": audio,
        "ais_sources": [str(path) for path in ais_paths],
        "ais_search_start_utc": iso_utc(start),
        "ais_search_end_utc": iso_utc(end),
        "radius_km": args.radius_km,
        "nearby_ais_rows": len(ais_rows),
        "nearby_vessel_count": len(vessel_summary),
        "closest_vessel": closest,
    }
    (output_dir / "recording_summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {output_dir / 'recording_summary.json'}")
    print(f"Wrote {output_dir / 'nearby_ais_rows.csv'}")
    print(f"Wrote {output_dir / 'nearby_vessel_summary.csv'}")
    print(
        f"Recording {recording.name}: {len(vessel_summary)} vessel(s), "
        f"{len(ais_rows)} AIS row(s)"
    )
    if closest:
        print(
            "Closest vessel: "
            f"{closest.get('ship_id')} {closest.get('name')} "
            f"at {closest.get('closest_distance_km')} km"
        )


def command_prepare_ais(args: argparse.Namespace) -> None:
    ais_paths = args.ais_csv or default_raw_ais_paths()
    if not ais_paths:
        raise FileNotFoundError("No raw AIS CSV files found under Data/AIS/aisdk-*")

    start_utc = parse_datetime(args.start_utc) if args.start_utc else None
    end_utc = parse_datetime(args.end_utc) if args.end_utc else None
    if start_utc is None or end_utc is None:
        audio_start, audio_end = audio_time_range(args.recordings_dir)
        margin = dt.timedelta(minutes=args.time_padding_minutes)
        start_utc = start_utc or (audio_start - margin)
        end_utc = end_utc or (audio_end + margin)

    if args.engine in {"auto", "duckdb"}:
        duckdb = try_import_duckdb()
        if duckdb is not None:
            try:
                kept = prepare_ais_with_duckdb(
                    duckdb=duckdb,
                    ais_paths=ais_paths,
                    output_csv=args.output_csv,
                    hydrophone_latitude=args.hydrophone_latitude,
                    hydrophone_longitude=args.hydrophone_longitude,
                    radius_km=args.radius_km,
                    start_utc=start_utc,
                    end_utc=end_utc,
                )
                print(f"Wrote {args.output_csv}")
                print(f"AIS rows kept: {kept:,}")
                print(f"Time window: {iso_utc(start_utc)} to {iso_utc(end_utc)}")
                print("AIS engine: duckdb")
                return
            except Exception as exc:
                if args.engine == "duckdb":
                    raise
                print(f"DuckDB AIS preparation failed; falling back to Python CSV reader: {exc}")
        elif args.engine == "duckdb":
            raise RuntimeError(
                "DuckDB was requested but could not be imported. "
                "Install it with: python -m pip install --target .codex_pydeps duckdb"
            )

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    kept = 0
    with args.output_csv.open("w", encoding="utf-8", newline="") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=AIS_OUTPUT_COLUMNS)
        writer.writeheader()
        for row in iter_ais_rows(
            ais_paths,
            hydrophone_latitude=args.hydrophone_latitude,
            hydrophone_longitude=args.hydrophone_longitude,
            radius_km=args.radius_km,
            start_utc=start_utc,
            end_utc=end_utc,
            progress_every=args.progress_every,
            limit_input_rows=args.limit_input_rows,
        ):
            writer.writerow(row)
            kept += 1
    print(f"Wrote {args.output_csv}")
    print(f"AIS rows kept: {kept:,}")
    print(f"Time window: {iso_utc(start_utc)} to {iso_utc(end_utc)}")
    print("AIS engine: python-csv")


def try_import_duckdb():
    try:
        import duckdb  # type: ignore[import-not-found]
    except Exception:
        return None
    return duckdb


def quote_sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def duckdb_csv_source(ais_paths: list[Path]) -> str:
    quoted_paths = ", ".join(quote_sql_string(str(path)) for path in ais_paths)
    return (
        f"read_csv([{quoted_paths}], header=true, all_varchar=true, "
        "union_by_name=true, ignore_errors=true, null_padding=true)"
    )


def prepare_ais_with_duckdb(
    *,
    duckdb,
    ais_paths: list[Path],
    output_csv: Path,
    hydrophone_latitude: float,
    hydrophone_longitude: float,
    radius_km: float,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
) -> int:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    connection.execute("PRAGMA threads=4")
    source = duckdb_csv_source(ais_paths)
    columns = [row[0] for row in connection.execute(f"DESCRIBE SELECT * FROM {source} LIMIT 0").fetchall()]
    mapping = ais_column_map(columns)
    for required in ("timestamp", "ship_id", "lat", "lon"):
        if required not in mapping:
            raise ValueError(f"AIS input is missing required column {required!r}")

    lat_col = quote_identifier(mapping["lat"])
    lon_col = quote_identifier(mapping["lon"])
    timestamp_col = quote_identifier(mapping["timestamp"])
    ship_id_col = quote_identifier(mapping["ship_id"])
    lat_delta = (radius_km * 1.15) / 111.32
    lon_delta = (radius_km * 1.15) / (
        111.32 * math.cos(math.radians(hydrophone_latitude))
    )
    lat_min = hydrophone_latitude - lat_delta
    lat_max = hydrophone_latitude + lat_delta
    lon_min = hydrophone_longitude - lon_delta
    lon_max = hydrophone_longitude + lon_delta
    start_text = start_utc.strftime("%Y-%m-%d %H:%M:%S.%f")
    end_text = end_utc.strftime("%Y-%m-%d %H:%M:%S.%f")

    selected_columns = []
    for output_column in AIS_OUTPUT_COLUMNS:
        if output_column in {"timestamp", "ship_id", "lat", "lon", "distance_km", "bearing_deg"}:
            continue
        source_column = mapping.get(output_column)
        if source_column:
            selected_columns.append(f"CAST({quote_identifier(source_column)} AS VARCHAR) AS {quote_identifier(output_column)}")
        else:
            selected_columns.append(f"'' AS {quote_identifier(output_column)}")

    selected_sql = ",\n            ".join(selected_columns)
    earth_radius = 6371.0
    query = f"""
    COPY (
        WITH source AS (
            SELECT
                *,
                TRY_CAST({lat_col} AS DOUBLE) AS lat_num,
                TRY_CAST({lon_col} AS DOUBLE) AS lon_num,
                COALESCE(
                    TRY_STRPTIME({timestamp_col}, '%d/%m/%Y %H:%M:%S.%f'),
                    TRY_STRPTIME({timestamp_col}, '%d/%m/%Y %H:%M:%S'),
                    TRY_CAST({timestamp_col} AS TIMESTAMP)
                ) AS ts
            FROM {source}
        ),
        boxed AS (
            SELECT *
            FROM source
            WHERE
                lat_num BETWEEN {lat_min:.12f} AND {lat_max:.12f}
                AND lon_num BETWEEN {lon_min:.12f} AND {lon_max:.12f}
                AND ts BETWEEN TIMESTAMP {quote_sql_string(start_text)}
                    AND TIMESTAMP {quote_sql_string(end_text)}
        ),
        measured AS (
            SELECT
                *,
                {earth_radius} * 2.0 * ASIN(SQRT(
                    POWER(SIN(RADIANS(lat_num - {hydrophone_latitude:.12f}) / 2.0), 2)
                    + COS(RADIANS({hydrophone_latitude:.12f}))
                    * COS(RADIANS(lat_num))
                    * POWER(SIN(RADIANS(lon_num - {hydrophone_longitude:.12f}) / 2.0), 2)
                )) AS distance_km_num,
                MOD(
                    DEGREES(ATAN2(
                        SIN(RADIANS(lon_num - {hydrophone_longitude:.12f})) * COS(RADIANS(lat_num)),
                        COS(RADIANS({hydrophone_latitude:.12f})) * SIN(RADIANS(lat_num))
                        - SIN(RADIANS({hydrophone_latitude:.12f})) * COS(RADIANS(lat_num))
                        * COS(RADIANS(lon_num - {hydrophone_longitude:.12f}))
                    )) + 360.0,
                    360.0
                ) AS bearing_deg_num
            FROM boxed
        )
        SELECT
            CAST({ship_id_col} AS VARCHAR) AS ship_id,
            STRFTIME(ts, '%Y-%m-%dT%H:%M:%SZ') AS timestamp,
            PRINTF('%.6f', lat_num) AS lat,
            PRINTF('%.6f', lon_num) AS lon,
            PRINTF('%.4f', distance_km_num) AS distance_km,
            PRINTF('%.2f', bearing_deg_num) AS bearing_deg,
            {selected_sql}
        FROM measured
        WHERE distance_km_num <= {radius_km:.12f}
        ORDER BY ts, ship_id
    )
    TO {quote_sql_string(str(output_csv))}
    WITH (HEADER, DELIMITER ',');
    """
    connection.execute(query)
    kept = connection.execute(f"SELECT COUNT(*) FROM read_csv_auto({quote_sql_string(str(output_csv))}, header=true)").fetchone()[0]
    connection.close()
    return int(kept)


def command_process_audio(args: argparse.Namespace) -> None:
    bands = parse_bands(args.bands)
    feature_columns = AUDIO_FEATURE_COLUMNS + [band_column(*band) for band in bands]
    paths = recording_paths(args.recordings_dir)
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        raise FileNotFoundError(f"No WAV files found in {args.recordings_dir}")

    processed_names: set[str] = set()
    append = args.resume and args.output_csv.exists()
    if append:
        with args.output_csv.open("r", encoding="utf-8", newline="") as existing_file:
            for row in csv.DictReader(existing_file):
                processed_names.add(row.get("file_name", ""))

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with args.output_csv.open(mode, encoding="utf-8", newline="") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=feature_columns, extrasaction="ignore")
        if not append:
            writer.writeheader()
        completed = 0
        skipped = 0
        for path in paths:
            if path.name in processed_names:
                skipped += 1
                continue
            features = analyze_wav(path, block_seconds=args.block_seconds, bands=bands)
            writer.writerow(features)
            out_file.flush()
            completed += 1
            if completed % args.progress_every_recordings == 0:
                print(f"Processed {completed:,} recording(s)...")

    print(f"Wrote {args.output_csv}")
    print(f"Processed recordings: {completed:,}; skipped existing: {skipped:,}")


def command_join_audio_ais(args: argparse.Namespace) -> None:
    with args.audio_features_csv.open("r", encoding="utf-8", newline="") as audio_file:
        audio_rows = list(csv.DictReader(audio_file))
    if not audio_rows:
        raise ValueError(f"No audio feature rows found in {args.audio_features_csv}")

    padding = dt.timedelta(minutes=args.time_padding_minutes)
    starts = [parse_datetime(row["start_utc"]) - padding for row in audio_rows]
    ends = [parse_datetime(row["end_utc"]) + padding for row in audio_rows]
    summaries = [
        {
            "ais_row_count": 0,
            "ship_ids": set(),
            "closest_distance_km": float("inf"),
            "closest_row": None,
            "max_sog": None,
            "max_length": None,
        }
        for _ in audio_rows
    ]

    with args.ais_csv.open("r", encoding="utf-8-sig", errors="replace", newline="") as ais_file:
        reader = csv.DictReader(ais_file)
        if not reader.fieldnames:
            raise ValueError(f"No AIS header found in {args.ais_csv}")
        mapping = ais_column_map(reader.fieldnames)
        for row in reader:
            try:
                timestamp = parse_datetime(get_field(row, mapping, "timestamp"))
            except ValueError:
                continue
            index = bisect_right(ends, timestamp)
            while index < len(audio_rows) and starts[index] <= timestamp:
                if timestamp <= ends[index]:
                    update_audio_ais_summary(summaries[index], row, mapping)
                index += 1

    fieldnames = list(audio_rows[0].keys()) + [
        "ais_row_count",
        "nearby_vessel_count",
        "closest_ship_id",
        "closest_name",
        "closest_timestamp",
        "closest_distance_km",
        "closest_bearing_deg",
        "closest_sog",
        "closest_cog",
        "closest_length",
        "closest_draught",
        "max_sog",
        "max_length",
    ]
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", encoding="utf-8", newline="") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for audio_row, summary in zip(audio_rows, summaries):
            output = dict(audio_row)
            closest = summary["closest_row"] or {}
            output.update(
                {
                    "ais_row_count": str(summary["ais_row_count"]),
                    "nearby_vessel_count": str(len(summary["ship_ids"])),
                    "closest_ship_id": get_field(closest, ais_column_map(closest.keys()), "ship_id")
                    if closest
                    else "",
                    "closest_name": get_field(closest, ais_column_map(closest.keys()), "name")
                    if closest
                    else "",
                    "closest_timestamp": get_field(closest, ais_column_map(closest.keys()), "timestamp")
                    if closest
                    else "",
                    "closest_distance_km": f"{summary['closest_distance_km']:.4f}"
                    if summary["closest_row"]
                    else "",
                    "closest_bearing_deg": get_field(closest, ais_column_map(closest.keys()), "bearing_deg")
                    if closest
                    else "",
                    "closest_sog": get_field(closest, ais_column_map(closest.keys()), "sog")
                    if closest
                    else "",
                    "closest_cog": get_field(closest, ais_column_map(closest.keys()), "cog")
                    if closest
                    else "",
                    "closest_length": get_field(closest, ais_column_map(closest.keys()), "length")
                    if closest
                    else "",
                    "closest_draught": get_field(closest, ais_column_map(closest.keys()), "draught")
                    if closest
                    else "",
                    "max_sog": f"{summary['max_sog']:.2f}" if summary["max_sog"] is not None else "",
                    "max_length": f"{summary['max_length']:.1f}"
                    if summary["max_length"] is not None
                    else "",
                }
            )
            writer.writerow(output)
    print(f"Wrote {args.output_csv}")


def update_audio_ais_summary(
    summary: dict[str, object],
    row: dict[str, str],
    mapping: dict[str, str],
) -> None:
    ship_id = get_field(row, mapping, "ship_id")
    summary["ais_row_count"] = int(summary["ais_row_count"]) + 1
    if ship_id:
        summary["ship_ids"].add(ship_id)  # type: ignore[union-attr]

    distance = parse_float(get_field(row, mapping, "distance_km"))
    if distance is None:
        lat = parse_float(get_field(row, mapping, "lat"))
        lon = parse_float(get_field(row, mapping, "lon"))
        if lat is not None and lon is not None:
            distance = haversine_km(HYDROPHONE_LATITUDE, HYDROPHONE_LONGITUDE, lat, lon)
    if distance is not None and distance < float(summary["closest_distance_km"]):
        summary["closest_distance_km"] = distance
        summary["closest_row"] = dict(row)

    sog = parse_float(get_field(row, mapping, "sog"))
    if sog is not None:
        summary["max_sog"] = max_optional_float(summary["max_sog"], sog)
    length = parse_float(get_field(row, mapping, "length"))
    if length is not None:
        summary["max_length"] = max_optional_float(summary["max_length"], length)


def max_optional_float(current: object, value: float) -> float:
    if current is None:
        return value
    return max(float(current), value)


def add_common_ais_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ais-csv", type=Path, action="append", help="AIS CSV path. May be repeated.")
    parser.add_argument("--radius-km", type=float, default=DEFAULT_RADIUS_KM)
    parser.add_argument("--hydrophone-latitude", type=float, default=HYDROPHONE_LATITUDE)
    parser.add_argument("--hydrophone-longitude", type=float, default=HYDROPHONE_LONGITUDE)
    parser.add_argument("--progress-every", type=int, default=1_000_000)
    parser.add_argument("--limit-input-rows", type=int, default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    one = subparsers.add_parser("one-recording", help="Analyze one WAV and nearby AIS rows.")
    one.add_argument("--recording", type=Path, default=None)
    one.add_argument("--recordings-dir", type=Path, default=Path("Data/Recordings"))
    one.add_argument("--output-dir", type=Path, default=Path("outputs/processing/single_recording"))
    one.add_argument("--time-padding-minutes", type=float, default=DEFAULT_TIME_PADDING_MINUTES)
    one.add_argument("--block-seconds", type=float, default=DEFAULT_AUDIO_BLOCK_SECONDS)
    one.add_argument("--bands", type=str, default=None)
    add_common_ais_args(one)
    one.set_defaults(func=command_one_recording)

    prepare = subparsers.add_parser("prepare-ais", help="Filter raw AIS CSV files near the hydrophone.")
    prepare.add_argument("--output-csv", type=Path, default=Path("outputs/processing/ais_near_hydrophone.csv"))
    prepare.add_argument("--recordings-dir", type=Path, default=Path("Data/Recordings"))
    prepare.add_argument("--start-utc", type=str, default=None)
    prepare.add_argument("--end-utc", type=str, default=None)
    prepare.add_argument("--time-padding-minutes", type=float, default=10.0)
    prepare.add_argument("--engine", choices=["auto", "duckdb", "python"], default="auto")
    add_common_ais_args(prepare)
    prepare.set_defaults(func=command_prepare_ais)

    process = subparsers.add_parser("process-audio", help="Extract acoustic features from WAV files.")
    process.add_argument("--recordings-dir", type=Path, default=Path("Data/Recordings"))
    process.add_argument("--output-csv", type=Path, default=Path("outputs/processing/audio_features.csv"))
    process.add_argument("--limit", type=int, default=None)
    process.add_argument("--resume", action="store_true")
    process.add_argument("--block-seconds", type=float, default=DEFAULT_AUDIO_BLOCK_SECONDS)
    process.add_argument("--bands", type=str, default=None)
    process.add_argument("--progress-every-recordings", type=int, default=25)
    process.set_defaults(func=command_process_audio)

    join = subparsers.add_parser("join-audio-ais", help="Join audio feature rows with nearby AIS context.")
    join.add_argument("--audio-features-csv", type=Path, default=Path("outputs/processing/audio_features.csv"))
    join.add_argument("--ais-csv", type=Path, default=Path("outputs/processing/ais_near_hydrophone.csv"))
    join.add_argument("--output-csv", type=Path, default=Path("outputs/processing/audio_ais_events.csv"))
    join.add_argument("--time-padding-minutes", type=float, default=DEFAULT_TIME_PADDING_MINUTES)
    join.set_defaults(func=command_join_audio_ais)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
