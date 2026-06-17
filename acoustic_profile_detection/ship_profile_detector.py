#!/usr/bin/env python3
"""Acoustic template detector for one vessel.

AIS is used only to select/build a reference template. The scan subcommand reads
only WAV files and the saved template.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import html
import json
import math
import os
import re
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


UTC = dt.timezone.utc
HYDROPHONE_LAT = 55.7645
HYDROPHONE_LON = 12.7465
SOUND_SPEED_M_S = 1445.0
DEFAULT_BINS_HZ = [
    20,
    31.5,
    50,
    80,
    125,
    200,
    315,
    500,
    800,
    1250,
    2000,
    3150,
    5000,
    8000,
    12000,
]


@dataclass(frozen=True)
class RecordingInfo:
    path: Path
    start: dt.datetime
    end: dt.datetime
    sample_rate: int
    channels: int
    sample_width: int
    frames: int


@dataclass(frozen=True)
class AisPoint:
    timestamp: dt.datetime
    ship_id: str
    name: str
    lat: float
    lon: float
    distance_km: float
    bearing_deg: float
    sog: float | None
    cog: float | None
    ship_type: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect", help="Summarize recordings without processing audio.")
    inspect.add_argument("--recordings-dir", type=Path, default=Path("Data/Recordings"))

    candidates = subparsers.add_parser("ais-candidates", help="List AIS target candidates near the hydrophone.")
    candidates.add_argument("--ais-csv", type=Path, required=True)
    candidates.add_argument("--target", default="FRENCH WARSHIP")
    candidates.add_argument("--target-mmsi", default="")
    candidates.add_argument("--max-distance-km", type=float, default=8.0)
    candidates.add_argument("--output-csv", type=Path, default=Path("outputs/acoustic_profile_detection/ais_candidates.csv"))
    candidates.add_argument("--max-rows", type=int, default=0, help="Debug cap; 0 means all rows.")

    build = subparsers.add_parser("build-template", help="Build one acoustic template from target AIS and audio.")
    build.add_argument("--ais-csv", type=Path, required=True)
    build.add_argument("--target", default="FRENCH WARSHIP")
    build.add_argument("--target-mmsi", default="")
    build.add_argument("--recordings-dir", type=Path, default=Path("Data/Recordings"))
    build.add_argument("--output-template", type=Path, default=Path("outputs/acoustic_profile_detection/template.json"))
    build.add_argument("--output-windows", type=Path, default=Path("outputs/acoustic_profile_detection/template_windows.csv"))
    build.add_argument("--max-distance-km", type=float, default=5.0)
    build.add_argument("--window-seconds", type=float, default=90.0)
    build.add_argument("--step-seconds", type=float, default=10.0)
    build.add_argument("--sample-seconds", type=float, default=4.0)
    build.add_argument("--max-ais-rows", type=int, default=0)

    scan = subparsers.add_parser("scan", help="Scan WAV files using only the saved acoustic template.")
    scan.add_argument("--template", type=Path, required=True)
    scan.add_argument("--recordings-dir", type=Path, default=Path("Data/Recordings"))
    scan.add_argument("--output-csv", type=Path, default=Path("outputs/acoustic_profile_detection/detections.csv"))
    scan.add_argument("--threshold", type=float, default=72.0)
    scan.add_argument("--window-seconds", type=float, default=20.0)
    scan.add_argument("--hop-seconds", type=float, default=10.0)
    scan.add_argument("--sample-seconds", type=float, default=4.0)
    scan.add_argument("--from-utc", default="", help="Optional scan start time, e.g. 2026-06-10T11:50:00Z.")
    scan.add_argument("--to-utc", default="", help="Optional scan end time, e.g. 2026-06-10T12:05:00Z.")
    scan.add_argument("--max-files", type=int, default=0, help="Smoke-test cap; 0 means all files.")
    scan.add_argument("--keep-all", action="store_true", help="Write every scored window, not only detections.")
    scan.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Parallel WAV workers. 0 uses most CPU cores; 1 keeps the old single-process behavior.",
    )
    scan.add_argument("--progress-every", type=int, default=10, help="Print progress every N completed files.")

    view = subparsers.add_parser("view", help="Build a standalone HTML report.")
    view.add_argument("--template", type=Path, required=True)
    view.add_argument("--detections", type=Path, required=True)
    view.add_argument("--output-html", type=Path, default=Path("outputs/acoustic_profile_detection/report.html"))
    view.add_argument("--top", type=int, default=250)

    return parser.parse_args()


def parse_datetime(value: object) -> dt.datetime:
    text = str(value).strip()
    if not text:
        raise ValueError("empty datetime")
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None  # type: ignore[assignment]
        if parsed is None:
            raise
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_float(value: object) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_column(value: str) -> str:
    return value.strip().lstrip("#").strip().lower().replace("_", " ")


def column_map(columns: Iterable[str]) -> dict[str, str]:
    aliases = {
        "timestamp": ("timestamp", "bs ts", "basedatetime"),
        "ship_id": ("ship id", "mmsi"),
        "lat": ("lat", "latitude"),
        "lon": ("lon", "longitude", "long"),
        "distance_km": ("distance km", "distance_km"),
        "bearing_deg": ("bearing deg", "bearing_deg"),
        "name": ("name", "shipname", "vesselname"),
        "sog": ("sog",),
        "cog": ("cog",),
        "ship_type": ("ship type", "ship_type"),
    }
    normalized = {normalize_column(column): column for column in columns}
    mapping: dict[str, str] = {}
    for canonical, names in aliases.items():
        for name in names:
            if name in normalized:
                mapping[canonical] = normalized[name]
                break
    return mapping


def get(row: dict[str, str], mapping: dict[str, str], name: str) -> str:
    source = mapping.get(name)
    return row.get(source, "").strip() if source else ""


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def parse_recording_start(path: Path) -> dt.datetime:
    parts = path.stem.split("_")
    if len(parts) < 3:
        raise ValueError(f"Cannot parse timestamp from {path.name}")
    seconds = int(parts[-2])
    fraction = int(parts[-1]) / (10 ** len(parts[-1]))
    return dt.datetime.fromtimestamp(seconds + fraction, tz=UTC)


def recording_index(recordings_dir: Path) -> list[RecordingInfo]:
    recordings: list[RecordingInfo] = []
    for path in sorted(recordings_dir.glob("*.wav")):
        try:
            start = parse_recording_start(path)
        except ValueError:
            continue
        with wave.open(str(path), "rb") as wav_file:
            frames = wav_file.getnframes()
            sample_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
        recordings.append(
            RecordingInfo(
                path=path,
                start=start,
                end=start + dt.timedelta(seconds=frames / sample_rate),
                sample_rate=sample_rate,
                channels=channels,
                sample_width=sample_width,
                frames=frames,
            )
        )
    if not recordings:
        raise SystemExit(f"No timestamped WAV files found in {recordings_dir}")
    return recordings


def decode_pcm(data: bytes, channels: int, sample_width: int) -> np.ndarray:
    if sample_width == 2:
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3)
        values = raw[:, 0].astype(np.int32) | (raw[:, 1].astype(np.int32) << 8) | (raw[:, 2].astype(np.int32) << 16)
        values = np.where(values & 0x800000, values - 0x1000000, values)
        samples = values.astype(np.float32) / 8388608.0
    elif sample_width == 4:
        samples = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported sample width: {sample_width}")
    if channels > 1:
        samples = samples.reshape(-1, channels)
    return samples


def read_recording(info: RecordingInfo) -> np.ndarray:
    with wave.open(str(info.path), "rb") as wav_file:
        data = wav_file.readframes(info.frames)
    return decode_pcm(data, info.channels, info.sample_width)


def mono(samples: np.ndarray) -> np.ndarray:
    if samples.ndim == 1:
        return samples.astype(np.float32)
    return samples.mean(axis=1).astype(np.float32)


def db(value: float) -> float:
    return 20.0 * math.log10(max(abs(value), 1e-12))


def safe_corr(a: np.ndarray, b: np.ndarray) -> float:
    aa = a.astype(np.float64) - float(np.mean(a))
    bb = b.astype(np.float64) - float(np.mean(b))
    denom = math.sqrt(float(np.sum(aa * aa) * np.sum(bb * bb)))
    return float(np.sum(aa * bb) / denom) if denom > 1e-12 else 0.0


def gcc_delay_seconds(samples: np.ndarray, sample_rate: int, max_delay_s: float = 0.001) -> float:
    if samples.ndim < 2 or samples.shape[1] < 2:
        return 0.0
    left = samples[:, 0].astype(np.float64) - float(np.mean(samples[:, 0]))
    right = samples[:, 1].astype(np.float64) - float(np.mean(samples[:, 1]))
    if not np.any(left) or not np.any(right):
        return 0.0
    n_fft = 1 << max(1, (left.size * 2 - 1).bit_length())
    cross = np.fft.rfft(left, n_fft) * np.conj(np.fft.rfft(right, n_fft))
    cross /= np.maximum(np.abs(cross), 1e-12)
    corr = np.fft.irfft(cross, n_fft)
    max_shift = max(1, int(round(max_delay_s * sample_rate)))
    centered = np.concatenate((corr[-max_shift:], corr[: max_shift + 1]))
    lag = int(np.argmax(np.abs(centered))) - max_shift
    return lag / sample_rate


def feature_vector(samples: np.ndarray, sample_rate: int, bins_hz: list[float]) -> dict[str, object]:
    x = mono(samples)
    if x.size < sample_rate // 4:
        raise ValueError("audio window is too short")

    window = np.hanning(x.size).astype(np.float32)
    spectrum = np.fft.rfft(x * window)
    power = (np.abs(spectrum) ** 2) / max(float(np.sum(window**2)), 1e-12)
    freqs = np.fft.rfftfreq(x.size, 1.0 / sample_rate)

    band_db: list[float] = []
    for lo, hi in zip(bins_hz[:-1], bins_hz[1:]):
        mask = (freqs >= lo) & (freqs < hi)
        value = float(np.mean(power[mask])) if np.any(mask) else 0.0
        band_db.append(10.0 * math.log10(max(value, 1e-18)))

    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    blocks = np.array_split(x, 16)
    env = [db(float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))) for block in blocks if block.size]
    env_mean = float(np.mean(env)) if env else -240.0
    env_std = float(np.std(env)) if len(env) > 1 else 0.0
    env_shape = [(value - env_mean) / max(env_std, 1e-6) for value in env]

    return {
        "band_db": band_db,
        "band_shape": (np.array(band_db) - float(np.mean(band_db))).tolist(),
        "rms_dbfs": db(rms),
        "peak_dbfs": db(peak),
        "crest_db": db(peak) - db(rms),
        "envelope_shape": env_shape,
        "envelope_std_db": env_std,
        "stereo_corr": safe_corr(samples[:, 0], samples[:, 1]) if samples.ndim == 2 and samples.shape[1] >= 2 else 0.0,
        "tdoa_ms": gcc_delay_seconds(samples, sample_rate) * 1000.0,
    }


def iter_ais_points(path: Path, target: str, target_mmsi: str, max_rows: int = 0) -> Iterable[AisPoint]:
    target_text = target.strip().lower()
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames:
            return
        mapping = column_map(reader.fieldnames)
        for index, row in enumerate(reader, start=1):
            if max_rows and index > max_rows:
                break
            ship_id = get(row, mapping, "ship_id")
            name = get(row, mapping, "name")
            ship_type = get(row, mapping, "ship_type")
            searchable = f"{ship_id} {name} {ship_type}".lower()
            if target_mmsi and ship_id != target_mmsi:
                continue
            if target_text and target_text not in searchable:
                continue
            lat = parse_float(get(row, mapping, "lat"))
            lon = parse_float(get(row, mapping, "lon"))
            if lat is None or lon is None:
                continue
            distance = parse_float(get(row, mapping, "distance_km"))
            if distance is None:
                distance = haversine_km(HYDROPHONE_LAT, HYDROPHONE_LON, lat, lon)
            bearing = parse_float(get(row, mapping, "bearing_deg"))
            if bearing is None:
                bearing = bearing_deg(HYDROPHONE_LAT, HYDROPHONE_LON, lat, lon)
            try:
                timestamp = parse_datetime(get(row, mapping, "timestamp"))
            except ValueError:
                continue
            yield AisPoint(
                timestamp=timestamp,
                ship_id=ship_id,
                name=name or ship_id,
                lat=lat,
                lon=lon,
                distance_km=distance,
                bearing_deg=bearing,
                sog=parse_float(get(row, mapping, "sog")),
                cog=parse_float(get(row, mapping, "cog")),
                ship_type=ship_type,
            )


def extract_window(info: RecordingInfo, samples: np.ndarray, start: dt.datetime, seconds: float) -> np.ndarray | None:
    offset = (start - info.start).total_seconds()
    first = int(round(offset * info.sample_rate))
    frames = int(round(seconds * info.sample_rate))
    last = first + frames
    if first < 0 or last > info.frames or frames <= 0:
        return None
    return samples[first:last]


def choose_template_point(points: list[AisPoint], recordings: list[RecordingInfo], window_seconds: float) -> AisPoint:
    audio_start = recordings[0].start
    audio_end = recordings[-1].end
    half = dt.timedelta(seconds=window_seconds / 2)
    candidates = []
    for point in points:
        arrival = point.timestamp + dt.timedelta(seconds=(point.distance_km * 1000.0) / SOUND_SPEED_M_S)
        if arrival - half >= audio_start and arrival + half <= audio_end:
            moving_bonus = -0.2 if (point.sog or 0.0) > 1.0 else 0.0
            candidates.append((point.distance_km + moving_bonus, point))
    if not candidates:
        raise SystemExit("No target AIS points overlap the recording coverage.")
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def median_list(values: list[list[float]]) -> list[float]:
    return np.median(np.array(values, dtype=float), axis=0).tolist()


def compare_feature(template: dict[str, object], feature: dict[str, object]) -> dict[str, float]:
    t_band = np.array(template["band_shape"], dtype=float)
    f_band = np.array(feature["band_shape"], dtype=float)
    band_delta = float(np.sqrt(np.mean((t_band - f_band) ** 2)))
    band_score = max(0.0, 100.0 - 8.0 * band_delta)

    t_env = np.array(template["envelope_shape"], dtype=float)
    f_env = np.array(feature["envelope_shape"], dtype=float)
    n = min(t_env.size, f_env.size)
    if n:
        env_delta = float(np.sqrt(np.mean((t_env[:n] - f_env[:n]) ** 2)))
        env_score = max(0.0, 100.0 - 20.0 * env_delta)
    else:
        env_score = 0.0

    rms_delta = abs(float(template["rms_dbfs"]) - float(feature["rms_dbfs"]))
    loudness_score = max(0.0, 100.0 - 2.0 * rms_delta)
    corr_delta = abs(float(template["stereo_corr"]) - float(feature["stereo_corr"]))
    stereo_score = max(0.0, 100.0 - 100.0 * corr_delta)
    tdoa_delta = abs(float(template["tdoa_ms"]) - float(feature["tdoa_ms"]))
    tdoa_score = max(0.0, 100.0 - 80.0 * tdoa_delta)
    score = 0.50 * band_score + 0.20 * env_score + 0.15 * loudness_score + 0.10 * stereo_score + 0.05 * tdoa_score
    return {
        "score": score,
        "band_score": band_score,
        "envelope_score": env_score,
        "loudness_score": loudness_score,
        "stereo_score": stereo_score,
        "tdoa_score": tdoa_score,
        "distance_proxy": 10 ** ((float(template["rms_dbfs"]) - float(feature["rms_dbfs"])) / 20.0),
    }


def detection_row(
    *,
    detected: bool,
    scores: dict[str, float],
    feat: dict[str, object],
    start: dt.datetime,
    end: dt.datetime,
    file_name: str,
) -> dict[str, str]:
    return {
        "detected": str(detected).lower(),
        "score": f"{scores['score']:.3f}",
        "start_utc": iso(start),
        "end_utc": iso(end),
        "file": file_name,
        "rms_dbfs": f"{float(feat['rms_dbfs']):.3f}",
        "peak_dbfs": f"{float(feat['peak_dbfs']):.3f}",
        "band_score": f"{scores['band_score']:.3f}",
        "envelope_score": f"{scores['envelope_score']:.3f}",
        "loudness_score": f"{scores['loudness_score']:.3f}",
        "stereo_score": f"{scores['stereo_score']:.3f}",
        "tdoa_score": f"{scores['tdoa_score']:.3f}",
        "distance_proxy": f"{scores['distance_proxy']:.3f}",
        "stereo_corr": f"{float(feat['stereo_corr']):.6f}",
        "tdoa_ms": f"{float(feat['tdoa_ms']):.4f}",
    }


def scan_one_recording(
    task: tuple[
        RecordingInfo,
        dict[str, object],
        list[float],
        float,
        float,
        float,
        float,
        dt.datetime | None,
        dt.datetime | None,
        bool,
    ],
) -> tuple[str, int, int, list[dict[str, str]]]:
    (
        info,
        template_feature,
        bins_hz,
        threshold,
        window_seconds,
        hop_seconds,
        sample_seconds,
        from_utc,
        to_utc,
        keep_all,
    ) = task
    rows: list[dict[str, str]] = []
    scored = 0
    detections = 0
    samples = read_recording(info)
    cursor = max(info.start, from_utc or info.start)
    file_scan_end = min(info.end, to_utc or info.end)
    while cursor + dt.timedelta(seconds=window_seconds) <= file_scan_end:
        chunk_start = cursor + dt.timedelta(seconds=max(0.0, (window_seconds - sample_seconds) / 2))
        chunk = extract_window(info, samples, chunk_start, sample_seconds)
        if chunk is not None:
            feat = feature_vector(chunk, info.sample_rate, bins_hz)
            scores = compare_feature(template_feature, feat)
            detected = scores["score"] >= threshold
            scored += 1
            detections += int(detected)
            if detected or keep_all:
                rows.append(
                    detection_row(
                        detected=detected,
                        scores=scores,
                        feat=feat,
                        start=cursor,
                        end=cursor + dt.timedelta(seconds=window_seconds),
                        file_name=info.path.name,
                    )
                )
        cursor += dt.timedelta(seconds=hop_seconds)
    return info.path.name, scored, detections, rows


def command_inspect(args: argparse.Namespace) -> None:
    recordings = recording_index(args.recordings_dir)
    first = recordings[0]
    last = recordings[-1]
    print(f"Recordings: {len(recordings)}")
    print(f"Coverage UTC: {iso(first.start)} to {iso(last.end)}")
    print(f"Format: {first.sample_rate} Hz, {first.channels} channels, {first.sample_width * 8}-bit")
    print(f"Typical file duration: {(first.end - first.start).total_seconds():.3f} s")


def command_ais_candidates(args: argparse.Namespace) -> None:
    points = [p for p in iter_ais_points(args.ais_csv, args.target, args.target_mmsi, args.max_rows) if p.distance_km <= args.max_distance_km]
    points.sort(key=lambda point: (point.distance_km, point.timestamp))
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=["timestamp_utc", "mmsi", "name", "distance_km", "bearing_deg", "sog", "cog", "ship_type", "lat", "lon"])
        writer.writeheader()
        for point in points:
            writer.writerow({
                "timestamp_utc": iso(point.timestamp),
                "mmsi": point.ship_id,
                "name": point.name,
                "distance_km": f"{point.distance_km:.4f}",
                "bearing_deg": f"{point.bearing_deg:.2f}",
                "sog": "" if point.sog is None else f"{point.sog:.2f}",
                "cog": "" if point.cog is None else f"{point.cog:.2f}",
                "ship_type": point.ship_type,
                "lat": point.lat,
                "lon": point.lon,
            })
    print(f"Candidates: {len(points)}")
    if points:
        best = points[0]
        print(f"Closest: {best.name} {best.ship_id} at {iso(best.timestamp)}, {best.distance_km:.3f} km")
    print(f"Wrote {args.output_csv}")


def command_build_template(args: argparse.Namespace) -> None:
    recordings = recording_index(args.recordings_dir)
    points = [p for p in iter_ais_points(args.ais_csv, args.target, args.target_mmsi, args.max_ais_rows) if p.distance_km <= args.max_distance_km]
    if not points:
        raise SystemExit("No AIS points matched the target and max distance.")
    point = choose_template_point(points, recordings, args.window_seconds)
    arrival = point.timestamp + dt.timedelta(seconds=(point.distance_km * 1000.0) / SOUND_SPEED_M_S)
    start = arrival - dt.timedelta(seconds=args.window_seconds / 2)
    end = arrival + dt.timedelta(seconds=args.window_seconds / 2)

    windows: list[dict[str, object]] = []
    for info in recordings:
        if info.end <= start or info.start >= end:
            continue
        samples = read_recording(info)
        cursor = max(start, info.start)
        while cursor + dt.timedelta(seconds=args.sample_seconds) <= min(end, info.end):
            chunk = extract_window(info, samples, cursor, args.sample_seconds)
            if chunk is not None:
                feat = feature_vector(chunk, info.sample_rate, DEFAULT_BINS_HZ)
                windows.append({"file": info.path.name, "start_utc": iso(cursor), **feat})
            cursor += dt.timedelta(seconds=args.step_seconds)

    if not windows:
        raise SystemExit("No audio feature windows could be extracted for the target.")
    band_shapes = [row["band_shape"] for row in windows]  # type: ignore[list-item]
    env_shapes = [row["envelope_shape"] for row in windows]  # type: ignore[list-item]
    template_feature = {
        "band_shape": median_list(band_shapes),
        "band_db": median_list([row["band_db"] for row in windows]),  # type: ignore[list-item]
        "envelope_shape": median_list(env_shapes),
        "rms_dbfs": float(np.median([row["rms_dbfs"] for row in windows])),
        "peak_dbfs": float(np.median([row["peak_dbfs"] for row in windows])),
        "crest_db": float(np.median([row["crest_db"] for row in windows])),
        "envelope_std_db": float(np.median([row["envelope_std_db"] for row in windows])),
        "stereo_corr": float(np.median([row["stereo_corr"] for row in windows])),
        "tdoa_ms": float(np.median([row["tdoa_ms"] for row in windows])),
    }
    template = {
        "created_utc": iso(dt.datetime.now(tz=UTC)),
        "target": args.target,
        "target_mmsi": point.ship_id,
        "target_name": point.name,
        "reference_ais_time_utc": iso(point.timestamp),
        "reference_arrival_time_utc": iso(arrival),
        "reference_distance_km": point.distance_km,
        "reference_bearing_deg": point.bearing_deg,
        "reference_sog": point.sog,
        "reference_cog": point.cog,
        "window_start_utc": iso(start),
        "window_end_utc": iso(end),
        "bins_hz": DEFAULT_BINS_HZ,
        "feature": template_feature,
        "reference_window_count": len(windows),
        "note": "AIS was used only to build this template. Scanning does not use AIS.",
    }
    args.output_template.parent.mkdir(parents=True, exist_ok=True)
    args.output_template.write_text(json.dumps(template, indent=2), encoding="utf-8")

    args.output_windows.parent.mkdir(parents=True, exist_ok=True)
    with args.output_windows.open("w", encoding="utf-8", newline="") as csv_file:
        columns = ["file", "start_utc", "rms_dbfs", "peak_dbfs", "crest_db", "stereo_corr", "tdoa_ms"]
        writer = csv.DictWriter(csv_file, fieldnames=columns)
        writer.writeheader()
        for row in windows:
            writer.writerow({key: row[key] for key in columns})
    print(f"Template target: {point.name} ({point.ship_id})")
    print(f"Reference arrival: {iso(arrival)}; distance {point.distance_km:.3f} km")
    print(f"Reference audio windows: {len(windows)}")
    print(f"Wrote {args.output_template}")
    print(f"Wrote {args.output_windows}")


def command_scan(args: argparse.Namespace) -> None:
    template = json.loads(args.template.read_text(encoding="utf-8"))
    bins_hz = [float(value) for value in template["bins_hz"]]
    feature = template["feature"]
    recordings = recording_index(args.recordings_dir)
    from_utc = parse_datetime(args.from_utc) if args.from_utc else None
    to_utc = parse_datetime(args.to_utc) if args.to_utc else None
    if from_utc or to_utc:
        recordings = [
            info
            for info in recordings
            if (to_utc is None or info.start < to_utc) and (from_utc is None or info.end > from_utc)
        ]
    if args.max_files:
        recordings = recordings[: args.max_files]
    cpu_count = os.cpu_count() or 1
    workers = args.workers if args.workers > 0 else max(1, cpu_count - 1)
    workers = min(workers, max(1, len(recordings)))

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "detected",
        "score",
        "start_utc",
        "end_utc",
        "file",
        "rms_dbfs",
        "peak_dbfs",
        "band_score",
        "envelope_score",
        "loudness_score",
        "stereo_score",
        "tdoa_score",
        "distance_proxy",
        "stereo_corr",
        "tdoa_ms",
    ]
    scored = 0
    detections = 0
    with args.output_csv.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        tasks = [
            (
                info,
                feature,
                bins_hz,
                args.threshold,
                args.window_seconds,
                args.hop_seconds,
                args.sample_seconds,
                from_utc,
                to_utc,
                args.keep_all,
            )
            for info in recordings
        ]
        print(f"Scanning {len(recordings)} files with {workers} worker(s)...")
        if workers == 1:
            for index, task in enumerate(tasks, start=1):
                _file_name, file_scored, file_detections, rows = scan_one_recording(task)
                scored += file_scored
                detections += file_detections
                writer.writerows(rows)
                if index % max(1, args.progress_every) == 0 or index == len(tasks):
                    print(f"Scanned {index}/{len(recordings)} files; detections={detections}")
        else:
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(scan_one_recording, task) for task in tasks]
                for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                    _file_name, file_scored, file_detections, rows = future.result()
                    scored += file_scored
                    detections += file_detections
                    writer.writerows(rows)
                    if index % max(1, args.progress_every) == 0 or index == len(futures):
                        print(f"Scanned {index}/{len(recordings)} files; detections={detections}")
    print(f"Scored windows: {scored}; detections >= {args.threshold}: {detections}")
    print(f"Wrote {args.output_csv}")


def read_detection_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def command_view(args: argparse.Namespace) -> None:
    template = json.loads(args.template.read_text(encoding="utf-8"))
    rows = read_detection_rows(args.detections)
    rows.sort(key=lambda row: parse_float(row.get("score")) or -1.0, reverse=True)
    top_rows = rows[: args.top]
    scores = [parse_float(row.get("score")) or 0.0 for row in rows]
    band_shape = template["feature"]["band_shape"]
    max_score = max(scores) if scores else 0.0
    median_score = float(np.median(scores)) if scores else 0.0

    table_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(row.get('start_utc', ''))}</td>"
        f"<td>{html.escape(row.get('file', ''))}</td>"
        f"<td>{html.escape(row.get('score', ''))}</td>"
        f"<td>{html.escape(row.get('rms_dbfs', ''))}</td>"
        f"<td>{html.escape(row.get('distance_proxy', ''))}</td>"
        f"<td>{html.escape(row.get('band_score', ''))}</td>"
        f"<td>{html.escape(row.get('envelope_score', ''))}</td>"
        "</tr>"
        for row in top_rows
    )
    bars = "\n".join(
        f'<div class="bar" style="height:{max(2, min(160, 80 + value * 4)):.1f}px"></div>'
        for value in band_shape
    )
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Acoustic Ship Profile Detection</title>
  <style>
    body {{ margin: 0; font-family: Segoe UI, Arial, sans-serif; background: #f6f7f9; color: #1d2430; }}
    header {{ padding: 24px 32px; background: #0f1f2e; color: white; }}
    main {{ padding: 24px 32px; max-width: 1280px; margin: auto; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(160px, 1fr)); gap: 12px; margin-bottom: 20px; }}
    .card {{ background: white; border: 1px solid #d8dde5; border-radius: 8px; padding: 14px; }}
    .label {{ font-size: 12px; color: #667085; text-transform: uppercase; letter-spacing: .04em; }}
    .value {{ font-size: 22px; font-weight: 650; margin-top: 6px; }}
    .bars {{ display: flex; align-items: end; gap: 4px; height: 190px; padding: 12px; background: white; border: 1px solid #d8dde5; border-radius: 8px; }}
    .bar {{ width: 100%; background: #2f6f73; border-radius: 3px 3px 0 0; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #d8dde5; border-radius: 8px; overflow: hidden; }}
    th, td {{ text-align: left; padding: 9px 10px; border-bottom: 1px solid #edf0f4; font-size: 13px; }}
    th {{ background: #eef2f5; font-size: 12px; text-transform: uppercase; color: #536171; }}
    tr:last-child td {{ border-bottom: 0; }}
  </style>
</head>
<body>
  <header>
    <h1>Acoustic Ship Profile Detection</h1>
    <div>{html.escape(template.get("target_name", ""))} ({html.escape(template.get("target_mmsi", ""))}) reference: {html.escape(template.get("reference_arrival_time_utc", ""))}</div>
  </header>
  <main>
    <section class="grid">
      <div class="card"><div class="label">Rows</div><div class="value">{len(rows)}</div></div>
      <div class="card"><div class="label">Max Score</div><div class="value">{max_score:.1f}</div></div>
      <div class="card"><div class="label">Median Score</div><div class="value">{median_score:.1f}</div></div>
      <div class="card"><div class="label">Reference Distance</div><div class="value">{float(template.get("reference_distance_km", 0.0)):.2f} km</div></div>
    </section>
    <section>
      <h2>Template Spectral Shape</h2>
      <div class="bars">{bars}</div>
    </section>
    <section>
      <h2>Top Candidate Windows</h2>
      <table>
        <thead><tr><th>Start UTC</th><th>File</th><th>Score</th><th>RMS dBFS</th><th>Distance Proxy</th><th>Band</th><th>Envelope</th></tr></thead>
        <tbody>{table_rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>
"""
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.write_text(html_text, encoding="utf-8")
    print(f"Wrote {args.output_html}")


def main() -> None:
    args = parse_args()
    if args.command == "inspect":
        command_inspect(args)
    elif args.command == "ais-candidates":
        command_ais_candidates(args)
    elif args.command == "build-template":
        command_build_template(args)
    elif args.command == "scan":
        command_scan(args)
    elif args.command == "view":
        command_view(args)
    else:
        raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
