#!/usr/bin/env python3
"""Generate deterministic visual PCB/Schematic diff & BOM delta between two git revisions (or directories).

Outputs:
- <out-dir>/visual-diff.html   (self-contained viewer: Before / After / Diff Overlay / Compare Slider)
- <out-dir>/diff-summary.json
- <out-dir>/diff-summary.md    (concise Markdown block for PR comments)
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageChops

from config_loader import kicad_env, load_config, parse_pcb_footprints


def _run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"[DIFF] {' '.join(cmd)}")
    res = subprocess.run(cmd, env=kicad_env(), capture_output=True, text=True)
    if check and res.returncode != 0:
        print(res.stdout)
        print(res.stderr, file=sys.stderr)
        raise RuntimeError(f"Command failed ({res.returncode}): {' '.join(cmd)}")
    return res


def _svg_to_png(svg_path: Path, png_path: Path, width: int = 1600) -> bool:
    """Convert SVG to PNG using rsvg-convert or inkscape if available."""
    if shutil.which("rsvg-convert"):
        res = _run(
            ["rsvg-convert", "-w", str(width), "-b", "white", "-o", str(png_path), str(svg_path)],
            check=False,
        )
        return res.returncode == 0 and png_path.is_file()
    if shutil.which("inkscape"):
        res = _run(
            ["inkscape", str(svg_path), f"--export-width={width}", f"--export-filename={png_path}"],
            check=False,
        )
        return res.returncode == 0 and png_path.is_file()
    return False


def render_snapshot(cfg: Dict[str, Any], repo_dir: Path, snap_dir: Path) -> Dict[str, Any]:
    """Render schematic SVGs/PNGs, 3D PCB top/bottom PNGs, and extract BOM refs for a snapshot."""
    snap_dir.mkdir(parents=True, exist_ok=True)
    sch_path = (repo_dir / cfg["project"]["schematic"]).resolve()
    pcb_path = (repo_dir / cfg["project"]["pcb"]).resolve()

    result: Dict[str, Any] = {
        "pcb_renders": {},
        "sch_sheets": {},
        "bom_refs": {},
    }

    r_cfg = cfg.get("renders", {})
    w = int(r_cfg.get("pr_diff_width", 1200))
    h = int(r_cfg.get("pr_diff_height", 900))
    q = str(r_cfg.get("pr_diff_quality", "basic"))

    if pcb_path.is_file():
        for side in ("top", "bottom"):
            out_png = snap_dir / f"pcb-{side}.png"
            _run(
                [
                    "kicad-cli",
                    "pcb",
                    "render",
                    "--side",
                    side,
                    "--width",
                    str(w),
                    "--height",
                    str(h),
                    "--background",
                    "opaque",
                    "--quality",
                    q,
                    "--output",
                    str(out_png),
                    str(pcb_path),
                ],
                check=False,
            )
            if out_png.is_file():
                result["pcb_renders"][f"PCB 3D ({side.title()})"] = out_png

        # Parse footprint map for BOM delta
        fps = parse_pcb_footprints(pcb_path)
        for ref, fp in fps.items():
            if not fp.get("dnp") and not fp.get("exclude_from_bom"):
                result["bom_refs"][ref] = {
                    "value": fp.get("value", ""),
                    "footprint": fp.get("footprint", "").split(":")[-1],
                    "lcsc": fp.get("lcsc", ""),
                }

    if sch_path.is_file():
        svg_dir = snap_dir / "sch_svg"
        svg_dir.mkdir(parents=True, exist_ok=True)
        _run(
            [
                "kicad-cli",
                "sch",
                "export",
                "svg",
                "--output",
                str(svg_dir),
                str(sch_path),
            ],
            check=False,
        )
        for svg in sorted(svg_dir.glob("*.svg")):
            png = svg.with_suffix(".png")
            if _svg_to_png(svg, png, width=1600):
                result["sch_sheets"][f"Schematic: {svg.stem}"] = png

        # Also enrich bom_refs from schematic BOM CSV
        bom_csv = snap_dir / "_bom.csv"
        res = _run(
            [
                "kicad-cli",
                "sch",
                "export",
                "bom",
                "--fields",
                "Reference,Value,Footprint,LCSC Part,LCSC Part #,LCSC",
                "--labels",
                "Reference,Value,Footprint,L1,L2,L3",
                "--ref-delimiter",
                ",",
                "--ref-range-delimiter",
                "",
                "--exclude-dnp",
                "--output",
                str(bom_csv),
                str(sch_path),
            ],
            check=False,
        )
        if res.returncode == 0 and bom_csv.is_file():
            with bom_csv.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    lcsc = (row.get("L1") or row.get("L2") or row.get("L3") or "").strip().upper()
                    val = (row.get("Value") or "").strip()
                    fp = (row.get("Footprint") or "").split(":")[-1].strip()
                    for ref in [r.strip() for r in (row.get("Reference") or "").split(",") if r.strip()]:
                        if ref.startswith("#"):
                            continue
                        cur = result["bom_refs"].get(ref, {"value": val, "footprint": fp, "lcsc": ""})
                        if val:
                            cur["value"] = val
                        if fp:
                            cur["footprint"] = fp
                        if lcsc and not cur["lcsc"]:
                            cur["lcsc"] = lcsc
                        result["bom_refs"][ref] = cur

    return result


def compute_pixel_diff(
    before_path: Optional[Path], after_path: Optional[Path], diff_path: Path
) -> Tuple[float, int]:
    """Create a red/green diff overlay image and return (% changed pixels, changed count)."""
    if before_path is None or after_path is None:
        return (100.0, -1)

    img_a = Image.open(before_path).convert("RGB")
    img_b = Image.open(after_path).convert("RGB")

    # Normalize dimensions onto a common canvas if sizes differ
    w = max(img_a.width, img_b.width)
    h = max(img_a.height, img_b.height)
    if img_a.size != (w, h):
        canvas = Image.new("RGB", (w, h), (255, 255, 255))
        canvas.paste(img_a, (0, 0))
        img_a = canvas
    if img_b.size != (w, h):
        canvas = Image.new("RGB", (w, h), (255, 255, 255))
        canvas.paste(img_b, (0, 0))
        img_b = canvas

    diff = ImageChops.difference(img_a, img_b)
    diff_gray = diff.convert("L")
    # Threshold minor anti-aliasing jitter
    mask = diff_gray.point(lambda p: 255 if p > 12 else 0)
    changed_pixels = mask.histogram()[255]
    total_pixels = w * h
    pct = (changed_pixels / total_pixels) * 100.0 if total_pixels else 0.0

    # Build visual overlay: dimmed grayscale of "after", red where removed (darker in A), green where added (darker in B)
    lum_a = img_a.convert("L")
    lum_b = img_b.convert("L")
    base_dim = lum_b.point(lambda p: int(40 + p * 0.45))

    out = Image.new("RGB", (w, h))
    pa = lum_a.load()
    pb = lum_b.load()
    pm = mask.load()
    pd = base_dim.load()
    po = out.load()

    for y in range(h):
        for x in range(w):
            if pm[x, y]:
                if pa[x, y] < pb[x, y]:
                    # Feature was present in base, removed in head -> Red
                    po[x, y] = (248, 81, 73)
                else:
                    # Feature added/changed in head -> Green
                    po[x, y] = (63, 185, 80)
            else:
                g = pd[x, y]
                po[x, y] = (g, g, g)

    out.save(diff_path, format="PNG", optimize=True)
    return (round(pct, 3), changed_pixels)


def compute_bom_delta(
    base_refs: Dict[str, Dict[str, str]], head_refs: Dict[str, Dict[str, str]]
) -> Dict[str, Any]:
    added = []
    removed = []
    changed = []
    for ref in sorted(set(base_refs) | set(head_refs)):
        if ref not in base_refs:
            added.append({"ref": ref, **head_refs[ref]})
        elif ref not in head_refs:
            removed.append({"ref": ref, **base_refs[ref]})
        elif base_refs[ref] != head_refs[ref]:
            changed.append({"ref": ref, "before": base_refs[ref], "after": head_refs[ref]})
    return {"added": added, "removed": removed, "changed": changed}


def _img_b64(path: Optional[Path]) -> str:
    if not path or not path.is_file():
        return ""
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build_diff_html(
    title: str, comparisons: List[Dict[str, Any]], bom_delta: Dict[str, Any]
) -> str:
    cards_html = []
    for idx, c in enumerate(comparisons):
        b64_before = _img_b64(c.get("before"))
        b64_after = _img_b64(c.get("after"))
        b64_diff = _img_b64(c.get("diff"))
        badge = (
            f'<span class="badge changed">{c["changed_pct"]:.2f}% changed</span>'
            if c["changed_pct"] > 0
            else '<span class="badge same">Unchanged</span>'
        )
        cards_html.append(
            f"""
            <section class="card" data-idx="{idx}">
              <div class="card-head">
                <h2>{c['label']}</h2>
                <div class="tabs">
                  {badge}
                  <button class="tab active" data-mode="diff">Diff Overlay</button>
                  <button class="tab" data-mode="after">After (PR)</button>
                  <button class="tab" data-mode="before">Before (Base)</button>
                  <button class="tab" data-mode="side">Side-by-Side</button>
                </div>
              </div>
              <div class="viewer">
                <img class="v-diff" src="{b64_diff}" alt="Diff">
                <img class="v-after hidden" src="{b64_after}" alt="After">
                <img class="v-before hidden" src="{b64_before}" alt="Before">
                <div class="v-side hidden">
                  <div><div class="cap">Before (Base)</div><img src="{b64_before}" alt="Before"></div>
                  <div><div class="cap">After (PR)</div><img src="{b64_after}" alt="After"></div>
                </div>
              </div>
            </section>
            """
        )

    bom_items = []
    for a in bom_delta["added"]:
        bom_items.append(f'<li class="add">+ <strong>{a["ref"]}</strong>: {a["value"]} ({a["footprint"]}) {a["lcsc"]}</li>')
    for r in bom_delta["removed"]:
        bom_items.append(f'<li class="rem">- <strong>{r["ref"]}</strong>: {r["value"]} ({r["footprint"]}) {r["lcsc"]}</li>')
    for ch in bom_delta["changed"]:
        bom_items.append(
            f'<li class="chg">~ <strong>{ch["ref"]}</strong>: '
            f'{ch["before"]["value"]} [{ch["before"]["lcsc"]}] → {ch["after"]["value"]} [{ch["after"]["lcsc"]}]</li>'
        )
    bom_html = "<ul>" + "".join(bom_items) + "</ul>" if bom_items else "<p class='muted'>No BOM component changes detected.</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title} — Visual KiCad Diff</title>
<style>
  body {{ margin:0; padding:24px; background:#0d1117; color:#e6edf3; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  .wrap {{ max-width:1300px; margin:0 auto; }}
  h1 {{ margin-top:0; font-size:1.5rem; }}
  .legend {{ color:#8b949e; font-size:0.9rem; margin-bottom:18px; }}
  .legend .g {{ color:#3fb950; font-weight:600; }}
  .legend .r {{ color:#f85149; font-weight:600; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:16px; margin-bottom:20px; }}
  .card-head {{ display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:10px; margin-bottom:12px; }}
  .card-head h2 {{ margin:0; font-size:1.1rem; }}
  .tabs {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center; }}
  .tab {{ background:#21262d; color:#c9d1d9; border:1px solid #30363d; border-radius:6px; padding:5px 10px; cursor:pointer; font-size:0.84rem; }}
  .tab.active {{ border-color:#58a6ff; color:#58a6ff; background:rgba(88,166,255,0.12); }}
  .badge {{ padding:3px 9px; border-radius:999px; font-size:0.78rem; font-weight:600; margin-right:6px; }}
  .badge.changed {{ background:rgba(210,153,34,0.18); color:#d29922; }}
  .badge.same {{ background:rgba(63,185,80,0.15); color:#3fb950; }}
  .viewer img {{ max-width:100%; height:auto; border-radius:6px; border:1px solid #30363d; display:block; margin:0 auto; background:#fff; }}
  .v-side {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
  .cap {{ font-size:0.82rem; color:#8b949e; margin-bottom:4px; text-align:center; }}
  .hidden {{ display:none !important; }}
  .add {{ color:#3fb950; }} .rem {{ color:#f85149; }} .chg {{ color:#d29922; }} .muted {{ color:#8b949e; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{title} — Visual Hardware Diff</h1>
  <div class="legend">Diff Overlay Legend: <span class="g">Green = Added / Changed in PR</span> · <span class="r">Red = Removed from Base</span></div>
  <section class="card">
    <h2>BOM Component Delta</h2>
    {bom_html}
  </section>
  {"".join(cards_html)}
</div>
<script>
document.querySelectorAll(".card[data-idx]").forEach(card => {{
  const tabs = card.querySelectorAll(".tab");
  tabs.forEach(tab => {{
    tab.addEventListener("click", () => {{
      tabs.forEach(t => t.classList.remove("active"));
      tab.classList.add("active");
      const mode = tab.dataset.mode;
      for (const m of ["diff", "after", "before", "side"]) {{
        const el = card.querySelector(".v-" + m);
        if (el) el.classList.toggle("hidden", m !== mode);
      }}
    }});
  }});
}});
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Visual diff between base and head KiCad directories.")
    ap.add_argument("--base-dir", required=True, help="Directory with base branch checkout")
    ap.add_argument("--head-dir", required=True, help="Directory with PR head checkout")
    ap.add_argument("--out-dir", required=True, help="Output directory")
    ap.add_argument("--config", default=None, help="Path to ci/config.yml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    base_snap = render_snapshot(cfg, Path(args.base_dir).resolve(), out_dir / "_base")
    head_snap = render_snapshot(cfg, Path(args.head_dir).resolve(), out_dir / "_head")

    all_labels: List[Tuple[str, Optional[Path], Optional[Path]]] = []
    for k in sorted(set(base_snap["pcb_renders"]) | set(head_snap["pcb_renders"])):
        all_labels.append((k, base_snap["pcb_renders"].get(k), head_snap["pcb_renders"].get(k)))
    for k in sorted(set(base_snap["sch_sheets"]) | set(head_snap["sch_sheets"])):
        all_labels.append((k, base_snap["sch_sheets"].get(k), head_snap["sch_sheets"].get(k)))

    comparisons: List[Dict[str, Any]] = []
    diff_img_dir = out_dir / "images"
    diff_img_dir.mkdir(parents=True, exist_ok=True)

    for idx, (label, p_base, p_head) in enumerate(all_labels):
        safe_name = f"diff-{idx:02d}.png"
        diff_png = diff_img_dir / safe_name
        pct, px = compute_pixel_diff(p_base, p_head, diff_png)
        comparisons.append(
            {
                "label": label,
                "before": p_base,
                "after": p_head,
                "diff": diff_png if diff_png.is_file() else p_head or p_base,
                "changed_pct": pct,
                "changed_pixels": px,
            }
        )

    bom_delta = compute_bom_delta(base_snap["bom_refs"], head_snap["bom_refs"])

    html = build_diff_html(cfg["project"].get("display_name", cfg["project"]["name"]), comparisons, bom_delta)
    (out_dir / "visual-diff.html").write_text(html, encoding="utf-8")

    # Build Markdown summary for PR comment
    md = ["### Visual PCB / Schematic Diff & BOM Delta", ""]
    if comparisons:
        md.append("| View | Visual Change |")
        md.append("| :--- | :---: |")
        for c in comparisons:
            status = f"🟡 `{c['changed_pct']:.2f}%` pixels changed" if c["changed_pct"] > 0 else "⚪ Unchanged"
            md.append(f"| {c['label']} | {status} |")
        md.append("")
    else:
        md.append("_No comparable PCB/Schematic views rendered._\n")

    n_add = len(bom_delta["added"])
    n_rem = len(bom_delta["removed"])
    n_chg = len(bom_delta["changed"])
    md.append(
        f"**BOM Delta:** `+{n_add}` added · `-{n_rem}` removed · `~{n_chg}` modified"
    )
    if n_add + n_rem + n_chg > 0:
        md.append("\n<details><summary>View BOM Component Changes</summary>\n")
        for a in bom_delta["added"][:20]:
            md.append(f"- ➕ `{a['ref']}`: {a['value']} (`{a['footprint']}`) `{a['lcsc']}`")
        for r in bom_delta["removed"][:20]:
            md.append(f"- ➖ `{r['ref']}`: {r['value']} (`{r['footprint']}`) `{r['lcsc']}`")
        for ch in bom_delta["changed"][:20]:
            md.append(
                f"- ✏️ `{ch['ref']}`: {ch['before']['value']} (`{ch['before']['lcsc']}`) → "
                f"{ch['after']['value']} (`{ch['after']['lcsc']}`)"
            )
        md.append("\n</details>")

    md.append(
        "\n> 📦 Download the **`visual-diff`** workflow artifact and open `visual-diff.html` "
        "to inspect interactive Before / After / Overlay diffs."
    )

    summary_md = "\n".join(md) + "\n"
    (out_dir / "diff-summary.md").write_text(summary_md, encoding="utf-8")
    (out_dir / "diff-summary.json").write_text(
        json.dumps(
            {
                "views": [
                    {"label": c["label"], "changed_pct": c["changed_pct"], "changed_pixels": c["changed_pixels"]}
                    for c in comparisons
                ],
                "bom_delta": bom_delta,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(summary_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
