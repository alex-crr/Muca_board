#!/usr/bin/env python3
"""Fetch live JLCPCB / LCSC pricing, availability, product images & datasheet links, and generate:
- bom-priced.json  (structured data + price tiers + summary at 1/5/10/50/100 boards)
- <prefix>-bom-priced.csv
- bom.html         (standalone interactive BOM cost & availability page)

Also exposes `render_bom_fragment(payload)` for native integration inside `build_docs.py`.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image

from config_loader import load_config

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) KiCad-CI-BOM/1.0"


def _http_get_bytes(url: str, timeout: float = 10.0, retries: int = 3) -> Optional[bytes]:
    """HTTP GET with exponential backoff + jitter to survive transient supplier API blips/429s."""
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception:
            if attempt + 1 < retries:
                delay = (0.5 * (2**attempt)) + random.uniform(0.05, 0.35)
                time.sleep(delay)
    return None


def _http_get_json(url: str, timeout: float = 10.0, retries: int = 3) -> Optional[Dict[str, Any]]:
    raw = _http_get_bytes(url, timeout=timeout, retries=retries)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None


def cache_part_thumbnail(
    code: str, image_url: str, images_dir: Path, offline: bool = False
) -> str:
    """Download & compress a 160x160 JPEG thumbnail locally so the Pages site never relies on external hotlinks."""
    if not code or not image_url:
        return ""
    images_dir.mkdir(parents=True, exist_ok=True)
    target = images_dir / f"{code}.jpg"
    rel_path = f"part-images/{code}.jpg"
    if target.is_file() and target.stat().st_size > 200:
        return rel_path
    if offline:
        return ""
    raw = _http_get_bytes(image_url, timeout=8.0, retries=2)
    if not raw:
        return ""
    try:
        img = Image.open(io.BytesIO(raw))
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[3])
            img = bg
        else:
            img = img.convert("RGB")
        img.thumbnail((160, 160), Image.Resampling.LANCZOS)
        img.save(target, format="JPEG", quality=82, optimize=True)
        return rel_path
    except Exception:
        return ""


def _parse_jlcsearch_tiers(comp: Dict[str, Any]) -> List[Dict[str, Any]]:
    tiers: List[Dict[str, Any]] = []
    extra = comp.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = None
    if isinstance(extra, dict):
        prices = extra.get("prices") or extra.get("price") or []
        if isinstance(prices, list):
            for p in prices:
                if not isinstance(p, dict):
                    continue
                q = p.get("qFrom") or p.get("min_qty") or p.get("qty") or 1
                pr = p.get("price") or p.get("usdPrice")
                if pr is not None:
                    try:
                        tiers.append({"min_qty": int(q), "unit_price": float(pr)})
                    except (TypeError, ValueError):
                        pass
    if not tiers:
        p1 = comp.get("price1") or comp.get("price")
        if p1 is not None:
            try:
                tiers.append({"min_qty": 1, "unit_price": float(p1)})
            except (TypeError, ValueError):
                pass
    tiers.sort(key=lambda t: t["min_qty"])
    return tiers


def _build_inline_datasheet_url(raw_url: str, lcsc_code: str) -> str:
    """Return a URL that opens the datasheet inline in a new browser tab without forcing a download.

    LCSC's direct CDN (`wmsc.lcsc.com`) often triggers a binary file download, whereas
    `https://www.lcsc.com/datasheet/<CODE>.pdf` opens LCSC's inline browser PDF viewer.
    """
    url = (raw_url or "").strip()
    code = (lcsc_code or "").strip().upper()
    if not url:
        return f"https://www.lcsc.com/datasheet/{code}.pdf" if code else ""
    if "wmsc.lcsc.com" in url and code:
        return f"https://www.lcsc.com/datasheet/{code}.pdf"
    if "lcsc.com/product-detail/" in url and code:
        return f"https://www.lcsc.com/datasheet/{code}.pdf"
    return url


def fetch_part_data(lcsc_code: str, offline: bool = False) -> Dict[str, Any]:
    """Fetch live JLCPCB SMT data (cart.jlcpcb.com) + LCSC warehouse data (wmsc.lcsc.com)
    for a single LCSC part code, keeping JLC and LCSC stocks & price tiers distinct."""
    code = lcsc_code.strip().upper()
    result: Dict[str, Any] = {
        "lcsc": code,
        "found": False,
        "source": [],
        "mfr": "",
        "brand": "",
        "description": "",
        "library_type": "Unknown",  # Basic | Preferred | Extended | Unknown
        "jlc_stock": None,
        "lcsc_stock": None,
        "stock": None,
        "jlc_price_tiers": [],
        "lcsc_price_tiers": [],
        "price_tiers": [],
        "product_image": "",
        "datasheet_url": "",
        "datasheet_viewer_url": f"https://www.lcsc.com/datasheet/{code}.pdf" if code else "",
        "lcsc_url": f"https://www.lcsc.com/product-detail/{code}.html" if code else "",
        "jlc_url": f"https://jlcpcb.com/partdetail/{code}" if code else "",
    }
    if offline or not code:
        return result

    num_code = code[1:] if code.startswith("C") else code

    # 1) Query JLCPCB official SMT component detail API (real-time JLC SMT stock, Basic/Preferred/Extended, JLC prices)
    jlc_detail_url = (
        "https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?"
        + urllib.parse.urlencode({"componentCode": code})
    )
    jlc_detail = _http_get_json(jlc_detail_url, timeout=10.0)
    jdata = (jlc_detail or {}).get("data") if isinstance(jlc_detail, dict) else None
    if isinstance(jdata, dict) and jdata.get("componentCode"):
        result["found"] = True
        result["source"].append("jlcpcb")
        result["mfr"] = str(jdata.get("componentModelEn") or "")
        result["brand"] = str(jdata.get("componentBrandEn") or "")
        result["description"] = str(jdata.get("describe") or "")

        # Real-time JLCPCB SMT stock
        raw_jlc_stock = jdata.get("stockCount")
        if raw_jlc_stock is None:
            raw_jlc_stock = jdata.get("overseasStockCount")
        if raw_jlc_stock is not None:
            try:
                result["jlc_stock"] = int(raw_jlc_stock)
            except (TypeError, ValueError):
                pass

        # Library classification: base (Basic), preferred, expand (Extended)
        lib_raw = str(jdata.get("componentLibraryType") or "").strip().lower()
        is_pref = bool(jdata.get("preferredComponentFlag"))
        if lib_raw == "base":
            result["library_type"] = "Basic"
        elif is_pref:
            result["library_type"] = "Preferred"
        elif lib_raw:
            result["library_type"] = "Extended"

        # JLCPCB SMT tiered pricing
        jlc_tiers: List[Dict[str, Any]] = []
        for row in jdata.get("prices") or jdata.get("jlcPrices") or []:
            if not isinstance(row, dict):
                continue
            start_q = row.get("startNumber")
            pr = row.get("productPrice")
            if start_q is not None and pr is not None:
                try:
                    jlc_tiers.append({"min_qty": int(start_q), "unit_price": float(pr)})
                except (TypeError, ValueError):
                    pass
        if not jlc_tiers and jdata.get("initialPrice") is not None:
            try:
                jlc_tiers.append({"min_qty": 1, "unit_price": float(jdata["initialPrice"])})
            except (TypeError, ValueError):
                pass
        if jlc_tiers:
            jlc_tiers.sort(key=lambda t: t["min_qty"])
            result["jlc_price_tiers"] = jlc_tiers

        # Official LCSC inline datasheet viewer URL from JLCPCB
        manual_url = str(jdata.get("dataManualUrl") or "").strip()
        if manual_url:
            result["datasheet_url"] = manual_url
            result["datasheet_viewer_url"] = _build_inline_datasheet_url(manual_url, code)

    # 2) Fallback to jlcsearch only if JLCPCB detail API didn't return a match
    if "jlcpcb" not in result["source"]:
        jlc_url = (
            "https://jlcsearch.tscircuit.com/components/list.json?"
            + urllib.parse.urlencode({"search": code, "full": "true", "limit": "10"})
        )
        jlc_json = _http_get_json(jlc_url, timeout=10.0)
        if isinstance(jlc_json, dict):
            comps = jlc_json.get("components") or []
            matched = None
            for c in comps:
                c_lcsc = str(c.get("lcsc", "")).strip().upper()
                if c_lcsc == code or c_lcsc == num_code:
                    matched = c
                    break
            if matched:
                result["found"] = True
                result["source"].append("jlcsearch")
                result["mfr"] = str(matched.get("mfr") or "")
                result["description"] = str(matched.get("description") or "")
                if matched.get("stock") is not None:
                    try:
                        result["jlc_stock"] = int(matched["stock"])
                    except (TypeError, ValueError):
                        pass
                is_basic = bool(matched.get("is_basic") or matched.get("basic"))
                is_pref = bool(matched.get("is_preferred") or matched.get("preferred"))
                if is_basic:
                    result["library_type"] = "Basic"
                elif is_pref:
                    result["library_type"] = "Preferred"
                else:
                    result["library_type"] = "Extended"
                jlc_tiers = _parse_jlcsearch_tiers(matched)
                if jlc_tiers:
                    result["jlc_price_tiers"] = jlc_tiers

    # 3) Query LCSC product detail API for LCSC warehouse stock, LCSC price breaks & product images
    lcsc_api = (
        "https://wmsc.lcsc.com/ftps/wm/product/detail?"
        + urllib.parse.urlencode({"productCode": code})
    )
    lcsc_json = _http_get_json(lcsc_api, timeout=10.0)
    prod = (lcsc_json or {}).get("result") if isinstance(lcsc_json, dict) else None
    if isinstance(prod, dict) and prod.get("productCode"):
        result["found"] = True
        result["source"].append("lcsc")
        if not result["mfr"]:
            result["mfr"] = str(prod.get("productModel") or "")
        if not result["brand"]:
            result["brand"] = str(prod.get("brandNameEn") or "")
        if not result["description"]:
            result["description"] = str(
                prod.get("productIntroEn") or prod.get("productDescEn") or ""
            )

        # Product image (LCSC returns productImages list, sometimes productImageUrl)
        imgs = prod.get("productImages") or []
        if isinstance(imgs, list):
            for img in imgs:
                if isinstance(img, str) and img.startswith("http"):
                    result["product_image"] = img
                    break
        if not result["product_image"]:
            for key in ("productImageUrl", "productImageUrlBig", "imageUrl"):
                val = prod.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    result["product_image"] = val
                    break

        if not result["datasheet_url"] and prod.get("pdfUrl"):
            result["datasheet_url"] = str(prod["pdfUrl"])
            result["datasheet_viewer_url"] = _build_inline_datasheet_url(
                result["datasheet_url"], code
            )

        if prod.get("stockNumber") is not None:
            try:
                result["lcsc_stock"] = int(prod["stockNumber"])
            except (TypeError, ValueError):
                pass

        lcsc_tiers: List[Dict[str, Any]] = []
        for row in prod.get("productPriceList") or []:
            if not isinstance(row, dict):
                continue
            ladder = row.get("ladder")
            usd = row.get("usdPrice") or row.get("productPrice")
            if ladder is not None and usd is not None:
                try:
                    lcsc_tiers.append({"min_qty": int(ladder), "unit_price": float(usd)})
                except (TypeError, ValueError):
                    pass
        if lcsc_tiers:
            lcsc_tiers.sort(key=lambda t: t["min_qty"])
            result["lcsc_price_tiers"] = lcsc_tiers

    # Default price_tiers & stock prefer JLCPCB SMT (matches default selector)
    result["price_tiers"] = result["jlc_price_tiers"] or result["lcsc_price_tiers"]
    result["stock"] = result["jlc_stock"]

    return result


def unit_price_for_qty(tiers: List[Dict[str, Any]], total_qty: int) -> Optional[float]:
    if not tiers:
        return None
    chosen = tiers[0]["unit_price"]
    for t in tiers:
        if total_qty >= t["min_qty"]:
            chosen = t["unit_price"]
        else:
            break
    return chosen


def compute_board_summary(
    items: List[Dict[str, Any]], board_count: int, extended_fee: float
) -> Dict[str, Any]:
    parts_total = 0.0
    extended_codes = set()
    in_stock_lines = 0
    insufficient_lines = 0
    missing_lcsc_lines = 0
    unpriced_lines = 0

    for it in items:
        needed = it["quantity"] * board_count
        lcsc = it.get("lcsc") or ""
        if not lcsc:
            missing_lcsc_lines += 1
            unpriced_lines += 1
            continue
        if it.get("library_type") not in ("Basic", "Preferred"):
            extended_codes.add(lcsc)
        stock = it.get("stock")
        if stock is None:
            insufficient_lines += 1
        elif stock >= needed:
            in_stock_lines += 1
        else:
            insufficient_lines += 1

        up = unit_price_for_qty(it.get("price_tiers") or [], needed)
        if up is None:
            unpriced_lines += 1
        else:
            parts_total += up * needed

    extended_count = len(extended_codes)
    feeder_fees = extended_count * extended_fee
    grand_total = parts_total + feeder_fees
    return {
        "boards": board_count,
        "parts_total_usd": round(parts_total, 4),
        "parts_per_board_usd": round(parts_total / board_count, 4),
        "extended_parts_count": extended_count,
        "feeder_fees_usd": round(feeder_fees, 2),
        "grand_total_usd": round(grand_total, 4),
        "grand_per_board_usd": round(grand_total / board_count, 4),
        "in_stock_lines": in_stock_lines,
        "insufficient_lines": insufficient_lines,
        "missing_lcsc_lines": missing_lcsc_lines,
        "unpriced_lines": unpriced_lines,
    }


def render_bom_fragment(payload: Dict[str, Any]) -> str:
    """Return the Mintlify-styled interactive Priced BOM component (CSS + HTML + JS)
    designed to render natively inside the docs shell or standalone."""
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    version = payload.get("version") or "dev"
    fetched_at = payload.get("fetched_at") or ""

    return f"""
<style>
  .bom-header {{
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-end;
    gap: 12px; margin-bottom: 20px;
  }}
  .bom-header h1 {{
    margin: 0; font-size: 1.75rem; font-weight: 650; letter-spacing: -0.025em; border: none; padding: 0;
  }}
  .bom-sub {{ color: var(--muted); font-size: 0.875rem; margin-top: 4px; }}

  .bom-controls {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }}
  .bom-ctrl-top {{
    display: flex; flex-wrap: wrap; align-items: center; gap: 16px; justify-content: space-between;
  }}
  .bom-field {{ display: flex; flex-direction: column; gap: 6px; }}
  .bom-field-label {{
    font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted);
  }}
  .bom-slider-group {{
    flex: 1; min-width: 260px; display: flex; flex-direction: column; gap: 8px;
  }}
  .bom-slider-row {{ display: flex; align-items: center; gap: 12px; }}
  .bom-slider-row input[type="range"] {{
    flex: 1; accent-color: var(--accent); height: 5px; cursor: pointer;
  }}
  .bom-qty-input {{
    width: 82px; padding: 6px 10px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--bg); color: var(--text);
    font-size: 0.92rem; font-weight: 600; text-align: center; font-variant-numeric: tabular-nums;
  }}
  .bom-select {{
    padding: 7px 32px 7px 12px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--bg); color: var(--text);
    font-size: 0.88rem; font-weight: 500; cursor: pointer; min-width: 190px;
  }}
  .bom-select:focus, .bom-qty-input:focus, .bom-search:focus {{
    outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft);
  }}
  .bom-ctrl-bottom {{
    display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
    padding-top: 12px; border-top: 1px solid var(--border);
  }}
  .bom-preset {{
    background: var(--bg); color: var(--muted); border: 1px solid var(--border);
    border-radius: 6px; padding: 4px 10px; cursor: pointer; font-size: 0.8rem; font-weight: 500;
    transition: border-color 0.15s, color 0.15s, background 0.15s;
  }}
  .bom-preset:hover {{ color: var(--text); border-color: var(--border-strong); }}
  .bom-preset.active {{
    border-color: var(--accent); color: var(--accent); background: var(--accent-soft); font-weight: 600;
  }}
  .bom-fee-toggle {{
    margin-left: auto; display: inline-flex; align-items: center; gap: 8px;
    font-size: 0.83rem; color: var(--muted); cursor: pointer; user-select: none;
  }}
  .bom-fee-toggle.disabled {{ opacity: 0.45; pointer-events: none; }}

  .bom-kpis {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(195px, 1fr));
    gap: 12px; margin-bottom: 16px;
  }}
  .bom-kpi {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    padding: 14px 16px;
  }}
  .bom-kpi-label {{
    font-size: 0.74rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted);
  }}
  .bom-kpi-val {{
    font-size: 1.45rem; font-weight: 650; margin-top: 4px; letter-spacing: -0.02em; font-variant-numeric: tabular-nums;
  }}
  .bom-kpi-sub {{ font-size: 0.8rem; color: var(--muted); margin-top: 2px; }}

  .bom-toolbar {{
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px;
  }}
  .bom-search {{
    flex: 1; min-width: 220px; padding: 8px 12px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--surface); color: var(--text); font-size: 0.88rem;
  }}
  .bom-pill {{
    background: var(--surface); color: var(--muted); border: 1px solid var(--border);
    border-radius: 8px; padding: 6px 11px; font-size: 0.82rem; font-weight: 500; cursor: pointer;
    transition: all 0.15s;
  }}
  .bom-pill:hover {{ color: var(--text); border-color: var(--border-strong); }}
  .bom-pill.active {{ border-color: var(--accent); color: var(--accent); background: var(--accent-soft); font-weight: 600; }}
  .bom-btn {{
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--surface); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 6px 12px; font-size: 0.82rem; font-weight: 500; cursor: pointer;
    text-decoration: none; transition: border-color 0.15s, background 0.15s;
  }}
  .bom-btn:hover {{ border-color: var(--border-strong); background: var(--surface-2); text-decoration: none; }}
  .bom-btn.primary {{
    background: var(--accent); border-color: var(--accent); color: #ffffff; font-weight: 600;
  }}
  .bom-btn.primary:hover {{ filter: brightness(1.06); }}

  .bom-table-card {{
    border: 1px solid var(--border); border-radius: 12px; background: var(--surface); overflow: hidden;
  }}
  .bom-table-scroll {{ overflow-x: auto; }}
  table.bom-table {{ width: 100%; border-collapse: collapse; margin: 0; font-size: 0.86rem; }}
  table.bom-table th, table.bom-table td {{
    border: none; border-bottom: 1px solid var(--border); padding: 10px 12px; text-align: left; vertical-align: middle;
  }}
  table.bom-table thead th {{
    background: var(--surface-2); color: var(--muted); font-size: 0.74rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.04em; cursor: pointer; user-select: none; white-space: nowrap;
  }}
  table.bom-table thead th:hover {{ color: var(--text); }}
  table.bom-table tbody tr.bom-row {{ cursor: pointer; transition: background 0.12s; }}
  table.bom-table tbody tr.bom-row:hover {{ background: var(--hover); }}
  table.bom-table tbody tr.bom-row.expanded {{ background: var(--surface-2); }}
  table.bom-table tbody tr.bom-detail-row td {{
    padding: 0; background: var(--bg); border-bottom: 1px solid var(--border);
  }}
  .bom-num {{ text-align: right !important; font-variant-numeric: tabular-nums; white-space: nowrap; }}

  .bom-chev {{
    width: 24px; color: var(--muted); transition: transform 0.15s ease; display: inline-flex; align-items: center; justify-content: center;
  }}
  tr.bom-row.expanded .bom-chev {{ transform: rotate(90deg); color: var(--text); }}

  .bom-part-cell {{ display: flex; align-items: center; gap: 11px; min-width: 220px; }}
  .bom-thumb {{
    width: 38px; height: 38px; border-radius: 8px; border: 1px solid var(--border);
    background: #ffffff; object-fit: contain; padding: 3px; flex-shrink: 0;
  }}
  .bom-thumb-placeholder {{
    width: 38px; height: 38px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--surface-2); color: var(--muted);
    display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0;
  }}
  .bom-part-title {{ font-weight: 600; color: var(--text); line-height: 1.3; }}
  .bom-part-meta {{ font-size: 0.78rem; color: var(--muted); line-height: 1.35; margin-top: 2px; }}
  .bom-mono {{ font-family: var(--mono); font-size: 0.8rem; }}

  .bom-badge {{
    display: inline-flex; align-items: center; gap: 5px;
    padding: 2px 8px; border-radius: 999px; font-size: 0.73rem; font-weight: 600; white-space: nowrap;
    border: 1px solid transparent;
  }}
  .bom-dot {{ width: 6px; height: 6px; border-radius: 50%; display: inline-block; }}
  .b-ok {{ background: rgba(16,185,129,0.12); color: #10b981; border-color: rgba(16,185,129,0.25); }}
  .b-ok .bom-dot {{ background: #10b981; }}
  .b-warn {{ background: rgba(245,158,11,0.12); color: #d97706; border-color: rgba(245,158,11,0.28); }}
  .b-warn .bom-dot {{ background: #f59e0b; }}
  .b-bad {{ background: rgba(239,68,68,0.12); color: #ef4444; border-color: rgba(239,68,68,0.28); }}
  .b-bad .bom-dot {{ background: #ef4444; }}
  .b-neutral {{ background: var(--surface-2); color: var(--muted); border-color: var(--border); }}
  .b-basic {{ background: rgba(16,185,129,0.10); color: #10b981; border-color: rgba(16,185,129,0.22); }}
  .b-pref {{ background: rgba(59,130,246,0.10); color: #3b82f6; border-color: rgba(59,130,246,0.22); }}
  .b-ext {{ background: var(--surface-2); color: var(--muted); border-color: var(--border); }}

  .bom-icon-link {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 30px; height: 30px; border-radius: 7px; border: 1px solid var(--border);
    color: var(--muted); background: var(--bg); text-decoration: none; transition: all 0.15s;
  }}
  .bom-icon-link:hover {{ color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }}

  /* Expandable JLC/LCSC-style detail drawer */
  .bom-drawer {{
    padding: 16px 20px 18px 46px;
    display: grid;
    grid-template-columns: auto 1.4fr 1fr;
    gap: 20px;
    align-items: start;
  }}
  .bom-drawer-img {{
    width: 88px; height: 88px; border-radius: 10px; border: 1px solid var(--border);
    background: #ffffff; object-fit: contain; padding: 6px;
  }}
  .bom-kv-grid {{
    display: grid; grid-template-columns: 110px 1fr; gap: 6px 12px; font-size: 0.83rem;
  }}
  .bom-kv-k {{ color: var(--muted); font-weight: 500; }}
  .bom-kv-v {{ color: var(--text); word-break: break-word; }}
  .bom-tiers-box {{
    border: 1px solid var(--border); border-radius: 10px; background: var(--surface); overflow: hidden;
  }}
  .bom-tiers-head {{
    padding: 7px 12px; font-size: 0.74rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.04em; color: var(--muted); background: var(--surface-2); border-bottom: 1px solid var(--border);
  }}
  table.bom-tiers-tbl {{ width: 100%; border-collapse: collapse; margin: 0; font-size: 0.81rem; }}
  table.bom-tiers-tbl td {{ padding: 5px 12px; border-bottom: 1px solid var(--border); }}
  table.bom-tiers-tbl tr:last-child td {{ border-bottom: none; }}
  table.bom-tiers-tbl tr.active-tier td {{
    background: var(--accent-soft); color: var(--accent); font-weight: 600;
  }}
  .bom-drawer-actions {{
    display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px;
  }}
  .bom-foot {{
    margin-top: 12px; color: var(--muted); font-size: 0.8rem;
    display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px;
  }}
  @media (max-width: 900px) {{
    .bom-drawer {{ grid-template-columns: 1fr; padding-left: 16px; }}
  }}
</style>

<div class="bom-header">
  <div>
    <h1>Priced BOM &amp; Availability</h1>
    <div class="bom-sub">Version <strong>{version}</strong> · Snapshot fetched {fetched_at} · Click any row to inspect price breaks, designators &amp; datasheet</div>
  </div>
</div>

<section class="bom-controls">
  <div class="bom-ctrl-top">
    <div class="bom-slider-group">
      <div class="bom-field-label">Board / Assembly Quantity</div>
      <div class="bom-slider-row">
        <input id="boardSlider" type="range" min="1" max="500" step="1" value="5" aria-label="Board count slider">
        <input id="boardInput" class="bom-qty-input" type="number" min="1" max="10000" step="1" value="5" aria-label="Board count">
        <span class="bom-sub" style="margin:0">boards</span>
      </div>
    </div>
    <div class="bom-field">
      <label class="bom-field-label" for="sourceSelect">Part Source &amp; Stock</label>
      <select id="sourceSelect" class="bom-select">
        <option value="jlc" selected>JLCPCB Parts (SMT)</option>
        <option value="lcsc">LCSC (Direct Warehouse)</option>
      </select>
    </div>
  </div>

  <div class="bom-ctrl-bottom">
    <span class="bom-sub" style="margin:0 4px 0 0">Presets:</span>
    <button type="button" class="bom-preset" data-q="1">1</button>
    <button type="button" class="bom-preset" data-q="2">2</button>
    <button type="button" class="bom-preset active" data-q="5">5</button>
    <button type="button" class="bom-preset" data-q="10">10</button>
    <button type="button" class="bom-preset" data-q="25">25</button>
    <button type="button" class="bom-preset" data-q="50">50</button>
    <button type="button" class="bom-preset" data-q="100">100</button>
    <button type="button" class="bom-preset" data-q="250">250</button>
    <button type="button" class="bom-preset" data-q="500">500</button>
    <label id="feeToggleLabel" class="bom-fee-toggle">
      <input id="includeFeeder" type="checkbox" checked>
      Include JLCPCB Extended feeder fee ($<span id="feeVal">3.00</span> / unique ext. part)
    </label>
  </div>
</section>

<section class="bom-kpis">
  <div class="bom-kpi">
    <div class="bom-kpi-label">Parts / Board</div>
    <div class="bom-kpi-val" id="kpiPartsPerBoard">$0.00</div>
    <div class="bom-kpi-sub" id="kpiPartsTotal">Total: $0.00</div>
  </div>
  <div class="bom-kpi">
    <div class="bom-kpi-label" id="kpiFeesLabel">JLC Feeder Fees</div>
    <div class="bom-kpi-val" id="kpiFees">$0.00</div>
    <div class="bom-kpi-sub" id="kpiExtCount">0 extended parts</div>
  </div>
  <div class="bom-kpi">
    <div class="bom-kpi-label">Est. Grand Total</div>
    <div class="bom-kpi-val" id="kpiGrandTotal" style="color:var(--accent)">$0.00</div>
    <div class="bom-kpi-sub" id="kpiGrandPerBoard">$0.00 / board</div>
  </div>
  <div class="bom-kpi">
    <div class="bom-kpi-label" id="kpiStockLabel">JLC Stock Readiness</div>
    <div class="bom-kpi-val" id="kpiStockHealth">—</div>
    <div class="bom-kpi-sub" id="kpiStockSub">—</div>
  </div>
</section>

<section class="bom-toolbar">
  <input id="searchBox" class="bom-search" type="search" placeholder="Search value, MPN, designator, footprint, or LCSC code...">
  <button type="button" class="bom-pill active" data-filter="all">All</button>
  <button type="button" class="bom-pill" data-filter="basic" id="pillBasic">Basic / Pref</button>
  <button type="button" class="bom-pill" data-filter="extended" id="pillExt">Extended</button>
  <button type="button" class="bom-pill" data-filter="issue">Stock / Missing</button>
  <button type="button" id="toggleExpandAll" class="bom-btn">Expand all</button>
  <button type="button" id="exportCsv" class="bom-btn primary">Export CSV (<span id="exportQty">5</span>)</button>
</section>

<div class="bom-table-card">
  <div class="bom-table-scroll">
    <table class="bom-table">
      <thead>
        <tr>
          <th style="width:32px"></th>
          <th data-sort="comment">Component</th>
          <th data-sort="lcsc">Part #</th>
          <th data-sort="active_stock" class="bom-num" id="thStock">JLC Stock</th>
          <th data-sort="total_qty" class="bom-num">Qty</th>
          <th data-sort="unit_price" class="bom-num">Unit</th>
          <th data-sort="line_total" class="bom-num">Total</th>
          <th style="width:46px;text-align:center">Doc</th>
        </tr>
      </thead>
      <tbody id="bomBody"></tbody>
    </table>
  </div>
</div>

<div class="bom-foot">
  <span>Total placements/board: <strong id="footPlacements">0</strong> · Unique BOM lines: <strong id="footLines">0</strong></span>
  <span>Excludes bare PCB fabrication, SMT assembly labor &amp; shipping</span>
</div>

<script>
(function() {{
  const BOM_DATA = {data_json};

  let boardCount = 5;
  let partSource = "jlc"; // "jlc" | "lcsc"
  let includeFeeder = true;
  let activeFilter = "all";
  let searchTerm = "";
  let sortKey = "designator_str";
  let sortAsc = true;
  const expandedSet = new Set();

  const slider = document.getElementById("boardSlider");
  const numInput = document.getElementById("boardInput");
  const sourceSelect = document.getElementById("sourceSelect");
  const feeCheckbox = document.getElementById("includeFeeder");
  const feeToggleLabel = document.getElementById("feeToggleLabel");
  const feeUnit = Number(BOM_DATA.extended_part_fee_usd ?? 3.0);

  document.getElementById("feeVal").textContent = feeUnit.toFixed(2);
  document.getElementById("footPlacements").textContent = BOM_DATA.total_components;
  document.getElementById("footLines").textContent = BOM_DATA.unique_parts;

  const CHIP_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M9 7V4m6 3V4M9 20v-3m6 3v-3M7 9H4m3 6H4m16-6h-3m3 6h-3"/></svg>';
  const PDF_SVG = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>';
  const CHEV_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>';

  function escHtml(s) {{
    return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
  }}

  function fmtUsd(v, digits = 2) {{
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    return "$" + v.toFixed(digits);
  }}

  function fmtUnit(v) {{
    if (v === null || v === undefined) return "—";
    if (v < 0.01) return "$" + v.toFixed(5);
    if (v < 1) return "$" + v.toFixed(4);
    return "$" + v.toFixed(3);
  }}

  function getActiveTierIndex(tiers, totalQty) {{
    if (!tiers || !tiers.length) return -1;
    let idx = 0;
    for (let i = 0; i < tiers.length; i++) {{
      if (totalQty >= tiers[i].min_qty) idx = i;
      else break;
    }}
    return idx;
  }}

  function computeRows() {{
    return BOM_DATA.items.map((it, idx) => {{
      const rowId = `${{it.designator_str}}__${{idx}}`;
      const totalQty = it.quantity * boardCount;
      const activeTiers = partSource === "jlc"
        ? ((it.jlc_price_tiers && it.jlc_price_tiers.length) ? it.jlc_price_tiers : (it.price_tiers || []))
        : ((it.lcsc_price_tiers && it.lcsc_price_tiers.length) ? it.lcsc_price_tiers : (it.price_tiers || []));
      const tierIdx = getActiveTierIndex(activeTiers, totalQty);
      const unitPrice = tierIdx >= 0 ? activeTiers[tierIdx].unit_price : null;
      const lineTotal = unitPrice !== null ? unitPrice * totalQty : null;
      const hasLcsc = Boolean(it.lcsc);

      // Strictly use the selected supplier's stock (no silent cross-supplier fallback)
      const activeStock = partSource === "jlc" ? it.jlc_stock : it.lcsc_stock;

      let status = "ok";
      if (!hasLcsc) status = "missing";
      else if (activeStock === null || activeStock === undefined) status = "unknown";
      else if (activeStock === 0) status = "out";
      else if (activeStock < totalQty) status = "short";
      else if (activeStock < totalQty * 3) status = "low";

      return {{
        ...it,
        _rowId: rowId,
        total_qty: totalQty,
        active_tiers: activeTiers,
        tier_idx: tierIdx,
        unit_price: unitPrice,
        line_total: lineTotal,
        active_stock: activeStock,
        status,
      }};
    }});
  }}

  function shortRefs(refs) {{
    if (!refs || !refs.length) return "";
    if (refs.length <= 4) return refs.join(", ");
    return refs.slice(0, 4).join(", ") + ` +${{refs.length - 4}}`;
  }}

  function render() {{
    const isJlc = partSource === "jlc";
    document.getElementById("thStock").textContent = isJlc ? "JLC Stock" : "LCSC Stock";
    document.getElementById("kpiStockLabel").textContent = isJlc ? "JLC Stock Readiness" : "LCSC Stock Readiness";
    document.getElementById("kpiFeesLabel").textContent = isJlc ? "JLC Feeder Fees" : "Feeder Fees (N/A)";
    feeToggleLabel.classList.toggle("disabled", !isJlc);
    document.getElementById("pillBasic").style.display = isJlc ? "" : "none";
    document.getElementById("pillExt").style.display = isJlc ? "" : "none";
    if (!isJlc && (activeFilter === "basic" || activeFilter === "extended")) {{
      activeFilter = "all";
      document.querySelectorAll(".bom-pill").forEach(b => b.classList.toggle("active", b.dataset.filter === "all"));
    }}

    const rows = computeRows();
    let partsTotal = 0;
    const extCodes = new Set();
    let okLines = 0, issueLines = 0, missingLines = 0;

    for (const r of rows) {{
      if (r.line_total !== null) partsTotal += r.line_total;
      if (r.lcsc && r.library_type !== "Basic" && r.library_type !== "Preferred") extCodes.add(r.lcsc);
      if (r.status === "ok" || r.status === "low") okLines++;
      else {{
        issueLines++;
        if (r.status === "missing") missingLines++;
      }}
    }}

    const extCount = extCodes.size;
    const fees = (isJlc && includeFeeder) ? extCount * feeUnit : 0;
    const grand = partsTotal + fees;

    document.getElementById("kpiPartsPerBoard").textContent = fmtUsd(partsTotal / boardCount);
    document.getElementById("kpiPartsTotal").textContent = `Total (${{boardCount}} bd): ${{fmtUsd(partsTotal)}}`;
    document.getElementById("kpiFees").textContent = isJlc ? fmtUsd(fees) : "$0.00";
    document.getElementById("kpiExtCount").textContent = isJlc
      ? `${{extCount}} unique extended part${{extCount === 1 ? "" : "s"}}`
      : "No SMT feeder fees on LCSC direct";
    document.getElementById("kpiGrandTotal").textContent = fmtUsd(grand);
    document.getElementById("kpiGrandPerBoard").textContent = `${{fmtUsd(grand / boardCount)}} / board`;
    document.getElementById("exportQty").textContent = boardCount;

    const stockEl = document.getElementById("kpiStockHealth");
    stockEl.textContent = `${{okLines}} / ${{rows.length}}`;
    stockEl.style.color = issueLines === 0 ? "#10b981" : (missingLines > 0 && issueLines === missingLines ? "#d97706" : "#ef4444");
    document.getElementById("kpiStockSub").textContent =
      issueLines === 0
        ? `All parts available for ${{boardCount}} boards`
        : `${{issueLines}} line${{issueLines === 1 ? "" : "s"}} need attention (${{missingLines}} missing LCSC)`;

    // Filter
    const q = searchTerm.trim().toLowerCase();
    const filtered = rows.filter(r => {{
      if (activeFilter === "basic" && r.library_type !== "Basic" && r.library_type !== "Preferred") return false;
      if (activeFilter === "extended" && (!r.lcsc || r.library_type === "Basic" || r.library_type === "Preferred")) return false;
      if (activeFilter === "issue" && r.status === "ok") return false;
      if (!q) return true;
      const hay = `${{r.designator_str}} ${{r.comment}} ${{r.footprint}} ${{r.lcsc}} ${{r.mfr || ""}} ${{r.brand || ""}} ${{r.description || ""}}`.toLowerCase();
      return hay.includes(q);
    }});

    // Sort
    filtered.sort((a, b) => {{
      let va = a[sortKey], vb = b[sortKey];
      if (va === null || va === undefined) va = sortAsc ? Infinity : -Infinity;
      if (vb === null || vb === undefined) vb = sortAsc ? Infinity : -Infinity;
      if (typeof va === "number" && typeof vb === "number") return sortAsc ? va - vb : vb - va;
      return sortAsc
        ? String(va).localeCompare(String(vb), undefined, {{ numeric: true }})
        : String(vb).localeCompare(String(va), undefined, {{ numeric: true }});
    }});

    const tbody = document.getElementById("bomBody");
    const htmlParts = [];

    for (const r of filtered) {{
      const isOpen = expandedSet.has(r._rowId);
      const partUrl = isJlc ? r.jlc_url : r.lcsc_url;
      const thumbHtml = r.product_image
        ? `<img class="bom-thumb" src="${{escHtml(r.product_image)}}" alt="" loading="lazy" onerror="this.style.display='none'">`
        : `<span class="bom-thumb-placeholder">${{CHIP_SVG}}</span>`;

      let typePill = "";
      if (isJlc && r.lcsc) {{
        if (r.library_type === "Basic") typePill = '<span class="bom-badge b-basic">Basic</span>';
        else if (r.library_type === "Preferred") typePill = '<span class="bom-badge b-pref">Pref</span>';
        else typePill = '<span class="bom-badge b-ext">Extended</span>';
      }}

      let stockBadge = "";
      if (r.status === "missing") stockBadge = '<span class="bom-badge b-warn"><span class="bom-dot"></span>No LCSC</span>';
      else if (r.status === "unknown") stockBadge = `<span class="bom-badge b-neutral">N/A (${{isJlc ? "JLC" : "LCSC"}})</span>`;
      else if (r.status === "out") stockBadge = '<span class="bom-badge b-bad"><span class="bom-dot"></span>Out of stock</span>';
      else if (r.status === "short") stockBadge = `<span class="bom-badge b-bad"><span class="bom-dot"></span>${{r.active_stock.toLocaleString()}} / ${{r.total_qty}}</span>`;
      else if (r.status === "low") stockBadge = `<span class="bom-badge b-warn"><span class="bom-dot"></span>${{r.active_stock.toLocaleString()}}</span>`;
      else stockBadge = `<span class="bom-badge b-ok"><span class="bom-dot"></span>${{r.active_stock.toLocaleString()}}</span>`;

      const lcscLink = r.lcsc
        ? `<a class="bom-mono" href="${{escHtml(partUrl)}}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">${{escHtml(r.lcsc)}}</a>`
        : '<span class="bom-badge b-warn">Missing</span>';

      const docLink = r.datasheet_viewer_url
        ? `<a class="bom-icon-link" href="${{escHtml(r.datasheet_viewer_url)}}" target="_blank" rel="noopener noreferrer" title="Open datasheet in new tab" onclick="event.stopPropagation()">${{PDF_SVG}}</a>`
        : '<span style="color:var(--muted)">—</span>';

      htmlParts.push(`
        <tr class="bom-row ${{isOpen ? "expanded" : ""}}" data-id="${{escHtml(r._rowId)}}">
          <td><span class="bom-chev">${{CHEV_SVG}}</span></td>
          <td>
            <div class="bom-part-cell">
              ${{thumbHtml}}
              <div>
                <div class="bom-part-title">${{escHtml(r.comment)}}</div>
                <div class="bom-part-meta">
                  <span class="bom-mono">${{escHtml(shortRefs(r.designators))}}</span>
                  ${{r.mfr ? ` · ${{escHtml(r.mfr)}}` : ""}}
                </div>
              </div>
            </div>
          </td>
          <td>
            <div style="display:flex;flex-direction:column;gap:4px;align-items:flex-start">
              ${{lcscLink}}
              ${{typePill}}
            </div>
          </td>
          <td class="bom-num">${{stockBadge}}</td>
          <td class="bom-num">
            <strong>${{r.total_qty}}</strong>
            <div class="bom-part-meta">${{r.quantity}}/bd</div>
          </td>
          <td class="bom-num">${{fmtUnit(r.unit_price)}}</td>
          <td class="bom-num"><strong>${{fmtUsd(r.line_total, 3)}}</strong></td>
          <td style="text-align:center">${{docLink}}</td>
        </tr>
      `);

      if (isOpen) {{
        const bigImg = r.product_image
          ? `<img class="bom-drawer-img" src="${{escHtml(r.product_image)}}" alt="${{escHtml(r.comment)}}" loading="lazy">`
          : `<div class="bom-drawer-img" style="display:flex;align-items:center;justify-content:center;background:var(--surface-2);color:var(--muted)">${{CHIP_SVG}}</div>`;

        const jlcStockStr = (r.jlc_stock !== null && r.jlc_stock !== undefined) ? r.jlc_stock.toLocaleString() : "N/A";
        const lcscStockStr = (r.lcsc_stock !== null && r.lcsc_stock !== undefined) ? r.lcsc_stock.toLocaleString() : "N/A";

        let tiersHtml = `<div class="bom-tiers-box"><div class="bom-tiers-head">${{isJlc ? "JLCPCB SMT" : "LCSC"}} Price Breaks</div>`;
        if (r.active_tiers && r.active_tiers.length) {{
          tiersHtml += '<table class="bom-tiers-tbl"><tbody>';
          r.active_tiers.forEach((t, ti) => {{
            tiersHtml += `<tr class="${{ti === r.tier_idx ? "active-tier" : ""}}"><td>${{t.min_qty.toLocaleString()}}+</td><td class="bom-num">${{fmtUnit(t.unit_price)}}</td></tr>`;
          }});
          tiersHtml += '</tbody></table>';
        }} else {{
          tiersHtml += '<div style="padding:10px 12px;font-size:0.8rem;color:var(--muted)">No price break data</div>';
        }}
        tiersHtml += '</div>';

        const actions = [];
        if (r.datasheet_viewer_url) {{
          actions.push(`<a class="bom-btn primary" href="${{escHtml(r.datasheet_viewer_url)}}" target="_blank" rel="noopener noreferrer">Open Datasheet ↗</a>`);
        }}
        if (r.jlc_url) {{
          actions.push(`<a class="bom-btn" href="${{escHtml(r.jlc_url)}}" target="_blank" rel="noopener noreferrer">JLCPCB Part ↗</a>`);
        }}
        if (r.lcsc_url) {{
          actions.push(`<a class="bom-btn" href="${{escHtml(r.lcsc_url)}}" target="_blank" rel="noopener noreferrer">LCSC Page ↗</a>`);
        }}

        htmlParts.push(`
          <tr class="bom-detail-row">
            <td colspan="8">
              <div class="bom-drawer">
                <div>${{bigImg}}</div>
                <div>
                  <div class="bom-kv-grid">
                    <div class="bom-kv-k">Designators</div>
                    <div class="bom-kv-v bom-mono">${{escHtml(r.designator_str)}}</div>
                    <div class="bom-kv-k">Footprint</div>
                    <div class="bom-kv-v bom-mono">${{escHtml(r.footprint)}}</div>
                    ${{r.mfr ? `<div class="bom-kv-k">MPN / Brand</div><div class="bom-kv-v">${{escHtml(r.mfr)}}${{r.brand ? " · " + escHtml(r.brand) : ""}}</div>` : ""}}
                    ${{r.description ? `<div class="bom-kv-k">Description</div><div class="bom-kv-v">${{escHtml(r.description)}}</div>` : ""}}
                    <div class="bom-kv-k">Availability</div>
                    <div class="bom-kv-v">JLCPCB SMT: <strong>${{jlcStockStr}}</strong> · LCSC Warehouse: <strong>${{lcscStockStr}}</strong></div>
                  </div>
                  <div class="bom-drawer-actions">${{actions.join("")}}</div>
                </div>
                <div>${{tiersHtml}}</div>
              </div>
            </td>
          </tr>
        `);
      }}
    }}

    tbody.innerHTML = htmlParts.join("");

    // Bind row fold/unfold click handlers
    tbody.querySelectorAll("tr.bom-row").forEach(tr => {{
      tr.addEventListener("click", () => {{
        const id = tr.dataset.id;
        if (expandedSet.has(id)) expandedSet.delete(id);
        else expandedSet.add(id);
        render();
      }});
    }});

    const expandBtn = document.getElementById("toggleExpandAll");
    expandBtn.textContent = expandedSet.size > 0 ? "Collapse all" : "Expand all";
  }}

  function setBoards(val) {{
    const n = Math.max(1, Math.min(10000, parseInt(val, 10) || 1));
    boardCount = n;
    slider.value = Math.min(500, n);
    numInput.value = n;
    document.querySelectorAll(".bom-preset[data-q]").forEach(b => {{
      b.classList.toggle("active", Number(b.dataset.q) === n);
    }});
    render();
  }}

  slider.addEventListener("input", e => setBoards(e.target.value));
  numInput.addEventListener("input", e => setBoards(e.target.value));
  sourceSelect.addEventListener("change", e => {{ partSource = e.target.value; render(); }});
  feeCheckbox.addEventListener("change", e => {{ includeFeeder = e.target.checked; render(); }});
  document.getElementById("searchBox").addEventListener("input", e => {{ searchTerm = e.target.value; render(); }});

  document.querySelectorAll(".bom-preset[data-q]").forEach(btn => {{
    btn.addEventListener("click", () => setBoards(btn.dataset.q));
  }});
  document.querySelectorAll(".bom-pill").forEach(btn => {{
    btn.addEventListener("click", () => {{
      document.querySelectorAll(".bom-pill").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      activeFilter = btn.dataset.filter;
      render();
    }});
  }});
  document.querySelectorAll("table.bom-table thead th[data-sort]").forEach(th => {{
    th.addEventListener("click", () => {{
      const k = th.dataset.sort;
      if (sortKey === k) sortAsc = !sortAsc;
      else {{ sortKey = k; sortAsc = true; }}
      render();
    }});
  }});

  document.getElementById("toggleExpandAll").addEventListener("click", () => {{
    if (expandedSet.size > 0) {{
      expandedSet.clear();
    }} else {{
      computeRows().forEach(r => expandedSet.add(r._rowId));
    }}
    render();
  }});

  document.getElementById("exportCsv").addEventListener("click", () => {{
    const rows = computeRows();
    const srcLabel = partSource === "jlc" ? "JLCPCB" : "LCSC";
    const header = ["Designators", "Value", "Footprint", "LCSC Part #", "JLC Type", `${{srcLabel}} Stock`, "Qty/Board", `Total Qty (${{boardCount}} bd)`, "Unit Price (USD)", "Line Total (USD)", "Datasheet"];
    const esc = v => `"${{String(v ?? "").replace(/"/g, '""')}}"`;
    const lines = [header.map(esc).join(",")];
    for (const r of rows) {{
      lines.push([
        r.designator_str, r.comment, r.footprint, r.lcsc, r.library_type,
        r.active_stock ?? "", r.quantity, r.total_qty,
        r.unit_price !== null ? r.unit_price.toFixed(5) : "",
        r.line_total !== null ? r.line_total.toFixed(4) : "",
        r.datasheet_viewer_url || ""
      ].map(esc).join(","));
    }}
    const blob = new Blob([lines.join("\\n")], {{ type: "text/csv;charset=utf-8;" }});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${{BOM_DATA.project}}-${{BOM_DATA.version}}-bom-${{partSource}}-${{boardCount}}pcs.csv`;
    a.click();
  }});

  render();
}})();
</script>
"""


def render_bom_html(payload: Dict[str, Any], accent: str = "#10b981") -> str:
    """Standalone HTML wrapper around `render_bom_fragment` (used when opened outside docs)."""
    project_title = payload.get("display_name") or payload.get("project") or "PCB"
    version = payload.get("version") or "dev"
    fragment = render_bom_fragment(payload)
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{project_title} ({version}) — Priced BOM &amp; Availability</title>
<style>
  :root {{
    --bg: #09090b; --surface: #111113; --surface-2: #18181b; --hover: rgba(255,255,255,0.03);
    --border: #27272a; --border-strong: #3f3f46;
    --text: #fafafa; --muted: #a1a1aa;
    --accent: {accent}; --accent-soft: color-mix(in srgb, {accent} 14%, transparent);
    --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 28px; background: var(--bg); color: var(--text);
    font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    line-height: 1.55;
  }}
  .standalone-wrap {{ max-width: 1220px; margin: 0 auto; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
  <div class="standalone-wrap">{fragment}</div>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch JLC/LCSC pricing & generate interactive BOM.")
    ap.add_argument("--bom-json", required=True, help="Input bom-merged.json from build_jlcpcb.py")
    ap.add_argument("--out-dir", required=True, help="Output directory")
    ap.add_argument("--version", default="dev", help="Release version or SHA")
    ap.add_argument("--config", default=None, help="Path to ci/config.yml")
    ap.add_argument("--cache-json", default=None, help="Optional previous bom-priced.json for fallback cache")
    ap.add_argument("--offline", action="store_true", help="Skip HTTP calls (offline dry-run)")
    ap.add_argument("--embedded", action="store_true", help="(Deprecated, kept for compatibility)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    bom_data = json.loads(Path(args.bom_json).read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    images_dir = out_dir / "part-images"

    # Load optional fallback price cache keyed by LCSC code
    cached_by_lcsc: Dict[str, Dict[str, Any]] = {}
    if args.cache_json and Path(args.cache_json).is_file():
        try:
            prev = json.loads(Path(args.cache_json).read_text(encoding="utf-8"))
            for it in prev.get("items", []):
                c = (it.get("lcsc") or "").strip().upper()
                if c and it.get("found"):
                    cached_by_lcsc[c] = it
            print(f"[BOM-PRICE] Loaded fallback cache ({len(cached_by_lcsc)} parts).")
        except Exception as exc:
            print(f"[BOM-PRICE] Notice: could not load cache-json ({exc}).")

    unique_lcsc = sorted({it["lcsc"] for it in bom_data["items"] if it.get("lcsc")})
    print(f"[BOM-PRICE] Querying {len(unique_lcsc)} unique LCSC parts (offline={args.offline})...")

    def _fetch_one(code: str) -> Dict[str, Any]:
        if not args.offline:
            time.sleep(0.12)
        info = fetch_part_data(code, args.offline)
        # Fallback to cache if live lookup failed
        if not info.get("found") and code in cached_by_lcsc:
            fallback = dict(cached_by_lcsc[code])
            fallback["stale"] = True
            return fallback
        if info.get("product_image"):
            local_rel = cache_part_thumbnail(code, info["product_image"], images_dir, offline=args.offline)
            if local_rel:
                info["product_image_remote"] = info["product_image"]
                info["product_image"] = local_rel
        return info

    fetched: Dict[str, Dict[str, Any]] = {}
    failed_codes: List[str] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {pool.submit(_fetch_one, code): code for code in unique_lcsc}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                res = fut.result()
                fetched[code] = res
                if not res.get("found") and not args.offline:
                    failed_codes.append(code)
            except Exception as exc:
                print(f"[BOM-PRICE] Warning: failed {code}: {exc}")
                fetched[code] = fetch_part_data(code, offline=True)
                if not args.offline:
                    failed_codes.append(code)

    if failed_codes:
        print(
            f"::warning title=BOM Pricing Incomplete::"
            f"Could not fetch live JLCPCB/LCSC data for {len(failed_codes)} part(s): {', '.join(sorted(failed_codes)[:10])}"
        )

    enriched_items: List[Dict[str, Any]] = []
    for it in bom_data["items"]:
        code = it.get("lcsc") or ""
        info = fetched.get(code, {})
        raw_ds = info.get("datasheet_url") or it.get("datasheet", "")
        viewer_ds = info.get("datasheet_viewer_url") or _build_inline_datasheet_url(raw_ds, code)
        merged = dict(it)
        merged.update(
            {
                "found": info.get("found", False),
                "stale": bool(info.get("stale", False)),
                "mfr": info.get("mfr", ""),
                "brand": info.get("brand", ""),
                "description": info.get("description", ""),
                "library_type": info.get("library_type", "Unknown") if code else "Missing",
                "jlc_stock": info.get("jlc_stock"),
                "lcsc_stock": info.get("lcsc_stock"),
                "stock": info.get("stock"),
                "jlc_price_tiers": info.get("jlc_price_tiers", []),
                "lcsc_price_tiers": info.get("lcsc_price_tiers", []),
                "price_tiers": info.get("price_tiers", []),
                "product_image": info.get("product_image", ""),
                "datasheet_url": raw_ds,
                "datasheet_viewer_url": viewer_ds,
                "lcsc_url": info.get("lcsc_url", ""),
                "jlc_url": info.get("jlc_url", ""),
            }
        )
        enriched_items.append(merged)

    ext_fee = float(cfg["jlcpcb"].get("extended_part_fee_usd", 3.0))
    summaries = {
        str(q): compute_board_summary(enriched_items, q, ext_fee)
        for q in (1, 5, 10, 50, 100)
    }

    payload = {
        "project": cfg["project"]["name"],
        "display_name": cfg["project"].get("display_name", cfg["project"]["name"]),
        "board": cfg["project"]["board_name"],
        "version": args.version,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "extended_part_fee_usd": ext_fee,
        "total_components": bom_data["total_components"],
        "unique_parts": bom_data["unique_parts"],
        "missing_lcsc_groups": bom_data["missing_lcsc_groups"],
        "summaries": summaries,
        "items": enriched_items,
    }

    priced_json = out_dir / "bom-priced.json"
    priced_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Write static priced CSV
    prefix = f"{cfg['project']['name']}-{args.version}"
    priced_csv = out_dir / f"{prefix}-bom-priced.csv"
    with priced_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "Designators",
                "Value",
                "Footprint",
                "Qty/Board",
                "LCSC Part #",
                "JLC Type",
                "JLC Stock",
                "LCSC Stock",
                "Unit @1bd (USD)",
                "Unit @5bd (USD)",
                "Unit @10bd (USD)",
                "Unit @50bd (USD)",
                "MPN",
                "Datasheet",
            ]
        )
        for it in enriched_items:
            tiers = it["price_tiers"]
            q = it["quantity"]
            p1 = unit_price_for_qty(tiers, q * 1)
            p5 = unit_price_for_qty(tiers, q * 5)
            p10 = unit_price_for_qty(tiers, q * 10)
            p50 = unit_price_for_qty(tiers, q * 50)
            w.writerow(
                [
                    it["designator_str"],
                    it["comment"],
                    it["footprint"],
                    q,
                    it["lcsc"],
                    it["library_type"],
                    "" if it["jlc_stock"] is None else it["jlc_stock"],
                    "" if it["lcsc_stock"] is None else it["lcsc_stock"],
                    "" if p1 is None else f"{p1:.5f}",
                    "" if p5 is None else f"{p5:.5f}",
                    "" if p10 is None else f"{p10:.5f}",
                    "" if p50 is None else f"{p50:.5f}",
                    it["mfr"],
                    it["datasheet_viewer_url"],
                ]
            )

    accent = cfg.get("docs", {}).get("accent_color", "#10b981")
    bom_html = out_dir / "bom.html"
    bom_html.write_text(render_bom_html(payload, accent=accent), encoding="utf-8")

    s5 = summaries["5"]
    print(
        f"[BOM-PRICE] Done. @5 boards: parts=${s5['parts_total_usd']:.2f} "
        f"(${s5['parts_per_board_usd']:.2f}/bd) + ${s5['feeder_fees_usd']:.2f} feeder fees "
        f"({s5['in_stock_lines']}/{len(enriched_items)} lines in stock)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
