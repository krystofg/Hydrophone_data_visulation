#!/usr/bin/env python3
"""Build a vessel-template confusion matrix from independent AIS-free scans."""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import html
import json
import math
from collections import defaultdict
from pathlib import Path


SOUND_SPEED_M_S = 1445.0
NO_DETECTION = "NO DETECTION"
BACKGROUND = "BACKGROUND"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ais-csv", type=Path, required=True)
    parser.add_argument(
        "--scan",
        action="append",
        required=True,
        metavar="NAME=MMSI=CSV",
        help="Template name, ground-truth MMSI, and keep-all scan CSV. Repeat for each vessel.",
    )
    parser.add_argument("--threshold", type=float, default=72.0)
    parser.add_argument("--max-distance-km", type=float, default=5.0)
    parser.add_argument("--min-sog", type=float, default=1.0)
    parser.add_argument("--max-time-offset-seconds", type=float, default=45.0)
    parser.add_argument("--output-csv", type=Path, default=Path("outputs/acoustic_profile_detection/fleet_confusion_matrix.csv"))
    parser.add_argument("--output-html", type=Path, default=Path("outputs/acoustic_profile_detection/fleet_confusion_matrix.html"))
    parser.add_argument("--output-json", type=Path, default=Path("outputs/acoustic_profile_detection/fleet_confusion_matrix.json"))
    return parser.parse_args()


def parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def number(value: object) -> float | None:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_spec(value: str) -> tuple[str, str, Path]:
    parts = value.split("=", 2)
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError(f"Invalid --scan value: {value!r}; expected NAME=MMSI=CSV")
    return parts[0].strip(), parts[1].strip(), Path(parts[2].strip())


def load_arrivals(path: Path, mmsi_to_name: dict[str, str], max_distance: float, min_sog: float) -> dict[str, list[dict[str, object]]]:
    arrivals: dict[str, list[dict[str, object]]] = defaultdict(list)
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            return arrivals
        id_column = "ship_id" if "ship_id" in reader.fieldnames else "mmsi"
        time_column = "timestamp" if "timestamp" in reader.fieldnames else "timestamp_utc"
        for row in reader:
            ship_id = row.get(id_column, "")
            if ship_id not in mmsi_to_name:
                continue
            distance = number(row.get("distance_km"))
            sog = number(row.get("sog"))
            lat = number(row.get("lat") or row.get("latitude"))
            lon = number(row.get("lon") or row.get("longitude"))
            if distance is None or lat is None or lon is None or distance > max_distance or (sog or 0.0) < min_sog:
                continue
            try:
                source = parse_time(row.get(time_column, ""))
            except ValueError:
                continue
            arrivals[mmsi_to_name[ship_id]].append({
                "arrival": source.timestamp() + distance * 1000.0 / SOUND_SPEED_M_S,
                "source_time": source.isoformat().replace("+00:00", "Z"),
                "lat": lat, "lon": lon, "distance": distance,
            })
    for values in arrivals.values():
        values.sort(key=lambda item: float(item["arrival"]))
    return arrivals


def nearest_point(values: list[dict[str, object]], times: list[float], target: float) -> tuple[float, dict[str, object] | None]:
    if not values:
        return math.inf, None
    index = bisect.bisect_left(times, target)
    candidates = [values[i] for i in (index - 1, index) if 0 <= i < len(values)]
    point = min(candidates, key=lambda item: abs(float(item["arrival"]) - target), default=None)
    return (abs(float(point["arrival"]) - target), point) if point is not None else (math.inf, None)


def load_scan(path: Path) -> tuple[dict[str, float], dict[str, float]]:
    scores: dict[str, float] = {}
    centers: dict[str, float] = {}
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        for row in csv.DictReader(stream):
            score = number(row.get("score"))
            if score is None:
                continue
            key = f"{row.get('file', '')}|{row.get('start_utc', '')}"
            try:
                start = parse_time(row.get("start_utc", ""))
                end = parse_time(row.get("end_utc", ""))
            except ValueError:
                continue
            scores[key] = score
            centers[key] = (start.timestamp() + end.timestamp()) / 2.0
    return scores, centers


def main() -> None:
    args = parse_args()
    specs = [parse_spec(value) for value in args.scan]
    names = [name for name, _, _ in specs]
    if len(set(names)) != len(names):
        raise SystemExit("Template names in --scan must be unique.")
    for _, _, path in specs:
        if not path.exists():
            raise SystemExit(f"Missing scan CSV: {path}")

    arrivals = load_arrivals(args.ais_csv, {mmsi: name for name, mmsi, _ in specs}, args.max_distance_km, args.min_sog)
    arrival_times = {name: [float(item["arrival"]) for item in values] for name, values in arrivals.items()}
    missing_ais = [name for name in names if not arrivals.get(name)]
    if missing_ais:
        raise SystemExit("No qualifying AIS passage for: " + ", ".join(missing_ais))

    scan_scores: dict[str, dict[str, float]] = {}
    centers: dict[str, float] = {}
    common_keys: set[str] | None = None
    for name, _, path in specs:
        values, scan_centers = load_scan(path)
        scan_scores[name] = values
        centers.update(scan_centers)
        common_keys = set(values) if common_keys is None else common_keys & set(values)
    keys = sorted(common_keys or [])
    if not keys:
        raise SystemExit("The scan CSV files have no common scored windows. Use identical scan settings and --keep-all.")

    predicted_labels = names + [NO_DETECTION]
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    classified_points: list[dict[str, object]] = []
    ambiguous = 0
    for key in keys:
        center = centers[key]
        nearest = {name: nearest_point(arrivals[name], arrival_times[name], center) for name in names}
        actual_matches = [name for name in names if nearest[name][0] <= args.max_time_offset_seconds]
        if len(actual_matches) > 1:
            ambiguous += 1
            continue
        actual = actual_matches[0] if actual_matches else BACKGROUND
        scores = {name: scan_scores[name][key] for name in names}
        winner = max(scores, key=scores.get)
        predicted = winner if scores[winner] >= args.threshold else NO_DETECTION
        counts[actual][predicted] += 1
        if actual != BACKGROUND:
            point = nearest[actual][1]
            if point is not None:
                classified_points.append({
                    "actual": actual, "predicted": predicted, "score": scores[winner],
                    "lat": point["lat"], "lon": point["lon"], "distance": point["distance"],
                    "sourceTime": point["source_time"], "correct": predicted == actual,
                })

    actual_labels = names + [BACKGROUND]
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["actual", "predicted", "count", "row_total", "row_percent"])
        writer.writeheader()
        for actual in actual_labels:
            total = sum(counts[actual].values())
            for predicted in predicted_labels:
                value = counts[actual][predicted]
                writer.writerow({
                    "actual": actual, "predicted": predicted, "count": value, "row_total": total,
                    "row_percent": f"{(100.0 * value / total if total else 0.0):.3f}",
                })

    matrix = []
    for actual in actual_labels:
        total = sum(counts[actual].values())
        matrix.append({
            "actual": actual, "total": total,
            "cells": [
                {"predicted": predicted, "count": counts[actual][predicted], "percent": 100.0 * counts[actual][predicted] / total if total else 0.0}
                for predicted in predicted_labels
            ],
        })
    report_data = {
        "columns": predicted_labels, "rows": matrix, "points": classified_points, "ships": names,
        "commonWindows": len(keys), "threshold": args.threshold, "ambiguous": ambiguous,
        "maxDistanceKm": args.max_distance_km,
    }
    payload = json.dumps(report_data, separators=(",", ":"))
    headers = "".join(f"<th>{html.escape(name)}</th>" for name in predicted_labels)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Fleet acoustic classification</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>*{{box-sizing:border-box}}body{{margin:0;font:14px Segoe UI,Arial,sans-serif;color:#18242c;background:#f4f6f7}}header{{background:#142a33;color:#fff;padding:16px 24px 12px}}h1{{font-size:22px;margin:0 0 4px}}header p{{margin:0;color:#cad6da}}.tabs{{display:flex;gap:4px;padding:9px 22px;background:#fff;border-bottom:1px solid #ccd5d8}}.tab{{border:1px solid #aebbc1;background:#fff;padding:8px 14px;border-radius:5px;font-weight:650;cursor:pointer}}.tab.active{{background:#173f50;border-color:#173f50;color:#fff}}.panel{{display:none}}.panel.active{{display:block}}.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:#ccd5d8;border-bottom:1px solid #ccd5d8}}.metric{{background:#fff;padding:11px 16px}}.label{{font-size:11px;text-transform:uppercase;color:#66747b}}.value{{font-size:20px;font-weight:650;margin-top:3px}}#map-layout{{display:grid;grid-template-columns:220px 1fr;height:calc(100vh - 183px)}}.sidebar{{background:#fff;padding:14px;border-right:1px solid #ccd5d8;overflow:auto}}.ship-filter{{display:block;width:100%;text-align:left;border:1px solid #b8c3c8;background:#fff;padding:8px 10px;margin-bottom:5px;border-radius:4px;cursor:pointer;font-weight:600}}.ship-filter.active{{background:#173f50;color:#fff;border-color:#173f50}}#map{{min-height:520px}}.legend{{background:#fff;border:1px solid #c7d0d4;padding:9px 11px;line-height:1.8}}.swatch{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}}.matrix-wrap{{max-width:1200px;margin:auto;padding:22px;overflow:auto}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{border:1px solid #d6dee1;padding:10px;text-align:center}}th{{background:#eaf0f2;font-size:12px}}th:first-child,td:first-child{{text-align:left;font-weight:650}}.cell strong{{display:block;font-size:18px}}.cell span{{font-size:11px;color:#536169}}.notes{{margin-top:18px;background:#fff;border-left:4px solid #d0a22d;padding:12px 16px;line-height:1.5}}@media(max-width:760px){{.metrics{{grid-template-columns:1fr}}#map-layout{{display:block;height:auto}}.sidebar{{border-right:0}}#map{{height:65vh}}table{{min-width:760px}}}}</style></head><body>
<header><h1>Fleet acoustic classification</h1><p>Independent template scans classified first; AIS positions joined only for validation.</p></header><nav class="tabs"><button class="tab active" data-panel="map-panel">Classification map</button><button class="tab" data-panel="matrix-panel">Confusion matrix</button></nav>
<section class="metrics"><div class="metric"><div class="label">Common windows</div><div class="value">{len(keys)}</div></div><div class="metric"><div class="label">Threshold</div><div class="value">{args.threshold:.1f}</div></div><div class="metric"><div class="label">Ambiguous windows excluded</div><div class="value">{ambiguous}</div></div></section>
<section id="map-panel" class="panel active"><div id="map-layout"><aside class="sidebar"><b>Actual vessel</b><div id="ship-filters"></div><p>Marker fill shows the predicted template. A black ring marks a wrong classification; grey means no detection.</p></aside><div id="map"></div></div></section>
<section id="matrix-panel" class="panel"><div class="matrix-wrap"><table><thead><tr><th>Actual / Predicted</th>{headers}</tr></thead><tbody id="matrix"></tbody></table><section class="notes"><b>How to read it:</b> diagonal cells are correct vessel identifications. Off-diagonal cells are vessel confusion. The BACKGROUND row measures false alarms, and NO DETECTION measures misses. Only moving AIS passages within {args.max_distance_km:g} km are labeled; simultaneous selected-vessel windows are excluded.</section></div></section>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>const data={payload},colors={{'FRENCH WARSHIP':'#2474a6','VICTORY':'#9b59b6','SALSA':'#df8b20','HAVFISKEN':'#087f6f','BALTIC SPLIT':'#8c5a3c','NO DETECTION':'#929da2'}};const body=document.getElementById('matrix');data.rows.forEach(row=>{{const tr=document.createElement('tr'),label=document.createElement('td');label.textContent=row.actual+` (${{row.total}})`;tr.appendChild(label);row.cells.forEach(cell=>{{const td=document.createElement('td');td.className='cell';const expected=(row.actual===cell.predicted)||(row.actual==='BACKGROUND'&&cell.predicted==='NO DETECTION');const strength=Math.min(.8,cell.percent/100*.8);td.style.background=expected?`rgba(26,130,112,${{strength}})`:`rgba(205,70,58,${{strength}})`;td.innerHTML=`<strong>${{cell.percent.toFixed(1)}}%</strong><span>${{cell.count}} windows</span>`;tr.appendChild(td)}});body.appendChild(tr)}});
const map=L.map('map');L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png',{{maxZoom:19,attribution:'© OpenStreetMap © CARTO'}}).addTo(map);let pointLayers=[];function showShip(name){{document.querySelectorAll('.ship-filter').forEach(b=>b.classList.toggle('active',b.dataset.ship===name));pointLayers.forEach(layer=>map.removeLayer(layer));pointLayers=[];const points=data.points.filter(p=>p.actual===name);points.forEach(p=>{{const layer=L.circleMarker([p.lat,p.lon],{{radius:4,color:p.correct?'#fff':'#111',weight:p.correct?1:2,fillColor:colors[p.predicted]||'#d44b40',fillOpacity:.88}}).bindTooltip(`<b>Actual: ${{p.actual}}</b><br>Predicted: ${{p.predicted}}<br>score ${{p.score.toFixed(1)}} · ${{p.distance.toFixed(2)}} km<br>${{p.sourceTime}}`).addTo(map);pointLayers.push(layer)}});if(points.length)map.fitBounds(L.latLngBounds(points.map(p=>[p.lat,p.lon])),{{padding:[30,30]}})}}const filters=document.getElementById('ship-filters');data.ships.forEach(name=>{{const button=document.createElement('button');button.className='ship-filter';button.dataset.ship=name;button.textContent=name;button.addEventListener('click',()=>showShip(name));filters.appendChild(button)}});const legend=L.control({{position:'bottomright'}});legend.onAdd=()=>{{const div=L.DomUtil.create('div','legend');div.innerHTML='<b>Predicted template</b><br>'+data.columns.map(name=>`<span class="swatch" style="background:${{colors[name]||'#d44b40'}}"></span>${{name}}`).join('<br>');return div}};legend.addTo(map);showShip(data.ships[0]);document.querySelectorAll('.tab').forEach(button=>button.addEventListener('click',()=>{{document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b===button));document.querySelectorAll('.panel').forEach(panel=>panel.classList.toggle('active',panel.id===button.dataset.panel));if(button.dataset.panel==='map-panel')setTimeout(()=>map.invalidateSize(),0)}}));</script></body></html>"""
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.write_text(page, encoding="utf-8")
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report_data, indent=2), encoding="utf-8")
    print(f"Common windows: {len(keys)}; ambiguous excluded: {ambiguous}")
    print(f"Wrote {args.output_csv}")
    print(f"Wrote {args.output_html}")
    print(f"Wrote {args.output_json}")


if __name__ == "__main__":
    main()
