#!/usr/bin/env python3
"""Generate deterministic GitHub Release notes from checks, priced BOM, and optional docs/CHANGELOG.md."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Optional

from config_loader import load_config


def extract_changelog_section(changelog_path: Path, version: str) -> Optional[str]:
    """Extract the matching ## [vX.Y.Z] or ## vX.Y.Z section from docs/CHANGELOG.md if present."""
    if not changelog_path.is_file():
        return None
    text = changelog_path.read_text(encoding="utf-8")
    ver_clean = version.lstrip("v")
    pat = re.compile(
        rf"^##\s+\[?v?{re.escape(ver_clean)}\]?.*$",
        flags=re.MULTILINE | re.IGNORECASE,
    )
    m = pat.search(text)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r"^##\s+", text[start:], flags=re.MULTILINE)
    section = text[start : start + nxt.start()] if nxt else text[start:]
    return section.strip() or None


def main() -> int:
    ap = argparse.ArgumentParser(description="Build deterministic GitHub Release notes.")
    ap.add_argument("--version", required=True)
    ap.add_argument("--checks-json", default=None)
    ap.add_argument("--bom-json", default=None)
    ap.add_argument("--pages-url", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    root = Path(cfg["_root"])
    display = cfg["project"].get("display_name", cfg["project"]["name"])

    lines = [f"# {display} — `{args.version}`", ""]

    chk = None
    if args.checks_json and Path(args.checks_json).is_file():
        chk = json.loads(Path(args.checks_json).read_text(encoding="utf-8"))
        erc_err = chk["erc"]["errors"]
        drc_err = chk["drc"]["errors"]
        if erc_err > 0 or drc_err > 0:
            lines.extend(
                [
                    "> [!CAUTION]",
                    f"> **🚨 BUILT WITH {erc_err + drc_err} KiCad ERROR(S) ({erc_err} ERC, {drc_err} DRC)**",
                    "> Review the attached `*-reports.zip` and ERC/DRC report before sending to manufacturing!",
                    "",
                ]
            )

    # Manual changelog section if maintainer wrote one
    cl_rel = cfg.get("docs", {}).get("changelog", "docs/CHANGELOG.md")
    cl_section = extract_changelog_section(root / cl_rel, args.version)
    if cl_section:
        lines.extend(["## Release Notes", "", cl_section, ""])

    if args.pages_url:
        base = args.pages_url.rstrip("/")
        lines.extend(
            [
                "## Interactive Documentation & Tools",
                "",
                f"- **Documentation Site:** {base}/",
                f"- **Interactive BOM (iBOM):** {base}/ibom.html",
                f"- **Priced BOM & Quantity Slider:** {base}/bom.html",
                f"- **PCB Renders & 3D:** {base}/renders.html",
                "",
            ]
        )

    if chk:
        erc = chk["erc"]
        drc = chk["drc"]
        erc_icon = "✅" if erc["errors"] == 0 else "🚨"
        drc_icon = "✅" if drc["errors"] == 0 else "🚨"
        lines.extend(
            [
                "## Verification Status",
                "",
                f"- **KiCad Version:** `{chk.get('kicad_version', '10.x')}`",
                f"- **ERC:** {erc_icon} **{erc['errors']} errors**, {erc['warnings']} warnings, {erc['exclusions']} exclusions",
                f"- **DRC:** {drc_icon} **{drc['errors']} errors**, {drc['warnings']} warnings, {drc['exclusions']} exclusions",
                "",
            ]
        )

    if args.bom_json and Path(args.bom_json).is_file():
        bom = json.loads(Path(args.bom_json).read_text(encoding="utf-8"))
        sums = bom.get("summaries", {})
        lines.extend(
            [
                "## BOM & Estimated JLCPCB Component Cost",
                "",
                f"- **Total Placements / Board:** {bom['total_components']} ({bom['unique_parts']} unique lines)",
                f"- **Price Snapshot Fetched:** {bom.get('fetched_at', 'N/A')}",
                "",
                "| Board Qty | Components / Board | Total Components | Extended Feeder Fees | Est. Total (Parts + Fees) | Stock Readiness |",
                "| :---: | :---: | :---: | :---: | :---: | :---: |",
            ]
        )
        for q in ("5", "10", "50", "100"):
            if q in sums:
                s = sums[q]
                lines.append(
                    f"| **{q}** | ${s['parts_per_board_usd']:.2f} | ${s['parts_total_usd']:.2f} | "
                    f"${s['feeder_fees_usd']:.2f} ({s['extended_parts_count']} ext) | "
                    f"**${s['grand_total_usd']:.2f}** (${s['grand_per_board_usd']:.2f}/bd) | "
                    f"{s['in_stock_lines']}/{bom['unique_parts']} lines |"
                )
        lines.append("")

    prefix = f"{cfg['project']['name']}-{args.version}"
    lines.extend(
        [
            "## Attached Files",
            "",
            f"- `{prefix}-jlcpcb-production.zip` — Complete JLCPCB PCBA pack (Gerbers + Drills + BOM + CPL)",
            f"- `{prefix}-jlcpcb-gerbers.zip`, `{prefix}-jlcpcb-bom.csv`, `{prefix}-jlcpcb-cpl.csv` — Individual JLCPCB upload files",
            f"- `{prefix}-bom-priced.csv` — Component price & stock snapshot",
            f"- `{prefix}-ibom.html` — Standalone Interactive HTML BOM",
            f"- `{prefix}-schematic.pdf` — Schematic PDF",
            f"- `{prefix}-3d.step` — 3D STEP CAD model",
            f"- `{prefix}-renders.zip` & `{prefix}-reports.zip` — 2D/3D renders and ERC/DRC JSON reports",
            "",
        ]
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[RELEASE-NOTES] Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
