#!/usr/bin/env python3
"""Build one tabbed fleet report from the full distance-validation pages."""

from __future__ import annotations

import argparse
import base64
import html
import json
from pathlib import Path


DEFAULT_PAGES = [
    ("FRENCH WARSHIP", "french_warship_distance_validation_full.html", ""),
    ("VICTORY", "victory_distance_validation_full.html", ""),
    ("SALSA", "salsa_distance_validation_full.html", ""),
    ("HAVFISKEN", "havfisken_distance_validation_full.html", ""),
    ("BALTIC SPLIT", "baltic_split_distance_validation_full.html", ""),
    ("CLASSIFICATION MAP", "fleet_confusion_matrix_standalone.html", "map-panel"),
    ("CONFUSION MATRIX", "fleet_confusion_matrix_standalone.html", "matrix-panel"),
]


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pages-dir", type=Path,
        default=Path("outputs/acoustic_profile_detection"),
    )
    parser.add_argument(
        "--output-html", type=Path,
        default=Path("outputs/acoustic_profile_detection/fleet_confusion_matrix.html"),
    )
    return parser.parse_args()


def main() -> None:
    args = arguments()
    missing = [name for name, filename, _ in DEFAULT_PAGES if not (args.pages_dir / filename).exists()]
    if missing:
        raise SystemExit("Missing report pages for: " + ", ".join(missing))

    documents = {
        filename: base64.b64encode((args.pages_dir / filename).read_bytes()).decode("ascii")
        for _, filename, _ in DEFAULT_PAGES
    }
    pages = [{"name": name, "document": filename, "panel": panel} for name, filename, panel in DEFAULT_PAGES]
    buttons = "".join(
        f'<button class="tab{" active" if index == 0 else ""}" data-index="{index}">{html.escape(name)}</button>'
        for index, (name, _, _) in enumerate(DEFAULT_PAGES)
    )
    payload = json.dumps(pages, separators=(",", ":"))
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fleet acoustic range validation</title>
<style>
*{{box-sizing:border-box}}html,body{{height:100%;margin:0;background:#f4f6f7;font:14px Segoe UI,Arial,sans-serif;color:#18212b;overflow:hidden}}
.tabs{{height:48px;display:flex;align-items:center;gap:5px;padding:7px 12px;background:#fff;border-bottom:1px solid #ccd4d8;overflow-x:auto}}
.tab{{border:1px solid #aebbc1;background:#fff;color:#24343c;padding:8px 15px;border-radius:5px;font-weight:650;cursor:pointer;white-space:nowrap}}
.tab:hover{{background:#eef3f5}}.tab.active{{background:#173f50;border-color:#173f50;color:#fff}}
iframe{{display:block;width:100%;height:calc(100vh - 48px);border:0;background:#fff}}
</style></head><body>
<nav class="tabs" aria-label="Fleet reports">{buttons}</nav>
<iframe id="report" title="Fleet acoustic report"></iframe>
<script>
const pages={payload},documents={json.dumps(documents, separators=(",", ":"))},frame=document.getElementById('report');let selected=0;
function documentHtml(name){{const bytes=Uint8Array.from(atob(documents[name]),c=>c.charCodeAt(0));return new TextDecoder('utf-8').decode(bytes)}}
function activate(index){{selected=index;document.querySelectorAll('.tab').forEach((button,i)=>button.classList.toggle('active',i===index));frame.srcdoc=documentHtml(pages[index].document)}}
frame.addEventListener('load',()=>{{const panel=pages[selected].panel;if(!panel)return;try{{const button=frame.contentDocument.querySelector(`[data-panel="${{panel}}"]`);if(button)button.click()}}catch(error){{console.warn('Could not select nested report panel',error)}}}});
document.querySelectorAll('.tab').forEach((button,index)=>button.addEventListener('click',()=>activate(index)));
activate(0);
</script></body></html>"""
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.write_text(page, encoding="utf-8")
    print("Included pages: " + ", ".join(page["name"] for page in pages))
    print(f"Wrote {args.output_html}")


if __name__ == "__main__":
    main()
