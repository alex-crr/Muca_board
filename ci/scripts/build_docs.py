#!/usr/bin/env python3
"""Generate KiCad 10 documentation assets (Schematic PDF/SVG, 2D SVG, 3D PNG, STEP)
and compile a Mintlify-inspired static GitHub Pages site combining manual docs (`docs/*.md`)
with natively integrated hardware views (Priced BOM, Interactive BOM, Renders, Schematic, Reports, Downloads).

Supports two modes:
1. Full build (default): runs kicad-cli exports in parallel and builds the complete site + `_meta/`.
2. Fast docs-only rebuild (`--reuse-site <dir>`): skips KiCad completely, reuses existing hardware
   artifacts (`renders/`, `files/`, `part-images/`, `ibom-raw.html`, `_meta/`), and recompiles
   all manual Markdown docs + HTML shell in seconds.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import markdown

from config_loader import kicad_env, load_config, resolve_project_path
from jlc_bom_pricing import render_bom_fragment


def _run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"[DOCS] {' '.join(cmd)}")
    res = subprocess.run(cmd, env=kicad_env(), capture_output=True, text=True)
    if check and res.returncode != 0:
        print(res.stdout)
        print(res.stderr, file=sys.stderr)
        raise RuntimeError(f"Command failed ({res.returncode}): {' '.join(cmd)}")
    return res


def _render_single_3d(
    pcb_path: Path, renders_dir: Path, view: Dict[str, Any], w: str, h: str, quality: str
) -> Tuple[str, Optional[Path]]:
    vname = view["name"]
    out_png = renders_dir / f"pcb-3d-{vname}.png"
    cmd = [
        "kicad-cli",
        "pcb",
        "render",
        "--side",
        view.get("side", "top"),
        "--width",
        w,
        "--height",
        h,
        "--background",
        "transparent",
        "--quality",
        quality,
        "--perspective",
        "--output",
        str(out_png),
    ]
    if view.get("rotate"):
        cmd.extend(["--rotate", str(view["rotate"])])
    cmd.append(str(pcb_path))
    res = _run(cmd, check=False)
    if res.returncode != 0 or not out_png.is_file():
        print(f"::warning title=3D Render Fallback ({vname})::Quality '{quality}' failed; retrying with 'basic'.")
        cmd[cmd.index(quality)] = "basic"
        res2 = _run(cmd, check=False)
        if res2.returncode != 0 or not out_png.is_file():
            print(f"::warning title=3D Render Failed ({vname})::Could not render 3D view '{vname}'.")
            return vname, None
    return vname, out_png


def export_kicad_docs(
    cfg: Dict[str, Any], out_dir: Path, prefix: str, skip_3d: bool = False
) -> Dict[str, Any]:
    """Export schematic PDF/SVG, 2D PCB SVGs, 3D raytraced renders, and STEP model (in parallel)."""
    sch_path = resolve_project_path(cfg, "schematic")
    pcb_path = resolve_project_path(cfg, "pcb")

    assets_dir = out_dir / "generated"
    sch_svg_dir = assets_dir / "schematic_svg"
    renders_dir = assets_dir / "renders"
    for d in (assets_dir, sch_svg_dir, renders_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 1. Fast 2D exports (Schematic PDF + SVGs, 2D PCB SVGs)
    sch_pdf = out_dir / f"{prefix}-schematic.pdf"
    top_svg = renders_dir / "pcb-2d-top.svg"
    bot_svg = renders_dir / "pcb-2d-bottom.svg"

    fast_cmds = [
        ["kicad-cli", "sch", "export", "pdf", "--output", str(sch_pdf), str(sch_path)],
        ["kicad-cli", "sch", "export", "svg", "--output", str(sch_svg_dir), str(sch_path)],
        [
            "kicad-cli", "pcb", "export", "svg",
            "--layers", "F.Cu,F.Silkscreen,F.Mask,Edge.Cuts",
            "--mode-single", "--exclude-drawing-sheet", "--page-size-mode", "2", "--check-zones",
            "--output", str(top_svg), str(pcb_path),
        ],
        [
            "kicad-cli", "pcb", "export", "svg",
            "--layers", "B.Cu,B.Silkscreen,B.Mask,Edge.Cuts",
            "--mirror", "--mode-single", "--exclude-drawing-sheet", "--page-size-mode", "2", "--check-zones",
            "--output", str(bot_svg), str(pcb_path),
        ],
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(_run, cmd, True) for cmd in fast_cmds]
        for fut in as_completed(futs):
            fut.result()

    # 2. Heavy 3D renders + STEP export concurrently
    r_cfg = cfg.get("renders", {})
    w = str(r_cfg.get("width", 1600))
    h = str(r_cfg.get("height", 1200))
    quality = str(r_cfg.get("quality", "raytrace_post"))
    views = r_cfg.get("views", [{"name": "top", "side": "top", "rotate": ""}])
    rendered_map: Dict[str, Path] = {}
    step_file = out_dir / f"{prefix}-3d.step"

    if not skip_3d:
        def _export_step() -> Optional[Path]:
            res = _run(
                [
                    "kicad-cli", "pcb", "export", "step",
                    "--force", "--subst-models", "--no-dnp",
                    "--output", str(step_file), str(pcb_path),
                ],
                check=False,
            )
            if res.returncode != 0 or not step_file.is_file():
                print("::warning title=STEP Export Failed::kicad-cli pcb export step did not produce a STEP file.")
                return None
            return step_file

        with ThreadPoolExecutor(max_workers=4) as pool:
            step_fut = pool.submit(_export_step)
            render_futs = [
                pool.submit(_render_single_3d, pcb_path, renders_dir, v, w, h, quality)
                for v in views
            ]
            for fut in as_completed(render_futs):
                vname, path = fut.result()
                if path:
                    rendered_map[vname] = path
            step_fut.result()

    # Preserve configured view order
    rendered_3d: List[Tuple[str, Path]] = [
        (v["name"], rendered_map[v["name"]]) for v in views if v["name"] in rendered_map
    ]

    renders_zip = out_dir / f"{prefix}-renders.zip"
    with zipfile.ZipFile(renders_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(renders_dir.iterdir()):
            if f.is_file():
                zf.write(f, arcname=f.name)

    return {
        "sch_pdf": sch_pdf,
        "sch_svgs": sorted(sch_svg_dir.glob("*.svg")),
        "top_svg": top_svg,
        "bot_svg": bot_svg,
        "rendered_3d": rendered_3d,
        "step_file": step_file if step_file.is_file() else None,
        "renders_zip": renders_zip,
    }


def _parse_frontmatter(raw: str) -> Tuple[Dict[str, str], str]:
    """Parse optional YAML-like frontmatter (--- key: value ---) at top of a Markdown file."""
    meta: Dict[str, str] = {}
    if raw.startswith("---\n"):
        end = raw.find("\n---", 4)
        if end != -1:
            block = raw[4:end]
            body = raw[end + 4 :].lstrip("\n")
            for line in block.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip().strip('"').strip("'")
            return meta, body
    return meta, raw


def _discover_manual_pages(manual_dir: Path) -> List[Dict[str, Any]]:
    """Discover all user-authored Markdown files in docs/ (excluding README.md)."""
    pages: List[Dict[str, Any]] = []
    if not manual_dir.is_dir():
        return pages

    for md_file in sorted(manual_dir.glob("*.md")):
        if md_file.name.lower() in ("readme.md",):
            continue
        raw = md_file.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        if meta.get("publish", "true").lower() == "false":
            continue
        slug = "index" if md_file.stem.lower() == "index" else md_file.stem.lower()
        title = meta.get("title", "")
        if not title:
            m = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
            title = (
                m.group(1).strip()
                if m
                else md_file.stem.replace("-", " ").replace("_", " ").title()
            )
        default_order = 0 if slug == "index" else (90 if slug == "changelog" else 50)
        try:
            order = int(meta.get("order", default_order))
        except ValueError:
            order = default_order
        pages.append(
            {
                "slug": slug,
                "href": f"{slug}.html",
                "title": title,
                "order": order,
                "body_md": body,
            }
        )

    pages.sort(key=lambda p: (p["order"], p["title"].lower()))
    return pages


def _site_shell(
    cfg: Dict[str, Any],
    site_title: str,
    version: str,
    version_id: str,
    commit_sha: str,
    root_rel: str,
    page_title: str,
    active_href: str,
    manual_nav: List[Dict[str, Any]],
    auto_nav: List[Tuple[str, str]],
    content_html: str,
    layout_mode: str = "prose",  # "prose" | "wide" | "fullbleed"
) -> str:
    docs_cfg = cfg.get("docs", {})
    accent = docs_cfg.get("accent_color", "#10b981")
    ext_url = docs_cfg.get("external_docs_url", "")
    ext_label = docs_cfg.get("external_docs_label", "Main Docs")

    def _nav_links(items: List[Tuple[str, str]]) -> str:
        out = []
        for label, href in items:
            cls = ' class="active"' if href == active_href else ""
            out.append(f'<a href="{href}"{cls}>{html.escape(label)}</a>')
        return "\n".join(out)

    manual_links = _nav_links([(p["title"], p["href"]) for p in manual_nav])
    auto_links = _nav_links(auto_nav)
    ext_link_html = (
        f'<a class="top-link" href="{html.escape(ext_url)}" target="_blank" rel="noopener noreferrer">'
        f"{html.escape(ext_label)} ↗</a>"
        if ext_url
        else ""
    )

    main_cls = f"layout-{layout_mode}"
    sha_badge = f" · {commit_sha[:7]}" if commit_sha else ""
    ver_label = f"{version}{sha_badge}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(page_title)} — {html.escape(site_title)}</title>
<script>
  (function() {{
    const saved = localStorage.getItem("espin-theme");
    const theme = saved || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    document.documentElement.setAttribute("data-theme", theme);
  }})();
</script>
<style>
  :root, [data-theme="dark"] {{
    --bg: #09090b;
    --bg-translucent: rgba(9, 9, 11, 0.82);
    --surface: #111113;
    --surface-2: #18181b;
    --hover: rgba(255, 255, 255, 0.04);
    --border: #27272a;
    --border-strong: #3f3f46;
    --text: #fafafa;
    --text-secondary: #d4d4d8;
    --muted: #a1a1aa;
    --accent: {accent};
    --accent-soft: color-mix(in srgb, {accent} 13%, transparent);
    --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    --sans: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  [data-theme="light"] {{
    --bg: #ffffff;
    --bg-translucent: rgba(255, 255, 255, 0.86);
    --surface: #fafafa;
    --surface-2: #f4f4f5;
    --hover: rgba(0, 0, 0, 0.035);
    --border: #e4e4e7;
    --border-strong: #d4d4d8;
    --text: #09090b;
    --text-secondary: #27272a;
    --muted: #71717a;
    --accent: color-mix(in srgb, {accent} 88%, #000000);
    --accent-soft: color-mix(in srgb, {accent} 12%, transparent);
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: var(--bg); color: var(--text); font-family: var(--sans); line-height: 1.65; -webkit-font-smoothing: antialiased; }}

  .topbar {{
    position: sticky; top: 0; z-index: 40; height: 56px;
    background: var(--bg-translucent); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 20px; gap: 12px;
  }}
  .topbar-left {{ display: flex; align-items: center; gap: 10px; min-width: 0; }}
  .menu-btn {{
    display: none; background: transparent; border: 1px solid var(--border); color: var(--text);
    width: 34px; height: 34px; border-radius: 8px; cursor: pointer; align-items: center; justify-content: center;
  }}
  .brand-link {{
    display: inline-flex; align-items: center; gap: 9px;
    font-weight: 650; font-size: 0.98rem; letter-spacing: -0.015em;
    color: var(--text); text-decoration: none; white-space: nowrap;
  }}
  .brand-dot {{
    width: 9px; height: 9px; border-radius: 50%; background: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-soft);
  }}
  .ver-select-wrap {{ position: relative; display: inline-flex; align-items: center; }}
  .ver-select {{
    appearance: none; -webkit-appearance: none;
    font-family: var(--mono); font-size: 0.74rem; font-weight: 500;
    padding: 4px 24px 4px 10px; border-radius: 999px;
    background: var(--surface-2); border: 1px solid var(--border); color: var(--text-secondary);
    cursor: pointer; max-width: 210px;
  }}
  .ver-select:hover {{ border-color: var(--border-strong); color: var(--text); }}
  .ver-select:focus {{ outline: none; border-color: var(--accent); }}
  .ver-caret {{
    position: absolute; right: 8px; pointer-events: none; color: var(--muted);
    display: inline-flex; align-items: center;
  }}

  .topbar-right {{ display: flex; align-items: center; gap: 10px; }}
  .top-link {{
    font-size: 0.84rem; font-weight: 500; color: var(--muted); text-decoration: none;
    padding: 5px 10px; border-radius: 7px; transition: color 0.15s, background 0.15s; white-space: nowrap;
  }}
  .top-link:hover {{ color: var(--text); background: var(--hover); }}
  .theme-btn {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 34px; height: 34px; border-radius: 8px;
    background: transparent; border: 1px solid var(--border); color: var(--muted);
    cursor: pointer; transition: color 0.15s, border-color 0.15s;
  }}
  .theme-btn:hover {{ color: var(--text); border-color: var(--border-strong); }}

  .shell {{ display: flex; min-height: calc(100vh - 56px); }}
  aside.sidebar {{
    width: 256px; flex-shrink: 0;
    border-right: 1px solid var(--border);
    background: var(--bg);
    padding: 20px 14px;
    position: sticky; top: 56px; height: calc(100vh - 56px); overflow-y: auto;
  }}
  .nav-group-title {{
    font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--muted); padding: 0 10px; margin: 18px 0 6px;
  }}
  .nav-group-title:first-child {{ margin-top: 0; }}
  aside.sidebar a {{
    display: flex; align-items: center; padding: 7px 10px; border-radius: 8px;
    color: var(--muted); text-decoration: none; font-size: 0.875rem; font-weight: 500;
    margin-bottom: 2px; transition: background 0.12s, color 0.12s;
  }}
  aside.sidebar a:hover {{ background: var(--hover); color: var(--text); }}
  aside.sidebar a.active {{
    background: var(--accent-soft); color: var(--accent); font-weight: 600;
  }}

  main {{ flex: 1; min-width: 0; }}
  main.layout-prose {{ padding: 36px 48px 64px; max-width: 920px; }}
  main.layout-wide {{ padding: 28px 36px 48px; max-width: 1320px; }}
  main.layout-fullbleed {{ padding: 0; display: flex; flex-direction: column; height: calc(100vh - 56px); }}

  h1 {{ font-size: 1.85rem; font-weight: 650; letter-spacing: -0.025em; margin: 0 0 16px; line-height: 1.25; }}
  h2 {{ font-size: 1.3rem; font-weight: 600; letter-spacing: -0.015em; margin: 32px 0 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border); }}
  h3 {{ font-size: 1.05rem; font-weight: 600; margin: 22px 0 8px; }}
  p {{ color: var(--text-secondary); margin: 0 0 14px; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  img {{ max-width: 100%; height: auto; border-radius: 10px; border: 1px solid var(--border); }}
  pre {{
    background: var(--surface); border: 1px solid var(--border);
    padding: 14px 16px; border-radius: 10px; overflow-x: auto; font-size: 0.85rem;
  }}
  code {{
    font-family: var(--mono); background: var(--surface-2);
    padding: 2px 6px; border-radius: 5px; font-size: 0.84em;
  }}
  pre code {{ background: none; padding: 0; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 0.88rem; }}
  th, td {{ border-bottom: 1px solid var(--border); padding: 10px 12px; text-align: left; }}
  th {{ color: var(--muted); font-size: 0.76rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; background: var(--surface); }}

  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin: 16px 0; }}
  .card {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px;
  }}
  .card img {{ background: #101216; display: block; margin: 0 auto; border-radius: 8px; border: none; }}
  .card .cap {{ color: var(--muted); font-size: 0.82rem; margin-top: 10px; text-align: center; font-weight: 500; }}

  .btn {{
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--accent); color: #ffffff !important; text-decoration: none !important;
    padding: 8px 14px; border-radius: 8px; font-weight: 600; font-size: 0.85rem; margin: 4px 6px 4px 0;
    transition: filter 0.15s;
  }}
  .btn:hover {{ filter: brightness(1.06); }}
  .btn.sec {{
    background: var(--surface); border: 1px solid var(--border); color: var(--text) !important; font-weight: 500;
  }}
  .btn.sec:hover {{ border-color: var(--border-strong); background: var(--surface-2); }}

  .alert-banner {{
    background: rgba(239, 68, 68, 0.09);
    border: 1px solid rgba(239, 68, 68, 0.42);
    border-left: 4px solid #ef4444;
    border-radius: 10px;
    padding: 13px 16px;
    margin-bottom: 22px;
    font-size: 0.88rem;
  }}
  .alert-banner strong {{ color: #ef4444; }}
  .alert-banner a {{ color: #ef4444; text-decoration: underline; font-weight: 600; }}

  .svg-box {{
    background: #ffffff; border-radius: 10px; padding: 12px; margin-bottom: 20px; border: 1px solid var(--border);
  }}
  .svg-box img {{ border: none; }}

  .ibom-bar {{
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 10px 20px; border-bottom: 1px solid var(--border); background: var(--surface);
    font-size: 0.86rem; flex-shrink: 0;
  }}
  .ibom-frame {{ flex: 1; width: 100%; border: none; display: block; background: #111; }}

  @media (max-width: 900px) {{
    .menu-btn {{ display: inline-flex; }}
    aside.sidebar {{
      position: fixed; left: -270px; top: 56px; z-index: 50; width: 260px;
      height: calc(100vh - 56px); transition: left 0.2s ease; box-shadow: 4px 0 24px rgba(0,0,0,0.35);
    }}
    aside.sidebar.open {{ left: 0; }}
    main.layout-prose, main.layout-wide {{ padding: 20px 18px 40px; }}
  }}
</style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-left">
      <button type="button" class="menu-btn" id="menuToggle" aria-label="Toggle navigation">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
      </button>
      <a class="brand-link" href="index.html">
        <span class="brand-dot"></span>
        <span>{html.escape(site_title)}</span>
      </a>
      <div class="ver-select-wrap">
        <select id="versionSwitcher" class="ver-select" aria-label="Select version or branch" data-current="{html.escape(version_id)}" data-root-rel="{html.escape(root_rel)}" data-page="{html.escape(active_href)}">
          <option value="{html.escape(version_id)}">{html.escape(ver_label)}</option>
        </select>
        <span class="ver-caret">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><polyline points="6 9 12 15 18 9"/></svg>
        </span>
      </div>
    </div>
    <div class="topbar-right">
      {ext_link_html}
      <button type="button" class="theme-btn" id="themeToggle" aria-label="Toggle light/dark theme" title="Switch theme">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>
      </button>
    </div>
  </header>

  <div class="shell">
    <aside class="sidebar" id="sidebar">
      <div class="nav-group-title">Documentation</div>
      {manual_links}
      <div class="nav-group-title">Hardware</div>
      {auto_links}
    </aside>
    <main class="{main_cls}">
      {content_html}
    </main>
  </div>

<script>
  document.getElementById("themeToggle").addEventListener("click", () => {{
    const cur = document.documentElement.getAttribute("data-theme") || "dark";
    const nxt = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", nxt);
    localStorage.setItem("espin-theme", nxt);
  }});
  document.getElementById("menuToggle").addEventListener("click", () => {{
    document.getElementById("sidebar").classList.toggle("open");
  }});

  // Dynamic multi-version / branch switcher populated from root versions.json
  (async function() {{
    const sel = document.getElementById("versionSwitcher");
    if (!sel) return;
    const rootRel = sel.dataset.rootRel || "..";
    const currentId = sel.dataset.current || "";
    const pageName = sel.dataset.page || "index.html";
    try {{
      const resp = await fetch(rootRel + "/versions.json", {{ cache: "no-cache" }});
      if (!resp.ok) return;
      const data = await resp.json();
      const versions = data.versions || [];
      if (!versions.length) return;

      const releases = versions.filter(v => v.type === "latest" || v.type === "tag");
      const branches = versions.filter(v => v.type === "branch");
      const others = versions.filter(v => !["latest", "tag", "branch"].includes(v.type));

      sel.innerHTML = "";
      function addGroup(label, items) {{
        if (!items.length) return;
        const og = document.createElement("optgroup");
        og.label = label;
        for (const v of items) {{
          const opt = document.createElement("option");
          opt.value = v.id;
          const sha = v.sha ? ` · ${{v.sha.slice(0, 7)}}` : "";
          opt.textContent = `${{v.label}}${{sha}}`;
          if (v.id === currentId) opt.selected = true;
          og.appendChild(opt);
        }}
        sel.appendChild(og);
      }}
      addGroup("Releases", releases);
      addGroup("Branches", branches);
      addGroup("Other", others);

      sel.addEventListener("change", () => {{
        const target = sel.value;
        if (target && target !== currentId) {{
          window.location.href = `${{rootRel}}/${{target}}/${{pageName}}`;
        }}
      }});
    }} catch (_e) {{
      // Standalone/local preview without root versions.json
    }}
  }})();
</script>
</body>
</html>
"""


def _compute_root_rel(version_id: str) -> str:
    """Compute relative path from `<pages_root>/<version_id>/` back to `<pages_root>`."""
    clean = version_id.strip("/")
    if not clean or clean == ".":
        return "."
    depth = len(clean.split("/"))
    return "/".join([".."] * depth)


def build_site(
    cfg: Dict[str, Any],
    site_dir: Path,
    version: str,
    version_id: str,
    commit_sha: str,
    copied_3d: List[Tuple[str, str]],
    copied_2d: List[Tuple[str, str]],
    copied_sch_svgs: List[Tuple[str, str]],
    dl_entries: List[Tuple[str, str, str]],
    has_ibom: bool,
    bom_payload: Optional[Dict[str, Any]],
    checks_md_text: Optional[str],
    checks_json_data: Optional[Dict[str, Any]],
    sanity_md_text: Optional[str],
) -> None:
    """Render all HTML pages into `site_dir` (already populated with static files/renders/_meta)."""
    root = Path(cfg["_root"])
    manual_dir = root / cfg.get("docs", {}).get("manual_dir", "docs")
    site_title = cfg["project"].get("display_name", cfg["project"]["name"])
    root_rel = _compute_root_rel(version_id)

    # Copy user-managed static assets from docs/assets or docs/images
    for sub in ("assets", "images", "img"):
        src = manual_dir / sub
        if src.is_dir():
            shutil.copytree(src, site_dir / sub, dirs_exist_ok=True)

    has_bom_calc = bom_payload is not None
    manual_pages = _discover_manual_pages(manual_dir)
    if not any(p["slug"] == "index" for p in manual_pages):
        manual_pages.insert(
            0,
            {
                "slug": "index",
                "href": "index.html",
                "title": "Overview",
                "order": 0,
                "body_md": f"# {site_title}\n\nAdd your own documentation in `docs/index.md`.",
            },
        )

    # Prominent error banner if ERC or DRC errors > 0
    error_banner_html = ""
    checks_nav_label = "ERC & DRC Reports"
    if checks_json_data:
        erc_e = int(checks_json_data.get("erc", {}).get("errors", 0))
        drc_e = int(checks_json_data.get("drc", {}).get("errors", 0))
        if erc_e > 0 or drc_e > 0:
            checks_nav_label = f"ERC & DRC ({erc_e + drc_e} err)"
            error_banner_html = (
                '<div class="alert-banner">'
                f"<strong>CAUTION — {erc_e + drc_e} KiCad ERROR(S) DETECTED "
                f"({erc_e} ERC, {drc_e} DRC)</strong><br>"
                'This build completed without blocking. Review the <a href="checks.html">ERC &amp; DRC Report</a> '
                "before manufacturing or ordering PCBs."
                "</div>\n"
            )

    auto_nav: List[Tuple[str, str]] = [
        ("PCB Renders & 3D", "renders.html"),
        ("Schematic", "schematic.html"),
    ]
    if has_bom_calc:
        auto_nav.append(("Priced BOM & Stock", "bom.html"))
    if has_ibom:
        auto_nav.append(("Interactive BOM (iBOM)", "ibom.html"))
    auto_nav.append((checks_nav_label, "checks.html"))
    auto_nav.append(("Downloads", "downloads.html"))

    md_conv = markdown.Markdown(extensions=["tables", "fenced_code", "toc"])

    # 1. Manual Markdown pages
    for page in manual_pages:
        md_conv.reset()
        body_html = md_conv.convert(page["body_md"])
        if page["slug"] == "index":
            body_html = error_banner_html + body_html
            if copied_3d:
                hero = ['<h2>Board Preview</h2><div class="grid">']
                for name, rel in copied_3d[:2]:
                    hero.append(
                        f'<div class="card"><img src="{rel}" alt="{html.escape(name)}">'
                        f'<div class="cap">{html.escape(name.title())}</div></div>'
                    )
                hero.append("</div><p>")
                if has_bom_calc:
                    hero.append('<a class="btn" href="bom.html">Priced BOM &amp; Stock</a>')
                if has_ibom:
                    hero.append('<a class="btn sec" href="ibom.html">Interactive BOM (iBOM)</a>')
                hero.append('<a class="btn sec" href="downloads.html">Manufacturing Downloads</a></p>')
                body_html += "\n".join(hero)

        out_html = _site_shell(
            cfg, site_title, version, version_id, commit_sha, root_rel,
            page["title"], page["href"], manual_pages, auto_nav, body_html, "prose",
        )
        (site_dir / page["href"]).write_text(out_html, encoding="utf-8")

    # 2. Integrated Priced BOM page
    if bom_payload is not None:
        bom_inner = error_banner_html + render_bom_fragment(bom_payload)
        (site_dir / "bom.html").write_text(
            _site_shell(
                cfg, site_title, version, version_id, commit_sha, root_rel,
                "Priced BOM & Stock", "bom.html", manual_pages, auto_nav, bom_inner, "wide",
            ),
            encoding="utf-8",
        )

    # 3. Integrated Interactive BOM page
    if has_ibom:
        ibom_shell_body = """
        <div class="ibom-bar">
          <div><strong>Interactive HTML BOM</strong> <span style="color:var(--muted);margin-left:8px">Click components to highlight pads &amp; assembly locations</span></div>
          <a class="btn sec" style="margin:0;padding:5px 11px;font-size:0.8rem" href="ibom-raw.html" target="_blank" rel="noopener noreferrer">Open Fullscreen ↗</a>
        </div>
        <iframe class="ibom-frame" src="ibom-raw.html" title="Interactive HTML BOM"></iframe>
        """
        (site_dir / "ibom.html").write_text(
            _site_shell(
                cfg, site_title, version, version_id, commit_sha, root_rel,
                "Interactive BOM (iBOM)", "ibom.html", manual_pages, auto_nav, ibom_shell_body, "fullbleed",
            ),
            encoding="utf-8",
        )

    # 4. Renders & 3D page
    r_html = ["<h1>PCB Renders &amp; 3D Model</h1>"]
    step_dl = next((href for lbl, href, _ in dl_entries if "STEP" in lbl), None)
    if step_dl:
        r_html.append(f'<p><a class="btn" href="{step_dl}">Download 3D STEP Model</a></p>')
    if copied_3d:
        r_html.append('<h2>3D Renders</h2><div class="grid">')
        for name, rel in copied_3d:
            r_html.append(
                f'<div class="card"><img src="{rel}" alt="{html.escape(name)}">'
                f'<div class="cap">{html.escape(name.replace("-", " ").title())}</div></div>'
            )
        r_html.append("</div>")
    if copied_2d:
        r_html.append('<h2>2D Copper &amp; Silkscreen</h2><div class="grid">')
        for label, rel in copied_2d:
            r_html.append(
                f'<div class="card"><img src="{rel}" alt="{html.escape(label)}">'
                f'<div class="cap">{html.escape(label)}</div></div>'
            )
        r_html.append("</div>")
    (site_dir / "renders.html").write_text(
        _site_shell(
            cfg, site_title, version, version_id, commit_sha, root_rel,
            "PCB Renders & 3D", "renders.html", manual_pages, auto_nav, "\n".join(r_html), "wide",
        ),
        encoding="utf-8",
    )

    # 5. Schematic page
    s_html = ["<h1>Schematic</h1>"]
    pdf_dl = next((href for lbl, href, _ in dl_entries if "Schematic PDF" in lbl), None)
    if pdf_dl:
        s_html.append(
            f'<p><a class="btn" href="{pdf_dl}" target="_blank" rel="noopener noreferrer">Open Schematic PDF ↗</a></p>'
        )
    for title, rel in copied_sch_svgs:
        s_html.append(
            f'<h3>{html.escape(title)}</h3><div class="svg-box"><img src="{rel}" alt="{html.escape(title)}"></div>'
        )
    (site_dir / "schematic.html").write_text(
        _site_shell(
            cfg, site_title, version, version_id, commit_sha, root_rel,
            "Schematic", "schematic.html", manual_pages, auto_nav, "\n".join(s_html), "wide",
        ),
        encoding="utf-8",
    )

    # 6. Checks & Sanity page
    combined_checks_md = ""
    if checks_md_text:
        combined_checks_md += checks_md_text + "\n\n"
    if sanity_md_text:
        combined_checks_md += "---\n\n" + sanity_md_text + "\n"
    if combined_checks_md.strip():
        md_conv.reset()
        checks_html = error_banner_html + md_conv.convert(combined_checks_md)
    else:
        checks_html = (
            error_banner_html + "<h1>ERC &amp; DRC Reports</h1><p>No check report was attached to this build.</p>"
        )
    (site_dir / "checks.html").write_text(
        _site_shell(
            cfg, site_title, version, version_id, commit_sha, root_rel,
            "ERC & DRC Reports", "checks.html", manual_pages, auto_nav, checks_html, "prose",
        ),
        encoding="utf-8",
    )

    # 7. Downloads page
    d_html = [
        error_banner_html,
        "<h1>Manufacturing &amp; Design Downloads</h1>",
        "<table><thead><tr><th>Artifact</th><th>Size</th><th>Download</th></tr></thead><tbody>",
    ]
    for label, href, size in dl_entries:
        d_html.append(
            f'<tr><td>{html.escape(label)}</td><td>{size}</td>'
            f'<td><a class="btn sec" style="margin:0;padding:5px 12px" href="{href}">Download</a></td></tr>'
        )
    d_html.append("</tbody></table>")
    (site_dir / "downloads.html").write_text(
        _site_shell(
            cfg, site_title, version, version_id, commit_sha, root_rel,
            "Downloads", "downloads.html", manual_pages, auto_nav, "\n".join(d_html), "prose",
        ),
        encoding="utf-8",
    )

    (site_dir / ".nojekyll").write_text("", encoding="utf-8")
    print(f"[DOCS] Built Mintlify-styled site at {site_dir} (version_id={version_id}, {len(manual_pages)} manual page(s)).")


def main() -> int:
    ap = argparse.ArgumentParser(description="Export KiCad docs and build static GitHub Pages site.")
    ap.add_argument("--out-dir", required=True, help="Output directory for generated docs & site")
    ap.add_argument("--version", default="dev", help="Display version label (tag or branch)")
    ap.add_argument("--version-id", default=".", help="Published Pages path id (e.g. main, v1.0.0, branches/foo)")
    ap.add_argument("--commit-sha", default="", help="Git commit SHA")
    ap.add_argument("--config", default=None, help="Path to ci/config.yml")
    ap.add_argument("--jlcpcb-dir", default=None, help="Directory with JLCPCB outputs")
    ap.add_argument("--ibom", default=None, help="Path to generated ibom.html")
    ap.add_argument("--bom-json", default=None, help="Path to bom-priced.json for native docs BOM page")
    ap.add_argument("--part-images-dir", default=None, help="Directory with cached part thumbnails")
    ap.add_argument("--bom-csv", default=None, help="Path to generated bom-priced.csv")
    ap.add_argument("--checks-md", default=None, help="Path to checks-summary.md")
    ap.add_argument("--checks-json", default=None, help="Path to checks-summary.json")
    ap.add_argument("--sanity-md", default=None, help="Path to sanity-summary.md")
    ap.add_argument("--reuse-site", default=None, help="Fast docs-only mode: reuse hardware artifacts from an existing site dir")
    ap.add_argument("--skip-3d", action="store_true", help="Skip 3D renders and STEP export")
    args = ap.parse_args()

    cfg = load_config(args.config)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    site_dir = out_dir / "site"

    if args.reuse_site:
        # Fast docs-only rebuild: NO KiCad needed!
        reuse = Path(args.reuse_site).resolve()
        if not reuse.is_dir():
            raise FileNotFoundError(f"--reuse-site directory not found: {reuse}")
        if site_dir.exists():
            shutil.rmtree(site_dir)
        site_dir.mkdir(parents=True, exist_ok=True)

        for sub in ("files", "renders", "part-images", "_meta"):
            src = reuse / sub
            if src.is_dir():
                shutil.copytree(src, site_dir / sub, dirs_exist_ok=True)
        if (reuse / "ibom-raw.html").is_file():
            shutil.copy2(reuse / "ibom-raw.html", site_dir / "ibom-raw.html")

        meta_dir = site_dir / "_meta"
        manifest = {}
        if (meta_dir / "manifest.json").is_file():
            manifest = json.loads((meta_dir / "manifest.json").read_text(encoding="utf-8"))

        # Allow optional BOM JSON refresh during fast rebuild
        bom_payload = None
        bom_src = Path(args.bom_json) if args.bom_json else (meta_dir / "bom-priced.json")
        if bom_src.is_file():
            bom_payload = json.loads(bom_src.read_text(encoding="utf-8"))
            if args.bom_json:
                meta_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bom_src, meta_dir / "bom-priced.json")
        if args.part_images_dir and Path(args.part_images_dir).is_dir():
            shutil.copytree(Path(args.part_images_dir), site_dir / "part-images", dirs_exist_ok=True)

        checks_md_text = (meta_dir / "checks-summary.md").read_text(encoding="utf-8") if (meta_dir / "checks-summary.md").is_file() else None
        checks_json_data = json.loads((meta_dir / "checks-summary.json").read_text(encoding="utf-8")) if (meta_dir / "checks-summary.json").is_file() else None
        sanity_md_text = (meta_dir / "sanity-summary.md").read_text(encoding="utf-8") if (meta_dir / "sanity-summary.md").is_file() else None

        build_site(
            cfg=cfg,
            site_dir=site_dir,
            version=args.version or manifest.get("version", "dev"),
            version_id=args.version_id,
            commit_sha=args.commit_sha or manifest.get("commit_sha", ""),
            copied_3d=[tuple(x) for x in manifest.get("copied_3d", [])],
            copied_2d=[tuple(x) for x in manifest.get("copied_2d", [])],
            copied_sch_svgs=[tuple(x) for x in manifest.get("copied_sch_svgs", [])],
            dl_entries=[tuple(x) for x in manifest.get("dl_entries", [])],
            has_ibom=(site_dir / "ibom-raw.html").is_file(),
            bom_payload=bom_payload,
            checks_md_text=checks_md_text,
            checks_json_data=checks_json_data,
            sanity_md_text=sanity_md_text,
        )
        return 0

    # Full build mode
    prefix = f"{cfg['project']['name']}-{args.version}"
    docs_assets = export_kicad_docs(cfg, out_dir, prefix, skip_3d=args.skip_3d)

    if site_dir.exists():
        shutil.rmtree(site_dir)
    files_dir = site_dir / "files"
    img_dir = site_dir / "renders"
    meta_dir = site_dir / "_meta"
    for d in (files_dir, img_dir, meta_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Copy cached product thumbnails if present
    if args.part_images_dir and Path(args.part_images_dir).is_dir():
        shutil.copytree(Path(args.part_images_dir), site_dir / "part-images", dirs_exist_ok=True)

    copied_3d: List[Tuple[str, str]] = []
    for name, p in docs_assets.get("rendered_3d", []):
        if p and p.is_file():
            shutil.copy2(p, img_dir / p.name)
            copied_3d.append((name, f"renders/{p.name}"))

    copied_2d: List[Tuple[str, str]] = []
    for label, key in (("2D Top View", "top_svg"), ("2D Bottom View", "bot_svg")):
        p = docs_assets.get(key)
        if p and p.is_file():
            shutil.copy2(p, img_dir / p.name)
            copied_2d.append((label, f"renders/{p.name}"))

    copied_sch_svgs: List[Tuple[str, str]] = []
    for svg in docs_assets.get("sch_svgs", []):
        dst = img_dir / f"sch-{svg.name}"
        shutil.copy2(svg, dst)
        copied_sch_svgs.append((svg.stem, f"renders/{dst.name}"))

    downloads: List[Tuple[str, Path]] = []
    if args.jlcpcb_dir:
        jdir = Path(args.jlcpcb_dir)
        for pattern, label in (
            ("*-jlcpcb-production.zip", "JLCPCB Full PCBA Bundle (ZIP)"),
            ("*-jlcpcb-gerbers.zip", "JLCPCB Gerbers + Drills (ZIP)"),
            ("*-jlcpcb-bom.csv", "JLCPCB BOM (CSV)"),
            ("*-jlcpcb-cpl.csv", "JLCPCB Pick & Place CPL (CSV)"),
        ):
            for f in sorted(jdir.glob(pattern)):
                downloads.append((label, f))
    if args.bom_csv and Path(args.bom_csv).is_file():
        downloads.append(("Priced BOM Snapshot (CSV)", Path(args.bom_csv)))
    downloads.append(("Schematic PDF", docs_assets["sch_pdf"]))
    if docs_assets.get("step_file"):
        downloads.append(("3D Mechanical Model (STEP)", docs_assets["step_file"]))
    downloads.append(("PCB Renders Pack (ZIP)", docs_assets["renders_zip"]))

    dl_entries: List[Tuple[str, str, str]] = []
    for label, src in downloads:
        if src and src.is_file():
            shutil.copy2(src, files_dir / src.name)
            size_kb = max(1, src.stat().st_size // 1024)
            dl_entries.append((label, f"files/{src.name}", f"{size_kb:,} KB"))

    has_ibom = False
    if args.ibom and Path(args.ibom).is_file():
        shutil.copy2(Path(args.ibom), site_dir / "ibom-raw.html")
        has_ibom = True

    bom_payload = None
    if args.bom_json and Path(args.bom_json).is_file():
        bom_payload = json.loads(Path(args.bom_json).read_text(encoding="utf-8"))
        shutil.copy2(Path(args.bom_json), meta_dir / "bom-priced.json")

    checks_md_text = None
    if args.checks_md and Path(args.checks_md).is_file():
        checks_md_text = Path(args.checks_md).read_text(encoding="utf-8")
        shutil.copy2(Path(args.checks_md), meta_dir / "checks-summary.md")

    checks_json_data = None
    if args.checks_json and Path(args.checks_json).is_file():
        checks_json_data = json.loads(Path(args.checks_json).read_text(encoding="utf-8"))
        shutil.copy2(Path(args.checks_json), meta_dir / "checks-summary.json")

    sanity_md_text = None
    if args.sanity_md and Path(args.sanity_md).is_file():
        sanity_md_text = Path(args.sanity_md).read_text(encoding="utf-8")
        shutil.copy2(Path(args.sanity_md), meta_dir / "sanity-summary.md")

    # Save manifest for future fast docs-only rebuilds
    (meta_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": args.version,
                "version_id": args.version_id,
                "commit_sha": args.commit_sha,
                "copied_3d": copied_3d,
                "copied_2d": copied_2d,
                "copied_sch_svgs": copied_sch_svgs,
                "dl_entries": dl_entries,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    build_site(
        cfg=cfg,
        site_dir=site_dir,
        version=args.version,
        version_id=args.version_id,
        commit_sha=args.commit_sha,
        copied_3d=copied_3d,
        copied_2d=copied_2d,
        copied_sch_svgs=copied_sch_svgs,
        dl_entries=dl_entries,
        has_ibom=has_ibom,
        bom_payload=bom_payload,
        checks_md_text=checks_md_text,
        checks_json_data=checks_json_data,
        sanity_md_text=sanity_md_text,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
