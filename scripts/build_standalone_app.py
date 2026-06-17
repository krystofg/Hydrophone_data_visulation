#!/usr/bin/env python3
"""Build a single clickable HTML file for the hydrophone web app."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=Path("web/index.html"))
    parser.add_argument("--css", type=Path, default=Path("web/styles.css"))
    parser.add_argument("--js", type=Path, default=Path("web/app.js"))
    parser.add_argument("--data", type=Path, default=Path("web/data/app_data.json"))
    parser.add_argument("--output", type=Path, default=Path("web/hydrophone_app.html"))
    return parser.parse_args()


def safe_script_json(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    text = json.dumps(data, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def main() -> None:
    args = parse_args()
    html = args.index.read_text(encoding="utf-8")
    css = args.css.read_text(encoding="utf-8")
    js = args.js.read_text(encoding="utf-8")
    data_json = safe_script_json(args.data)

    html = html.replace('<link rel="stylesheet" href="styles.css">', f"<style>\n{css}\n</style>")
    html = html.replace(
        '<script src="app.js"></script>',
        f'<script>window.HYDROPHONE_APP_DATA = {data_json};</script>\n<script>\n{js}\n</script>',
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
