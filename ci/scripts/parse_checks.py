#!/usr/bin/env python3
"""Run and/or parse KiCad 10 ERC & DRC JSON reports.

Policy:
- Fail on Errors only (exit code 1 when configured).
- Always report Warnings & Exclusions cleanly in Markdown / GitHub Step Summary.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

from config_loader import kicad_env, load_config, resolve_project_path


def run_kicad_checks(cfg: Dict[str, Any], out_dir: Path) -> Tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    sch_path = resolve_project_path(cfg, "schematic")
    pcb_path = resolve_project_path(cfg, "pcb")
    erc_json = out_dir / "erc.json"
    drc_json = out_dir / "drc.json"

    env = kicad_env()

    erc_cmd = [
        "kicad-cli",
        "sch",
        "erc",
        "--format",
        "json",
        "--severity-all",
        "--units",
        "mm",
        "--output",
        str(erc_json),
        str(sch_path),
    ]
    print(f"[ERC] Running: {' '.join(erc_cmd)}")
    res_erc = subprocess.run(erc_cmd, env=env, capture_output=True, text=True)
    if res_erc.returncode != 0 and not erc_json.is_file():
        print(res_erc.stdout)
        print(res_erc.stderr, file=sys.stderr)
        raise RuntimeError(f"kicad-cli sch erc failed with exit {res_erc.returncode}")

    drc_cmd = [
        "kicad-cli",
        "pcb",
        "drc",
        "--format",
        "json",
        "--severity-all",
        "--units",
        "mm",
        "--output",
        str(drc_json),
    ]
    if cfg.get("checks", {}).get("drc_schematic_parity", True):
        drc_cmd.append("--schematic-parity")
    if cfg.get("checks", {}).get("drc_refill_zones", True):
        drc_cmd.append("--refill-zones")
    drc_cmd.append(str(pcb_path))

    print(f"[DRC] Running: {' '.join(drc_cmd)}")
    res_drc = subprocess.run(drc_cmd, env=env, capture_output=True, text=True)
    if res_drc.returncode != 0 and not drc_json.is_file():
        print(res_drc.stdout)
        print(res_drc.stderr, file=sys.stderr)
        raise RuntimeError(f"kicad-cli pcb drc failed with exit {res_drc.returncode}")

    return erc_json, drc_json


def _tally_violations(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    exclusions: List[Dict[str, Any]] = []
    other: List[Dict[str, Any]] = []

    for v in items:
        sev = (v.get("severity") or "").lower()
        if sev == "error":
            errors.append(v)
        elif sev == "warning":
            warnings.append(v)
        elif sev in ("exclusion", "excluded", "ignore", "ignored"):
            exclusions.append(v)
        else:
            other.append(v)

    return {
        "errors": errors,
        "warnings": warnings,
        "exclusions": exclusions,
        "other": other,
        "error_types": Counter(v.get("type", "unknown") for v in errors),
        "warning_types": Counter(v.get("type", "unknown") for v in warnings),
    }


def analyze_erc(erc_path: Path) -> Dict[str, Any]:
    data = json.loads(erc_path.read_text(encoding="utf-8"))
    all_v: List[Dict[str, Any]] = []
    for sheet in data.get("sheets", []):
        sheet_path = sheet.get("path", "/")
        for v in sheet.get("violations", []):
            v_copy = dict(v)
            v_copy["_sheet"] = sheet_path
            all_v.append(v_copy)
    tally = _tally_violations(all_v)
    tally["kicad_version"] = data.get("kicad_version", "")
    tally["source"] = data.get("source", "")
    return tally


def analyze_drc(drc_path: Path) -> Dict[str, Any]:
    data = json.loads(drc_path.read_text(encoding="utf-8"))
    violations = data.get("violations", [])
    unconnected = data.get("unconnected_items", [])
    parity = data.get("schematic_parity", [])

    v_tally = _tally_violations(violations)
    u_tally = _tally_violations(unconnected)
    p_tally = _tally_violations(parity)

    combined = _tally_violations(violations + unconnected + parity)
    combined["kicad_version"] = data.get("kicad_version", "")
    combined["source"] = data.get("source", "")
    combined["breakdown"] = {
        "rules_errors": len(v_tally["errors"]),
        "rules_warnings": len(v_tally["warnings"]),
        "unconnected_errors": len(u_tally["errors"]),
        "unconnected_warnings": len(u_tally["warnings"]),
        "parity_errors": len(p_tally["errors"]),
        "parity_warnings": len(p_tally["warnings"]),
    }
    return combined


def _format_violation_list(items: List[Dict[str, Any]], limit: int = 25) -> List[str]:
    lines: List[str] = []
    for idx, v in enumerate(items[:limit], 1):
        vtype = v.get("type", "unknown")
        desc = v.get("description", "")
        locs = []
        for it in v.get("items", [])[:2]:
            d = it.get("description", "")
            pos = it.get("pos") or {}
            if "x" in pos and "y" in pos:
                locs.append(f"{d} @ ({pos['x']:.2f}, {pos['y']:.2f} mm)")
            elif d:
                locs.append(d)
        detail = " — ".join(locs)
        sheet = f" [{v['_sheet']}]" if v.get("_sheet") else ""
        lines.append(f"{idx}. **`{vtype}`**{sheet}: {desc}" + (f" ({detail})" if detail else ""))
    if len(items) > limit:
        lines.append(f"\n_...and {len(items) - limit} more (see full JSON report artifact)._")
    return lines


def build_markdown(
    cfg: Dict[str, Any],
    erc: Dict[str, Any],
    drc: Dict[str, Any],
) -> str:
    erc_err = len(erc["errors"])
    erc_warn = len(erc["warnings"])
    erc_excl = len(erc["exclusions"])
    drc_err = len(drc["errors"])
    drc_warn = len(drc["warnings"])
    drc_excl = len(drc["exclusions"])
    bd = drc["breakdown"]

    erc_badge = "✅ PASS" if erc_err == 0 else f"🚨 **{erc_err} ERROR(S)**"
    drc_badge = "✅ PASS" if drc_err == 0 else f"🚨 **{drc_err} ERROR(S)**"

    total_errors = erc_err + drc_err
    total_warnings = erc_warn + drc_warn

    md: List[str] = [
        f"## KiCad 10 ERC & DRC Report — `{cfg['project']['board_name']}`",
        "",
    ]

    if total_errors > 0:
        md.extend(
            [
                "> [!CAUTION]",
                f"> **🚨 {total_errors} KiCad ERROR(S) DETECTED ({erc_err} ERC, {drc_err} DRC)** — "
                "The pipeline continued without blocking so artifacts remain available. **Do not manufacture before reviewing!**",
                "",
            ]
        )
    elif total_warnings > 0:
        md.extend(
            [
                "> [!WARNING]",
                f"> **0 errors**, but **{total_warnings} warning(s)** detected ({erc_warn} ERC, {drc_warn} DRC).",
                "",
            ]
        )
    else:
        md.extend(
            [
                "> [!TIP]",
                "> **All clean!** 0 ERC errors/warnings and 0 DRC errors/warnings.",
                "",
            ]
        )

    md.extend(
        [
            f"- **KiCad Version:** `{drc.get('kicad_version') or erc.get('kicad_version') or 'unknown'}`",
            "- **Gate Policy:** Non-blocking — errors are highlighted via alerts & GitHub annotations without stopping the build.",
            "",
            "| Check | Status | Errors | Warnings | Exclusions |",
            "| :--- | :---: | :---: | :---: | :---: |",
            f"| **ERC** (`{cfg['project']['schematic']}`) | {erc_badge} | **{erc_err}** | {erc_warn} | {erc_excl} |",
            f"| **DRC** (`{cfg['project']['pcb']}`) | {drc_badge} | **{drc_err}** | {drc_warn} | {drc_excl} |",
            f"| ↳ PCB Rules | — | {bd['rules_errors']} | {bd['rules_warnings']} | — |",
            f"| ↳ Unconnected Items | — | {bd['unconnected_errors']} | {bd['unconnected_warnings']} | — |",
            f"| ↳ Schematic Parity | — | {bd['parity_errors']} | {bd['parity_warnings']} | — |",
            "",
        ]
    )

    if erc_err > 0:
        md.append("<details><summary>❌ ERC Errors by Category</summary>\n")
        for k, cnt in erc["error_types"].most_common():
            md.append(f"- `{k}`: **{cnt}**")
        md.append("")
        md.extend(_format_violation_list(erc["errors"], limit=20))
        md.append("\n</details>\n")

    if erc_warn > 0:
        md.append("<details><summary>⚠️ ERC Warnings</summary>\n")
        for k, cnt in erc["warning_types"].most_common():
            md.append(f"- `{k}`: **{cnt}**")
        md.append("")
        md.extend(_format_violation_list(erc["warnings"], limit=15))
        md.append("\n</details>\n")

    if drc_err > 0:
        md.append("<details><summary>❌ DRC Errors by Category</summary>\n")
        for k, cnt in drc["error_types"].most_common():
            md.append(f"- `{k}`: **{cnt}**")
        md.append("")
        md.extend(_format_violation_list(drc["errors"], limit=20))
        md.append("\n</details>\n")

    if drc_warn > 0:
        md.append("<details><summary>⚠️ DRC Warnings</summary>\n")
        for k, cnt in drc["warning_types"].most_common():
            md.append(f"- `{k}`: **{cnt}**")
        md.append("")
        md.extend(_format_violation_list(drc["warnings"], limit=15))
        md.append("\n</details>\n")

    return "\n".join(md).strip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Run and evaluate KiCad 10 ERC & DRC.")
    ap.add_argument("--out-dir", required=True, help="Directory for reports")
    ap.add_argument("--config", default=None, help="Path to ci/config.yml")
    ap.add_argument("--parse-only", action="store_true", help="Skip running kicad-cli; parse existing erc.json/drc.json")
    ap.add_argument("--no-fail", action="store_true", help="Always exit 0 (report-only mode)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.parse_only:
        erc_json = out_dir / "erc.json"
        drc_json = out_dir / "drc.json"
    else:
        erc_json, drc_json = run_kicad_checks(cfg, out_dir)

    erc = analyze_erc(erc_json)
    drc = analyze_drc(drc_json)
    md = build_markdown(cfg, erc, drc)

    summary_md = out_dir / "checks-summary.md"
    summary_md.write_text(md, encoding="utf-8")

    summary_json = out_dir / "checks-summary.json"
    summary_payload = {
        "kicad_version": drc.get("kicad_version") or erc.get("kicad_version"),
        "erc": {
            "errors": len(erc["errors"]),
            "warnings": len(erc["warnings"]),
            "exclusions": len(erc["exclusions"]),
            "error_types": dict(erc["error_types"]),
            "warning_types": dict(erc["warning_types"]),
        },
        "drc": {
            "errors": len(drc["errors"]),
            "warnings": len(drc["warnings"]),
            "exclusions": len(drc["exclusions"]),
            "breakdown": drc["breakdown"],
            "error_types": dict(drc["error_types"]),
            "warning_types": dict(drc["warning_types"]),
        },
    }
    summary_json.write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")

    print(md)

    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary:
        with open(gh_summary, "a", encoding="utf-8") as f:
            f.write(md + "\n")

    # Emit prominent GitHub Actions UI annotations even when non-blocking
    sch_rel = cfg["project"]["schematic"]
    pcb_rel = cfg["project"]["pcb"]
    if len(erc["errors"]) > 0:
        top_erc = ", ".join(f"{k} ({v})" for k, v in erc["error_types"].most_common(5))
        print(
            f"::error file={sch_rel},title=KiCad ERC: {len(erc['errors'])} error(s)::"
            f"ERC found {len(erc['errors'])} error(s) and {len(erc['warnings'])} warning(s): {top_erc}"
        )
    elif len(erc["warnings"]) > 0:
        print(
            f"::warning file={sch_rel},title=KiCad ERC: {len(erc['warnings'])} warning(s)::"
            f"ERC found 0 errors and {len(erc['warnings'])} warning(s)."
        )

    if len(drc["errors"]) > 0:
        top_drc = ", ".join(f"{k} ({v})" for k, v in drc["error_types"].most_common(5))
        print(
            f"::error file={pcb_rel},title=KiCad DRC: {len(drc['errors'])} error(s)::"
            f"DRC found {len(drc['errors'])} error(s) "
            f"({drc['breakdown']['rules_errors']} rules, {drc['breakdown']['unconnected_errors']} unconnected, "
            f"{drc['breakdown']['parity_errors']} parity) and {len(drc['warnings'])} warning(s): {top_drc}"
        )
    elif len(drc["warnings"]) > 0:
        print(
            f"::warning file={pcb_rel},title=KiCad DRC: {len(drc['warnings'])} warning(s)::"
            f"DRC found 0 errors and {len(drc['warnings'])} warning(s)."
        )

    fail_erc = cfg.get("checks", {}).get("fail_on_erc_errors", False) and len(erc["errors"]) > 0
    fail_drc = cfg.get("checks", {}).get("fail_on_drc_errors", False) and len(drc["errors"]) > 0

    if not args.no_fail and (fail_erc or fail_drc):
        print(
            f"::error::KiCad strict gate failed — ERC errors: {len(erc['errors'])}, DRC errors: {len(drc['errors'])}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
