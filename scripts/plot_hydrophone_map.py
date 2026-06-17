#!/usr/bin/env python3
"""Create an interactive HTML map from hydrophone deployment coordinates."""

from __future__ import annotations

import argparse
import csv
import html
import json
from pathlib import Path
from statistics import mean


REQUIRED_COLUMNS = {"latitude", "longitude"}
POPUP_COLUMNS = [
    "id",
    "name",
    "site",
    "depth_m",
    "deployment_start",
    "deployment_end",
    "notes",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot hydrophone locations from a CSV file as an interactive HTML map."
    )
    parser.add_argument("input_csv", type=Path, help="CSV containing latitude/longitude columns.")
    parser.add_argument("output_html", type=Path, help="HTML map file to create.")
    return parser.parse_args()


def load_hydrophones(input_csv: Path) -> list[dict[str, str | float]]:
    with input_csv.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames:
            raise ValueError(f"{input_csv} has no header row.")

        normalized = {column.lower().strip(): column for column in reader.fieldnames}
        missing = REQUIRED_COLUMNS - set(normalized)
        if missing:
            missing_text = ", ".join(sorted(missing))
            raise ValueError(f"{input_csv} is missing required column(s): {missing_text}")

        rows: list[dict[str, str | float]] = []
        for line_number, row in enumerate(reader, start=2):
            latitude_text = row[normalized["latitude"]]
            longitude_text = row[normalized["longitude"]]
            try:
                latitude = float(latitude_text)
                longitude = float(longitude_text)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid coordinates on line {line_number}: "
                    f"latitude={latitude_text!r}, longitude={longitude_text!r}"
                ) from exc

            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise ValueError(
                    f"Coordinates out of range on line {line_number}: "
                    f"latitude={latitude}, longitude={longitude}"
                )

            marker: dict[str, str | float] = {
                "latitude": latitude,
                "longitude": longitude,
            }
            for output_name, source_name in normalized.items():
                value = row.get(source_name, "").strip()
                if value:
                    marker[output_name] = value
            rows.append(marker)

    if not rows:
        raise ValueError(f"{input_csv} does not contain any hydrophone rows.")
    return rows


def marker_popup(row: dict[str, str | float]) -> str:
    title = str(row.get("name") or row.get("id") or "Hydrophone")
    lines = [f"<strong>{html.escape(title)}</strong>"]
    for column in POPUP_COLUMNS:
        if column == "name":
            continue
        value = row.get(column)
        if value is not None and value != "":
            label = column.replace("_", " ").title()
            lines.append(f"<br><b>{html.escape(label)}:</b> {html.escape(str(value))}")
    return "".join(lines)


def render_html(rows: list[dict[str, str | float]]) -> str:
    latitudes = [float(row["latitude"]) for row in rows]
    longitudes = [float(row["longitude"]) for row in rows]
    center = [mean(latitudes), mean(longitudes)]
    marker_data = [
        {
            "lat": row["latitude"],
            "lon": row["longitude"],
            "popup": marker_popup(row),
        }
        for row in rows
    ]

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hydrophone Map</title>
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
    .legend {{
      background: white;
      border-radius: 6px;
      box-shadow: 0 2px 10px rgba(0, 0, 0, 0.16);
      color: #17202a;
      font: 14px/1.35 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      padding: 10px 12px;
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
    const markers = {json.dumps(marker_data, indent=6)};
    const map = L.map("map").setView({json.dumps(center)}, 9);

    L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);

    const markerLayer = L.featureGroup();
    markers.forEach((hydrophone) => {{
      L.circleMarker([hydrophone.lat, hydrophone.lon], {{
        radius: 8,
        color: "#0f5e9c",
        weight: 2,
        fillColor: "#22a6b3",
        fillOpacity: 0.88
      }})
        .bindPopup(hydrophone.popup)
        .addTo(markerLayer);
    }});
    markerLayer.addTo(map);
    map.fitBounds(markerLayer.getBounds().pad(0.2));

    const legend = L.control({{ position: "bottomleft" }});
    legend.onAdd = () => {{
      const div = L.DomUtil.create("div", "legend");
      div.innerHTML = `<strong>Hydrophones</strong><br>${{markers.length}} station${{markers.length === 1 ? "" : "s"}}`;
      return div;
    }};
    legend.addTo(map);
  </script>
</body>
</html>
"""


def main() -> None:
    args = parse_args()
    rows = load_hydrophones(args.input_csv)
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.write_text(render_html(rows), encoding="utf-8")
    print(f"Wrote {args.output_html} with {len(rows)} hydrophone marker(s).")


if __name__ == "__main__":
    main()
