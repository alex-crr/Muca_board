#!/usr/bin/env python3
"""Generate JLCPCB PCBA manufacturing package using KiCad 10 CLI.

Produces:
- <prefix>-jlcpcb-gerbers.zip      (Gerbers + Excellon drills)
- <prefix>-jlcpcb-bom.csv          (JLCPCB BOM merging schematic + PCB LCSC properties)
- <prefix>-jlcpcb-cpl.csv          (JLCPCB CPL with rotation/offset & bottom-side transforms)
- <prefix>-jlcpcb-production.zip   (All-in-one bundle)
- bom-merged.json                  (Structured grouped BOM for live JLC price calculator)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config_loader import (
    kicad_env,
    load_config,
    normalize_lcsc,
    parse_pcb_footprints,
    resolve_project_path,
)


def _run(cmd: List[str]) -> None:
    print(f"[CMD] {' '.join(cmd)}")
    res = subprocess.run(cmd, env=kicad_env(), capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stdout)
        print(res.stderr, file=sys.stderr)
        raise RuntimeError(f"Command failed ({res.returncode}): {' '.join(cmd)}")


def _nat_sort_key(ref: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", ref)]


def export_gerbers_and_drills(
    cfg: Dict[str, Any], pcb_path: Path, out_dir: Path, prefix: str
) -> Path:
    gerber_dir = out_dir / "gerbers"
    gerber_dir.mkdir(parents=True, exist_ok=True)

    layers = ",".join(cfg["jlcpcb"]["gerber_layers"])
    _run(
        [
            "kicad-cli",
            "pcb",
            "export",
            "gerbers",
            "--layers",
            layers,
            "--subtract-soldermask",
            "--use-drill-file-origin",
            "--check-zones",
            "--output",
            str(gerber_dir) + "/",
            str(pcb_path),
        ]
    )

    _run(
        [
            "kicad-cli",
            "pcb",
            "export",
            "drill",
            "--format",
            "excellon",
            "--excellon-units",
            "mm",
            "--excellon-zeros-format",
            "decimal",
            "--excellon-separate-th",
            "--drill-origin",
            "plot",
            "--generate-map",
            "--map-format",
            "pdf",
            "--output",
            str(gerber_dir) + "/",
            str(pcb_path),
        ]
    )

    zip_path = out_dir / f"{prefix}-jlcpcb-gerbers.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(gerber_dir.iterdir()):
            if f.is_file() and f.suffix.lower() not in (".pdf",):
                zf.write(f, arcname=f.name)
    print(f"[JLCPCB] Wrote Gerbers+Drills ZIP: {zip_path}")
    return zip_path


def extract_merged_bom(
    cfg: Dict[str, Any], sch_path: Path, pcb_path: Path, out_dir: Path, prefix: str
) -> Tuple[Path, Path, Dict[str, Any]]:
    """Extract per-reference BOM from KiCad schematic and enrich with PCB footprint
    properties (so LCSC codes stored on either schematic or PCB are always captured)."""
    raw_bom_csv = out_dir / "_raw_sch_bom.csv"
    cmd = [
        "kicad-cli",
        "sch",
        "export",
        "bom",
        "--fields",
        "Reference,Value,Footprint,LCSC Part,LCSC Part #,LCSC,Datasheet,DNP",
        "--labels",
        "Reference,Value,Footprint,LCSC_Part,LCSC_Part_Hash,LCSC,Datasheet,DNP",
        "--ref-delimiter",
        ",",
        "--ref-range-delimiter",
        "",
        "--output",
        str(raw_bom_csv),
    ]
    if cfg["jlcpcb"].get("exclude_dnp", True):
        cmd.append("--exclude-dnp")
    cmd.append(str(sch_path))
    _run(cmd)

    pcb_fps = parse_pcb_footprints(pcb_path)

    # Read ungrouped rows from schematic BOM
    items_by_ref: Dict[str, Dict[str, Any]] = {}
    with raw_bom_csv.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            refs = [r.strip() for r in (row.get("Reference") or "").split(",") if r.strip()]
            sch_lcsc = (
                normalize_lcsc(row.get("LCSC_Part", ""))
                or normalize_lcsc(row.get("LCSC_Part_Hash", ""))
                or normalize_lcsc(row.get("LCSC", ""))
            )
            for ref in refs:
                if ref.startswith("#"):
                    continue
                fp_info = pcb_fps.get(ref, {})
                if cfg["jlcpcb"].get("exclude_dnp", True) and fp_info.get("dnp"):
                    continue
                if fp_info.get("exclude_from_bom"):
                    continue

                lcsc = sch_lcsc or fp_info.get("lcsc", "")
                fp_full = (row.get("Footprint") or fp_info.get("footprint") or "").strip()
                fp_short = fp_full.split(":", 1)[-1] if ":" in fp_full else fp_full
                val = (row.get("Value") or fp_info.get("value") or "").strip()
                datasheet = (row.get("Datasheet") or fp_info.get("datasheet") or "").strip()
                if datasheet == "~":
                    datasheet = ""

                items_by_ref[ref] = {
                    "reference": ref,
                    "value": val,
                    "footprint_full": fp_full,
                    "footprint": fp_short,
                    "lcsc": lcsc,
                    "datasheet": datasheet,
                    "layer": fp_info.get("layer", ""),
                }

    # Group by (value, footprint, lcsc)
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for ref in sorted(items_by_ref.keys(), key=_nat_sort_key):
        it = items_by_ref[ref]
        groups[(it["value"], it["footprint"], it["lcsc"])].append(it)

    grouped_rows: List[Dict[str, Any]] = []
    for (val, fp_short, lcsc), members in sorted(
        groups.items(), key=lambda kv: _nat_sort_key(kv[1][0]["reference"])
    ):
        refs = sorted([m["reference"] for m in members], key=_nat_sort_key)
        datasheet = next((m["datasheet"] for m in members if m["datasheet"]), "")
        layers = sorted({m["layer"] for m in members if m["layer"]})
        grouped_rows.append(
            {
                "comment": val,
                "designators": refs,
                "designator_str": ",".join(refs),
                "footprint": fp_short,
                "footprint_full": members[0]["footprint_full"],
                "quantity": len(refs),
                "lcsc": lcsc,
                "datasheet": datasheet,
                "layers": layers,
            }
        )

    # Write JLCPCB BOM CSV
    jlc_bom_csv = out_dir / f"{prefix}-jlcpcb-bom.csv"
    with jlc_bom_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        for g in grouped_rows:
            w.writerow([g["comment"], g["designator_str"], g["footprint"], g["lcsc"]])

    missing_lcsc = [g for g in grouped_rows if not g["lcsc"]]
    merged_payload = {
        "project": cfg["project"]["name"],
        "board": cfg["project"]["board_name"],
        "total_components": sum(g["quantity"] for g in grouped_rows),
        "unique_parts": len(grouped_rows),
        "missing_lcsc_groups": len(missing_lcsc),
        "missing_lcsc_refs": [r for g in missing_lcsc for r in g["designators"]],
        "items": grouped_rows,
    }
    merged_json = out_dir / "bom-merged.json"
    merged_json.write_text(json.dumps(merged_payload, indent=2), encoding="utf-8")

    print(
        f"[JLCPCB] Wrote BOM ({merged_payload['total_components']} parts, "
        f"{merged_payload['unique_parts']} unique, {len(missing_lcsc)} missing LCSC): {jlc_bom_csv}"
    )
    return jlc_bom_csv, merged_json, merged_payload


def _load_rotation_file(path: Path, source: str) -> List[Tuple[re.Pattern, float, float, float, str]]:
    rules: List[Tuple[re.Pattern, float, float, float, str]] = []
    if not path.is_file():
        return rules
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    for row in csv.DictReader(lines):
        pat = (row.get("pattern") or "").strip()
        if not pat:
            continue
        rot = float(row.get("rotation") or 0.0)
        ox = float(row.get("offset_x") or 0.0)
        oy = float(row.get("offset_y") or 0.0)
        rules.append((re.compile(pat), rot, ox, oy, source))
    return rules


def _load_rotation_rules(cfg: Dict[str, Any]) -> List[Tuple[re.Pattern, float, float, float, str]]:
    """Load project-local overrides first (highest priority), then vendored base JLCPCB table.
    Fully deterministic — zero live network fetches."""
    root = Path(cfg["_root"])
    local_file = root / cfg["jlcpcb"]["rotations_file"]
    base_rel = cfg["jlcpcb"].get("base_rotations_file", "ci/jlcpcb-rotations-base.csv")
    base_file = root / base_rel

    local_rules = _load_rotation_file(local_file, "local")
    base_rules = _load_rotation_file(base_file, "base")
    rules = local_rules + base_rules
    print(
        f"[JLCPCB] Loaded {len(local_rules)} local override rule(s) + "
        f"{len(base_rules)} vendored base rule(s)."
    )
    return rules


def generate_cpl(
    cfg: Dict[str, Any], pcb_path: Path, out_dir: Path, prefix: str
) -> Path:
    raw_pos = out_dir / "_raw_pos.csv"
    cmd = [
        "kicad-cli",
        "pcb",
        "export",
        "pos",
        "--format",
        "csv",
        "--units",
        "mm",
        "--side",
        "both",
        "--use-drill-file-origin",
        "--output",
        str(raw_pos),
    ]
    if cfg["jlcpcb"].get("exclude_dnp", True):
        cmd.append("--exclude-dnp")
    if cfg["jlcpcb"].get("cpl_smd_only", False):
        cmd.append("--smd-only")
    cmd.append(str(pcb_path))
    _run(cmd)

    rules = _load_rotation_rules(cfg)
    cpl_csv = out_dir / f"{prefix}-jlcpcb-cpl.csv"

    rows_out: List[List[str]] = []
    with raw_pos.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            ref = (row.get("Ref") or "").strip()
            pkg = (row.get("Package") or "").strip()
            if not ref or ref.startswith("#"):
                continue

            x = float(row["PosX"])
            y = float(row["PosY"])
            rot = float(row["Rot"])
            side_raw = (row.get("Side") or "top").strip().lower()
            is_bottom = side_raw in ("bottom", "back", "b.cu")
            layer_str = "Bottom" if is_bottom else "Top"

            # Find first matching rotation/offset rule (exact REF: override or footprint regex)
            delta_rot = 0.0
            off_x = 0.0
            off_y = 0.0
            for pat, d_rot, ox, oy, _src in rules:
                if pat.pattern.startswith("REF:"):
                    if pat.pattern[4:] == ref:
                        delta_rot, off_x, off_y = d_rot, ox, oy
                        break
                elif pat.search(pkg):
                    delta_rot, off_x, off_y = d_rot, ox, oy
                    break

            # Apply local offset rotated by KiCad angle if specified
            if off_x != 0.0 or off_y != 0.0:
                rad = math.radians(rot)
                dx = off_x * math.cos(rad) - off_y * math.sin(rad)
                dy = off_x * math.sin(rad) + off_y * math.cos(rad)
                x += -dx if is_bottom else dx
                y += dy

            if is_bottom:
                jlc_rot = (180.0 - rot + delta_rot) % 360.0
            else:
                jlc_rot = (rot + delta_rot) % 360.0

            # Avoid "-0.0" formatting
            if abs(jlc_rot) < 1e-6 or abs(jlc_rot - 360.0) < 1e-6:
                jlc_rot = 0.0

            rows_out.append([ref, f"{x:.4f}mm", f"{y:.4f}mm", layer_str, f"{jlc_rot:.2f}"])

    rows_out.sort(key=lambda r: _nat_sort_key(r[0]))

    with cpl_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        w.writerows(rows_out)

    print(f"[JLCPCB] Wrote CPL ({len(rows_out)} placements): {cpl_csv}")
    return cpl_csv


def main() -> int:
    ap = argparse.ArgumentParser(description="Build JLCPCB Gerbers, BOM, and CPL.")
    ap.add_argument("--out-dir", required=True, help="Output directory")
    ap.add_argument("--version", default="dev", help="Version tag or commit short SHA")
    ap.add_argument("--config", default=None, help="Path to ci/config.yml")
    ap.add_argument("--bom-only", action="store_true", help="Only extract merged BOM (fast CI check)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    sch_path = resolve_project_path(cfg, "schematic")
    pcb_path = resolve_project_path(cfg, "pcb")
    prefix = f"{cfg['project']['name']}-{args.version}"

    jlc_bom, merged_json, _ = extract_merged_bom(cfg, sch_path, pcb_path, out_dir, prefix)
    if args.bom_only:
        return 0

    gerbers_zip = export_gerbers_and_drills(cfg, pcb_path, out_dir, prefix)
    jlc_cpl = generate_cpl(cfg, pcb_path, out_dir, prefix)

    # All-in-one production archive
    bundle_zip = out_dir / f"{prefix}-jlcpcb-production.zip"
    with zipfile.ZipFile(bundle_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(gerbers_zip, arcname=gerbers_zip.name)
        zf.write(jlc_bom, arcname=jlc_bom.name)
        zf.write(jlc_cpl, arcname=jlc_cpl.name)

    print(f"[JLCPCB] Wrote full PCBA bundle: {bundle_zip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
