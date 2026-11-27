#!/usr/bin/env python3
"""Deterministic hardware & repository sanity checks for KiCad 10 projects.

Catches common manufacturing bugs that ERC/DRC alone miss:
1. LCSC <-> Value conflicts (same LCSC code assigned to components with different values)
2. Missing LCSC Part # on non-DNP components
3. Broken ${KIPRJMOD} entries in sym-lib-table / fp-lib-table
4. BOM <-> CPL mismatches (non-DNP BOM designators missing from CPL, if CPL provided)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

from config_loader import load_config


def check_lib_tables(root: Path) -> List[Dict[str, str]]:
    broken: List[Dict[str, str]] = []
    uri_re = re.compile(r'\(lib\s+\(name\s+"([^"]+)"\).*?\(uri\s+"([^"]+)"\)')
    for tbl_name in ("sym-lib-table", "fp-lib-table"):
        tbl = root / tbl_name
        if not tbl.is_file():
            continue
        for line in tbl.read_text(encoding="utf-8", errors="replace").splitlines():
            m = uri_re.search(line)
            if not m:
                continue
            lib_name, uri = m.group(1), m.group(2)
            if "${KIPRJMOD}" in uri:
                rel = uri.replace("${KIPRJMOD}/", "").replace("${KIPRJMOD}", "")
                target = root / rel
                if not target.exists():
                    broken.append({"table": tbl_name, "lib": lib_name, "uri": uri})
    return broken


def check_bom_sanity(bom_json: Path) -> Dict[str, Any]:
    data = json.loads(bom_json.read_text(encoding="utf-8"))
    items = data.get("items", [])

    # Detect same LCSC code used for different component values
    by_lcsc: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    missing_lcsc: List[Dict[str, Any]] = []
    all_refs = set()

    for it in items:
        for r in it.get("designators", []):
            all_refs.add(r)
        code = (it.get("lcsc") or "").strip().upper()
        if not code:
            missing_lcsc.append(
                {
                    "refs": it.get("designator_str", ""),
                    "value": it.get("comment", ""),
                    "footprint": it.get("footprint", ""),
                }
            )
        else:
            by_lcsc[code].append(it)

    conflicts: List[Dict[str, Any]] = []
    for code, entries in sorted(by_lcsc.items()):
        distinct_vals = sorted({(e.get("comment") or "").strip().lower() for e in entries})
        if len(distinct_vals) > 1:
            conflicts.append(
                {
                    "lcsc": code,
                    "entries": [
                        {"refs": e["designator_str"], "value": e["comment"], "footprint": e["footprint"]}
                        for e in entries
                    ],
                }
            )

    return {
        "total_components": data.get("total_components", 0),
        "unique_parts": data.get("unique_parts", 0),
        "lcsc_value_conflicts": conflicts,
        "missing_lcsc": missing_lcsc,
        "all_refs": sorted(all_refs),
    }


def check_cpl_consistency(bom_refs: List[str], cpl_csv: Path) -> List[str]:
    if not cpl_csv.is_file():
        return []
    cpl_refs = set()
    with cpl_csv.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            ref = (row.get("Designator") or "").strip()
            if ref:
                cpl_refs.add(ref)
    return sorted(r for r in bom_refs if r not in cpl_refs)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run deterministic KiCad hardware sanity checks.")
    ap.add_argument("--bom-json", required=True, help="Path to bom-merged.json")
    ap.add_argument("--out-dir", required=True, help="Output directory")
    ap.add_argument("--cpl-csv", default=None, help="Optional JLCPCB CPL CSV to verify BOM coverage")
    ap.add_argument("--config", default=None, help="Path to ci/config.yml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    root = Path(cfg["_root"])
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    bom_res = check_bom_sanity(Path(args.bom_json))
    broken_libs = check_lib_tables(root)
    missing_from_cpl = (
        check_cpl_consistency(bom_res["all_refs"], Path(args.cpl_csv)) if args.cpl_csv else []
    )

    conflicts = bom_res["lcsc_value_conflicts"]
    missing_lcsc = bom_res["missing_lcsc"]

    md: List[str] = [
        "### Hardware & BOM Sanity Checks",
        "",
        f"- **BOM Components:** {bom_res['total_components']} placements ({bom_res['unique_parts']} unique lines)",
        f"- **LCSC ↔ Value Conflicts:** {'✅ 0' if not conflicts else f'⚠️ **{len(conflicts)} conflict(s)**'}",
        f"- **Missing LCSC Part #:** {'✅ 0' if not missing_lcsc else f'⚠️ **{len(missing_lcsc)} line(s)**'}",
        f"- **Broken Local Lib Paths (`sym`/`fp-lib-table`):** {'✅ 0' if not broken_libs else f'⚠️ **{len(broken_libs)} missing**'}",
    ]
    if args.cpl_csv:
        md.append(
            f"- **BOM Parts Missing from CPL:** {'✅ 0' if not missing_from_cpl else f'⚠️ **{len(missing_from_cpl)} part(s)** ({', '.join(missing_from_cpl)})'}"
        )
    md.append("")

    if conflicts:
        md.append("<details open><summary>⚠️ LCSC ↔ Value Conflicts (check before ordering!)</summary>\n")
        for c in conflicts:
            desc = " vs ".join(f"`{e['refs']}` = `{e['value']}`" for e in c["entries"])
            md.append(f"- **`{c['lcsc']}`** assigned to different values: {desc}")
            print(f"::warning title=LCSC Value Conflict ({c['lcsc']})::{desc}")
        md.append("\n</details>\n")

    if missing_lcsc:
        md.append("<details><summary>⚠️ BOM Lines Missing LCSC Part #</summary>\n")
        for m in missing_lcsc:
            md.append(f"- `{m['refs']}` — `{m['value']}` (`{m['footprint']}`)")
        md.append("\n</details>\n")

    if broken_libs:
        md.append("<details><summary>⚠️ Broken Local Library Table Entries</summary>\n")
        for b in broken_libs:
            md.append(f"- `{b['table']}`: **`{b['lib']}`** → `{b['uri']}` (not found)")
        md.append("\n</details>\n")
        print(
            f"::warning title=Broken KiCad Library Paths::"
            f"{len(broken_libs)} local library entries in sym-lib-table/fp-lib-table point to missing files."
        )

    summary_md = "\n".join(md).strip() + "\n"
    (out_dir / "sanity-summary.md").write_text(summary_md, encoding="utf-8")
    (out_dir / "sanity-summary.json").write_text(
        json.dumps(
            {
                "lcsc_value_conflicts": conflicts,
                "missing_lcsc": missing_lcsc,
                "broken_libs": broken_libs,
                "missing_from_cpl": missing_from_cpl,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(summary_md)
    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary:
        with open(gh_summary, "a", encoding="utf-8") as f:
            f.write("\n" + summary_md + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
