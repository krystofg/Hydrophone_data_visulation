#!/usr/bin/env python3
"""Build per-vessel and per-CTD acoustic profiles for the local web app."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import wave
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from hydrophone_pipeline import (
    DEFAULT_BANDS,
    UTC,
    band_column,
    db_from_amplitude,
    db_from_power,
    decode_pcm_bytes,
    parse_datetime,
    parse_float,
    parse_recording_start,
)


OUTPUT_COLUMNS = [
    "event_type",
    "event_id",
    "event_label",
    "source_time_utc",
    "event_time_utc",
    "source_distance_km",
    "propagation_delay_seconds",
    "event_offset_seconds",
    "window_start_utc",
    "window_end_utc",
    "coverage_seconds",
    "recording_files",
    "waveform_times_seconds",
    "waveform_rms_dbfs",
    "waveform_peak_dbfs",
    "sample_rate_hz",
    "channels",
    "rms_dbfs_ch1",
    "rms_dbfs_ch2",
    "rms_dbfs_mean",
    "peak_dbfs",
    "crest_factor_db",
    "stereo_correlation",
    "captured",
    "capture_note",
]


@dataclass(frozen=True)
class RecordingInfo:
    path: Path
    start: dt.datetime
    end: dt.datetime
    sample_rate: int
    channels: int
    sample_width_bytes: int
    frames: int


class RecordingCache:
    def __init__(self, max_files: int = 8) -> None:
        self.max_files = max_files
        self._cache: OrderedDict[Path, np.ndarray] = OrderedDict()

    def read(self, info: RecordingInfo) -> np.ndarray:
        if info.path in self._cache:
            samples = self._cache.pop(info.path)
            self._cache[info.path] = samples
            return samples

        with wave.open(str(info.path), "rb") as wav_file:
            data = wav_file.readframes(info.frames)
        samples = decode_pcm_bytes(data, info.channels, info.sample_width_bytes)
        self._cache[info.path] = samples
        while len(self._cache) > self.max_files:
            self._cache.popitem(last=False)
        return samples


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-data", type=Path, default=Path("web/data/app_data.json"))
    parser.add_argument("--recordings-dir", type=Path, default=Path("Data/Recordings"))
    parser.add_argument("--output-csv", type=Path, default=Path("outputs/processing/event_audio_profiles.csv"))
    parser.add_argument("--max-vessels", type=int, default=0, help="Maximum vessels to profile; 0 means all.")
    parser.add_argument("--vessel-window-seconds", type=float, default=45.0)
    parser.add_argument("--ctd-padding-seconds", type=float, default=20.0)
    parser.add_argument("--sound-speed-m-s", type=float, default=1500.0)
    parser.add_argument("--block-seconds", type=float, default=5.0)
    parser.add_argument("--waveform-bins", type=int, default=120)
    parser.add_argument("--cache-files", type=int, default=8)
    return parser.parse_args()


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def recording_index(recordings_dir: Path) -> list[RecordingInfo]:
    infos: list[RecordingInfo] = []
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
        infos.append(
            RecordingInfo(
                path=path,
                start=start,
                end=start + dt.timedelta(seconds=frames / sample_rate),
                sample_rate=sample_rate,
                channels=channels,
                sample_width_bytes=sample_width,
                frames=frames,
            )
        )
    if not infos:
        raise FileNotFoundError(f"No timestamped WAV files found in {recordings_dir}")
    return infos


def extract_audio_window(
    *,
    recordings: list[RecordingInfo],
    cache: RecordingCache,
    start: dt.datetime,
    end: dt.datetime,
) -> tuple[np.ndarray | None, int | None, list[str], float]:
    chunks: list[np.ndarray] = []
    files: list[str] = []
    sample_rate: int | None = None
    channels: int | None = None
    first_offset_seconds: float | None = None

    for info in recordings:
        if info.end <= start:
            continue
        if info.start >= end:
            break
        if sample_rate is None:
            sample_rate = info.sample_rate
            channels = info.channels
        elif sample_rate != info.sample_rate or channels != info.channels:
            continue

        overlap_start = max(start, info.start)
        overlap_end = min(end, info.end)
        frame_start = max(0, int(round((overlap_start - info.start).total_seconds() * info.sample_rate)))
        frame_end = min(info.frames, int(round((overlap_end - info.start).total_seconds() * info.sample_rate)))
        if frame_end <= frame_start:
            continue

        samples = cache.read(info)
        chunks.append(samples[frame_start:frame_end])
        files.append(info.path.name)
        if first_offset_seconds is None:
            first_offset_seconds = (overlap_start - start).total_seconds()

    if not chunks or sample_rate is None:
        return None, None, [], 0.0
    return np.concatenate(chunks, axis=0), sample_rate, files, first_offset_seconds or 0.0


def compute_waveform_summary(
    samples: np.ndarray,
    sample_rate: int,
    start_offset_seconds: float,
    bin_count: int,
) -> dict[str, str]:
    if bin_count <= 0 or samples.size == 0:
        return {
            "waveform_times_seconds": "[]",
            "waveform_rms_dbfs": "[]",
            "waveform_peak_dbfs": "[]",
        }
    if samples.ndim == 1:
        mono = samples.astype(np.float32)
    else:
        mono = samples.astype(np.float32).mean(axis=1)

    total_frames = mono.shape[0]
    bins = min(bin_count, total_frames)
    times: list[float] = []
    rms_values: list[float] = []
    peak_values: list[float] = []
    for index in range(bins):
        frame_start = int(round(index * total_frames / bins))
        frame_end = int(round((index + 1) * total_frames / bins))
        if frame_end <= frame_start:
            continue
        segment = mono[frame_start:frame_end]
        rms = float(np.sqrt(np.mean(segment.astype(np.float64) ** 2)))
        peak = float(np.max(np.abs(segment)))
        center_seconds = start_offset_seconds + ((frame_start + frame_end) / 2.0 / sample_rate)
        times.append(round(center_seconds, 3))
        rms_values.append(round(db_from_amplitude(rms), 3))
        peak_values.append(round(db_from_amplitude(peak), 3))

    return {
        "waveform_times_seconds": json.dumps(times, separators=(",", ":")),
        "waveform_rms_dbfs": json.dumps(rms_values, separators=(",", ":")),
        "waveform_peak_dbfs": json.dumps(peak_values, separators=(",", ":")),
    }


def compute_features(
    samples: np.ndarray,
    sample_rate: int,
    block_seconds: float,
) -> dict[str, str]:
    channels = samples.shape[1] if samples.ndim == 2 else 1
    if samples.ndim == 1:
        samples = samples[:, None]

    sumsq = np.sum(samples.astype(np.float64) ** 2, axis=0)
    rms = np.sqrt(sumsq / max(samples.shape[0], 1))
    rms_mean = float(np.mean(rms))
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    peak_dbfs = db_from_amplitude(peak)
    rms_mean_dbfs = db_from_amplitude(rms_mean)
    result = {
        "sample_rate_hz": str(sample_rate),
        "channels": str(channels),
        "rms_dbfs_ch1": f"{db_from_amplitude(float(rms[0])):.3f}",
        "rms_dbfs_ch2": f"{db_from_amplitude(float(rms[1])):.3f}" if channels >= 2 else "",
        "rms_dbfs_mean": f"{rms_mean_dbfs:.3f}",
        "peak_dbfs": f"{peak_dbfs:.3f}",
        "crest_factor_db": f"{peak_dbfs - rms_mean_dbfs:.3f}",
        "stereo_correlation": "",
    }
    if channels >= 2 and sumsq[0] > 0 and sumsq[1] > 0:
        correlation = float(np.sum(samples[:, 0] * samples[:, 1])) / math.sqrt(float(sumsq[0] * sumsq[1]))
        result["stereo_correlation"] = f"{correlation:.6f}"

    block_frames = max(1, int(round(sample_rate * block_seconds)))
    band_power_sum = {band: 0.0 for band in DEFAULT_BANDS}
    band_block_count = {band: 0 for band in DEFAULT_BANDS}
    for offset in range(0, samples.shape[0], block_frames):
        block = samples[offset : offset + block_frames]
        if block.shape[0] < sample_rate * 0.25:
            continue
        mono = block.mean(axis=1)
        window = np.hanning(mono.size).astype(np.float32)
        spectrum = np.fft.rfft(mono * window)
        power = (np.abs(spectrum) ** 2) / max(float(np.sum(window**2)), 1e-12)
        freqs = np.fft.rfftfreq(mono.size, d=1.0 / sample_rate)
        for band in DEFAULT_BANDS:
            mask = (freqs >= band[0]) & (freqs < band[1])
            if np.any(mask):
                band_power_sum[band] += float(np.mean(power[mask]))
                band_block_count[band] += 1

    for band in DEFAULT_BANDS:
        count = band_block_count[band]
        average_power = band_power_sum[band] / count if count else 0.0
        result[band_column(*band)] = f"{db_from_power(average_power):.3f}"
    return result


def empty_feature_values() -> dict[str, str]:
    values = {
        "sample_rate_hz": "",
        "channels": "",
        "rms_dbfs_ch1": "",
        "rms_dbfs_ch2": "",
        "rms_dbfs_mean": "",
        "peak_dbfs": "",
        "crest_factor_db": "",
        "stereo_correlation": "",
        "waveform_times_seconds": "[]",
        "waveform_rms_dbfs": "[]",
        "waveform_peak_dbfs": "[]",
    }
    for band in DEFAULT_BANDS:
        values[band_column(*band)] = ""
    return values


def safe_parse_datetime(value: object) -> dt.datetime | None:
    try:
        return parse_datetime(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def profile_row(
    *,
    event_type: str,
    event_id: str,
    event_label: str,
    source_time: dt.datetime,
    event_time: dt.datetime,
    source_distance_km: float | None,
    propagation_delay_seconds: float,
    window_start: dt.datetime,
    window_end: dt.datetime,
    recordings: list[RecordingInfo],
    cache: RecordingCache,
    block_seconds: float,
    waveform_bins: int,
) -> dict[str, str]:
    samples, sample_rate, files, audio_offset_seconds = extract_audio_window(
        recordings=recordings,
        cache=cache,
        start=window_start,
        end=window_end,
    )
    row = {
        "event_type": event_type,
        "event_id": event_id,
        "event_label": event_label,
        "source_time_utc": iso(source_time),
        "event_time_utc": iso(event_time),
        "source_distance_km": f"{source_distance_km:.4f}" if source_distance_km is not None else "",
        "propagation_delay_seconds": f"{propagation_delay_seconds:.3f}",
        "event_offset_seconds": f"{(event_time - window_start).total_seconds():.3f}",
        "window_start_utc": iso(window_start),
        "window_end_utc": iso(window_end),
        "coverage_seconds": "0.000",
        "recording_files": "",
        "waveform_times_seconds": "[]",
        "waveform_rms_dbfs": "[]",
        "waveform_peak_dbfs": "[]",
        "captured": "false",
        "capture_note": "No hydrophone recording overlaps this event window.",
    }
    if samples is None or sample_rate is None or samples.size == 0:
        row.update(empty_feature_values())
        return row

    row["coverage_seconds"] = f"{samples.shape[0] / sample_rate:.3f}"
    row["recording_files"] = ";".join(files)
    row["captured"] = "true"
    row["capture_note"] = "Hydrophone audio was available for this event window."
    row.update(compute_waveform_summary(samples, sample_rate, audio_offset_seconds, waveform_bins))
    row.update(compute_features(samples, sample_rate, block_seconds))
    return row


def load_events(
    app_data_path: Path,
    max_vessels: int,
    vessel_window_seconds: float,
    ctd_padding_seconds: float,
    audio_start: dt.datetime,
    audio_end: dt.datetime,
    sound_speed_m_s: float,
):
    data = json.loads(app_data_path.read_text(encoding="utf-8"))
    half_vessel = dt.timedelta(seconds=vessel_window_seconds / 2)
    events: list[dict[str, object]] = []

    vessels = data.get("vessels", [])
    if max_vessels > 0:
        vessels = vessels[:max_vessels]

    for vessel in vessels:
        track_candidates = []
        for point in vessel.get("track", []):
            point_time = safe_parse_datetime(point.get("timestamp"))
            if point_time is None:
                continue
            distance_km = parse_float(point.get("distanceKm"))
            delay_seconds = ((distance_km or 0.0) * 1000.0) / sound_speed_m_s
            arrival_time = point_time + dt.timedelta(seconds=delay_seconds)
            if arrival_time + half_vessel < audio_start or arrival_time - half_vessel > audio_end:
                continue
            track_candidates.append(point)

        if track_candidates:
            best_point = min(
                track_candidates,
                key=lambda point: float(point.get("distanceKm") or float("inf")),
            )
            source_time = safe_parse_datetime(best_point.get("timestamp"))
            source_distance_km = parse_float(best_point.get("distanceKm"))
        else:
            source_time = safe_parse_datetime(vessel.get("closestTimestamp"))
            source_distance_km = parse_float(vessel.get("closestDistanceKm"))
        if source_time is None:
            continue
        propagation_delay_seconds = ((source_distance_km or 0.0) * 1000.0) / sound_speed_m_s
        event_time = source_time + dt.timedelta(seconds=propagation_delay_seconds)
        events.append(
            {
                "event_type": "vessel",
                "event_id": str(vessel.get("id", "")),
                "event_label": str(vessel.get("name") or vessel.get("id") or "Vessel"),
                "source_time": source_time,
                "event_time": event_time,
                "source_distance_km": source_distance_km,
                "propagation_delay_seconds": propagation_delay_seconds,
                "window_start": event_time - half_vessel,
                "window_end": event_time + half_vessel,
            }
        )

    ctd_padding = dt.timedelta(seconds=ctd_padding_seconds)
    for event in data.get("ctdEvents", []):
        start = safe_parse_datetime(event.get("startUtc"))
        end = safe_parse_datetime(event.get("endUtc")) or start
        if start is None or end is None:
            continue
        station = str(event.get("station") or event.get("id") or event.get("fileName") or "CTD")
        midpoint = start + (end - start) / 2
        source_distance_km = parse_float(event.get("distanceToHydrophoneKm"))
        propagation_delay_seconds = ((source_distance_km or 0.0) * 1000.0) / sound_speed_m_s
        arrival_midpoint = midpoint + dt.timedelta(seconds=propagation_delay_seconds)
        events.append(
            {
                "event_type": "ctd",
                "event_id": station,
                "event_label": f"CTD {station}",
                "source_time": midpoint,
                "event_time": arrival_midpoint,
                "source_distance_km": source_distance_km,
                "propagation_delay_seconds": propagation_delay_seconds,
                "window_start": start + dt.timedelta(seconds=propagation_delay_seconds) - ctd_padding,
                "window_end": end + dt.timedelta(seconds=propagation_delay_seconds) + ctd_padding,
            }
        )
    return events


def main() -> None:
    args = parse_args()
    recordings = recording_index(args.recordings_dir)
    cache = RecordingCache(max_files=args.cache_files)
    events = load_events(
        args.app_data,
        args.max_vessels,
        args.vessel_window_seconds,
        args.ctd_padding_seconds,
        audio_start=recordings[0].start,
        audio_end=recordings[-1].end,
        sound_speed_m_s=args.sound_speed_m_s,
    )
    fieldnames = OUTPUT_COLUMNS + [band_column(*band) for band in DEFAULT_BANDS]

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    captured = 0
    with args.output_csv.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for index, event in enumerate(events, start=1):
            row = profile_row(
                event_type=event["event_type"],
                event_id=event["event_id"],
                event_label=event["event_label"],
                source_time=event["source_time"],
                event_time=event["event_time"],
                source_distance_km=event["source_distance_km"],
                propagation_delay_seconds=event["propagation_delay_seconds"],
                window_start=event["window_start"],
                window_end=event["window_end"],
                recordings=recordings,
                cache=cache,
                block_seconds=args.block_seconds,
                waveform_bins=args.waveform_bins,
            )
            writer.writerow(row)
            captured += row["captured"] == "true"
            if index % 25 == 0:
                print(f"Built {index}/{len(events)} event audio profiles...")

    print(f"Wrote {args.output_csv}")
    print(f"Events: {len(events)}; with audio: {captured}")


if __name__ == "__main__":
    main()
