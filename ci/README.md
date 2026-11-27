# KiCad 10 CI/CD Pipeline Guide

This repository uses a deterministic, self-contained **KiCad 10** CI/CD pipeline built for GitHub Actions and GitHub Pages. Everything is driven from a single config file: [`ci/config.yml`](config.yml).

---

## 1. Architecture Overview

Three workflows split fast feedback, fast documentation updates, and full manufacturing releases:

| Workflow | Triggers | Runtime | What it does |
| :--- | :--- | :---: | :--- |
| **KiCad CI** (`.github/workflows/ci.yml`) | Push / PR touching `*.kicad_*`, `assets/**`, `*lib-table`, `ci/**` | ~45–90 s | Runs KiCad `10.0.8` ERC & DRC (non-blocking, loud annotations + CAUTION banners), BOM/LCSC & lib-table sanity checks, and PR visual PCB/Schematic diff + sticky PR comment. |
| **Docs Fast Update & Cleanup** (`.github/workflows/docs-fast.yml`) | Push to `main` touching `docs/**`, `ci/config.yml`, or docs scripts; manual dispatch; branch delete | ~15–20 s | **No KiCad container.** Reuses the already-built renders, STEP, iBOM, and BOM JSON from `gh-pages`, recompiles your Markdown pages (`docs/*.md`), and redeploys Pages. Optionally refreshes live JLC/LCSC prices (`refresh_bom_prices`) and auto-prunes deleted branch previews. |
| **KiCad Release, Production & Docs** (`.github/workflows/release.yml`) | Tag push (`v*`) or manual `workflow_dispatch` from any branch/tag | ~2–4 min | Full KiCad `10.0.8-full` build: ERC/DRC, JLCPCB PCBA pack (Gerbers + Drills, merged BOM, rotation-corrected CPL), hardware sanity checks, live JLCPCB + LCSC pricing & locally cached product thumbnails, Interactive BOM (`InteractiveHtmlBom==2.12.0`), parallel 2D/3D renders & STEP model, versioned GitHub Pages publish, and GitHub Release creation. |

---

## 2. Versioned GitHub Pages Layout

Published docs are stored persistently on the orphan `gh-pages` branch and deployed via GitHub's native Pages action (`actions/deploy-pages@v4`):

```text
gh-pages/
├── index.html             # Redirects to ./latest/ (or ./main/ before first tag)
├── versions.json          # Powers the topbar Releases / Branches dropdown switcher
├── .nojekyll
├── latest/                # Mirror of latest v* tag (or main until first tag)
├── v1.0.0/                # Frozen immutable release tag
├── v0.2.0/                # Frozen immutable release tag
├── main/                  # Rolling main branch build (updated by docs-fast.yml)
└── branches/
    └── feat-usb/          # Optional branch preview (auto-pruned when branch is deleted)
```

- **Tag builds (`v*`)** publish to `/<tag>/`, update `/latest/`, and create/update a GitHub Release.
- **Manual builds on `main`** publish to `/main/` without overwriting your frozen `/v*/` or `/latest/` release docs (unless you tick `update_latest`).
- **Manual builds on feature branches** publish to `/branches/<slug>/` and are automatically cleaned up when the branch is deleted.
- **Switching versions** in the Mintlify topbar dropdown keeps you on the same page (`bom.html` → `../v0.2.0/bom.html`).

---

## 3. Pipeline Scripts Reference

| File | Responsibility |
| :--- | :--- |
| `ci/config.yml` | Single source of truth for board filenames, layers, pinned KiCad & iBOM versions, render views, branding color, and Pages storage branch. |
| `ci/jlcpcb-rotations-base.csv` | Vendored standard KiCad → JLCPCB CPL rotation rules (deterministic, zero network fetch). |
| `ci/jlcpcb-rotations.csv` | Board-specific rotation/offset overrides (highest priority; supports regex on footprint or `REF:<Designator>`). |
| `ci/scripts/config_loader.py` | Shared YAML config loader + KiCad 10 S-expression PCB footprint/LCSC parser. |
| `ci/scripts/parse_checks.py` | Runs `kicad-cli sch erc` and `kicad-cli pcb drc`, writes JSON + Markdown reports, emits `::error::` / `::warning::` annotations, and respects `fail_on_*_errors` policy. |
| `ci/scripts/build_jlcpcb.py` | Exports Gerbers + Excellon drills ZIP, merges schematic + PCB LCSC fields into JLCPCB BOM CSV, and builds rotation-corrected CPL CSV + all-in-one ZIP. |
| `ci/scripts/sanity_checks.py` | Deterministic checks for LCSC ↔ Value conflicts, missing LCSC codes, broken `${KIPRJMOD}` library table paths, and BOM ↔ CPL placement coverage. |
| `ci/scripts/jlc_bom_pricing.py` | Queries live JLCPCB SMT (`cart.jlcpcb.com`) and LCSC (`wmsc.lcsc.com`) with retries/backoff, optional `--cache-json` fallback, local 160×160 thumbnail caching, and generates the foldable interactive Priced BOM widget. |
| `ci/scripts/visual_diff.py` | Renders base vs. PR head PCB 3D views & schematic pages, creates red/green diff overlays, computes BOM deltas, and outputs `visual-diff.html`. |
| `ci/scripts/build_docs.py` | Runs 2D/3D/STEP exports in parallel and compiles manual `docs/*.md` + integrated Priced BOM + embedded iBOM into the Mintlify-styled site. Supports `--reuse-site` for ~15 s docs-only rebuilds without KiCad. |
| `ci/scripts/pages_manager.py` | Manages `<pages-root>/<version-id>/`, `/latest/`, `versions.json`, root redirect, and branch pruning. |
| `ci/scripts/build_release_notes.py` | Assembles deterministic GitHub Release notes (CAUTION banner if errors exist, manual `docs/CHANGELOG.md` excerpt, verification table, BOM cost breakdown, Pages links). |

---

## 4. How to Set Up This Pipeline on Another KiCad 10 Repo

Follow these 5 steps to reuse the entire pipeline on a new KiCad 10 hardware repository:

### Step 1 — Copy the Pipeline Files
Copy these paths into the root of your new KiCad 10 repository:
```text
.github/workflows/ci.yml
.github/workflows/release.yml
.github/workflows/docs-fast.yml
ci/
docs/
Makefile
```
And add these lines to your new repo's `.gitignore` (especially if it ignores `*.csv`):
```gitignore
build/
__pycache__/
*.py[cod]
!ci/jlcpcb-rotations.csv
!ci/jlcpcb-rotations-base.csv
```

### Step 2 — Edit `ci/config.yml` (the only file you must change)
Update the `project:`, `jlcpcb.gerber_layers`, and `docs:` blocks in `ci/config.yml`:
```yaml
project:
  name: "my-board"                    # Prefix for ZIPs/CSVs/PDFs
  display_name: "My Board PCB"        # Site title & Release title
  board_name: "my_board"
  schematic: "my_board.kicad_sch"     # Path relative to repo root (subfolders supported)
  pcb: "my_board.kicad_pcb"
  pro: "my_board.kicad_pro"

jlcpcb:
  # For a 4-layer board, add "In1.Cu" and "In2.Cu" to gerber_layers!
  gerber_layers:
    - "F.Cu"
    - "B.Cu"
    - "F.Paste"
    - "B.Paste"
    - "F.Silkscreen"
    - "B.Silkscreen"
    - "F.Mask"
    - "B.Mask"
    - "Edge.Cuts"

docs:
  accent_color: "#10b981"             # Your brand hex color
  external_docs_url: ""               # Optional link in top bar (leave empty to hide)
  external_docs_label: ""
```
> **Note:** If you use a non-default primary branch (e.g. `master`), update `pages.default_branch` in `ci/config.yml` and the `branches:` entry in `.github/workflows/docs-fast.yml`. Also update `handspinner_pcb.kicad_pcb` in the local `Makefile` `ibom` target if you run `make ibom` locally.

### Step 3 — Verify KiCad Project Hygiene
- Ensure all custom symbols, footprints, and 3D models in `sym-lib-table` and `fp-lib-table` use `${KIPRJMOD}/...` relative paths (never `/home/...` or `C:\...`) and are committed in the repo.
- Assign LCSC codes using any of `LCSC Part`, `LCSC Part #`, or `LCSC` (in either schematic or PCB).
- Put board-specific CPL rotation fixes in `ci/jlcpcb-rotations.csv` (keep `ci/jlcpcb-rotations-base.csv` untouched).

### Step 4 — Configure GitHub Repository Settings (One-Time)
1. **GitHub Pages**: Go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**.
2. **Workflow Permissions**: Go to **Settings → Actions → General → Workflow permissions** and confirm **Read and write permissions** is allowed (needed so `release.yml` and `docs-fast.yml` can push to the `gh-pages` branch and create Releases).

### Step 5 — Seed & Run
1. Push your commit to `main` → **KiCad CI** runs automatically.
2. Open **Actions → KiCad Release, Production & Docs → Run workflow** once to seed the initial `gh-pages` hardware build (renders, STEP, iBOM, Priced BOM).
3. From then on:
   - Editing `docs/*.md` on `main` updates your live Pages site in **~15 seconds** via `docs-fast.yml`.
   - Pushing a tag (`git tag v0.1.0 && git push origin v0.1.0`) runs the full manufacturing build, freezes `/v0.1.0/` + updates `/latest/` on Pages, and creates a GitHub Release with all JLCPCB & CAD files attached.

---

## 5. Local Testing (Linux / macOS with KiCad 10)

```bash
make check          # Run ERC & DRC locally
make sanity         # Check LCSC/Value conflicts, missing LCSC & lib tables
make jlcpcb         # Build Gerbers, BOM & CPL into build/jlcpcb/
make bom-price      # Fetch live JLC/LCSC stock, prices & thumbnails
make docs           # Full docs site build into build/docs/site/
make docs-fast      # Recompile docs/*.md reusing build/docs/site/ (no KiCad)
make all            # Run the complete pipeline locally
```
