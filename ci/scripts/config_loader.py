#!/usr/bin/env python3
"""Shared configuration loader and KiCad 10 S-expression helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

LCSC_RE = re.compile(r"^C\d{4,}$", re.IGNORECASE)


def find_repo_root(start: Optional[Path] = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    for p in [cur, *cur.parents]:
        if (p / "ci" / "config.yml").is_file():
            return p
    return cur


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    if config_path:
        path = Path(config_path).resolve()
        # Config lives at <repo_root>/ci/config.yml
        root = path.parent.parent if path.parent.name == "ci" else path.parent
    else:
        root = find_repo_root()
        path = root / "ci" / "config.yml"
    if not path.is_file():
        raise FileNotFoundError(f"CI config not found at {path}")
    if yaml is None:
        raise RuntimeError("PyYAML is required: pip install PyYAML")
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["_root"] = str(root)
    return cfg


def resolve_project_path(cfg: Dict[str, Any], key: str) -> Path:
    root = Path(cfg["_root"])
    return (root / cfg["project"][key]).resolve()


def normalize_lcsc(raw: str) -> str:
    val = (raw or "").strip().upper()
    if LCSC_RE.match(val):
        return val
    return ""


def _tokenize_sexpr(text: str):
    """Fast streaming tokenizer for KiCad S-expressions."""
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(" or c == ")":
            yield c
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n:
                ch = text[j]
                if ch == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                elif ch == '"':
                    j += 1
                    break
                else:
                    buf.append(ch)
                    j += 1
            yield "".join(buf)
            i = j
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            yield text[i:j]
            i = j


def parse_pcb_footprints(pcb_path: Path) -> Dict[str, Dict[str, Any]]:
    """Extract footprint metadata (reference, value, footprint, layer, dnp, LCSC, datasheet)
    directly from a KiCad 8/9/10 .kicad_pcb file.

    This guarantees we capture LCSC part numbers even when stored on PCB footprints
    (e.g. 'LCSC Part #', 'LCSC Part # ', 'LCSC Part', 'LCSC') rather than the schematic.
    """
    text = pcb_path.read_text(encoding="utf-8", errors="replace")
    tokens = _tokenize_sexpr(text)

    footprints: Dict[str, Dict[str, Any]] = {}
    depth = 0

    for tok in tokens:
        if tok == "(":
            depth += 1
            if depth == 2:
                head = next(tokens, None)
                if head == "footprint":
                    fp_name = next(tokens, "")
                    fp_data: Dict[str, Any] = {
                        "footprint": fp_name,
                        "reference": "",
                        "value": "",
                        "layer": "",
                        "dnp": False,
                        "exclude_from_bom": False,
                        "exclude_from_pos": False,
                        "lcsc": "",
                        "datasheet": "",
                        "properties": {},
                    }
                    # Parse inside (footprint ...) until depth returns to 1
                    sub_depth = 2
                    while sub_depth >= 2:
                        t = next(tokens, None)
                        if t is None:
                            break
                        if t == "(":
                            sub_depth += 1
                            if sub_depth == 3:
                                sub_head = next(tokens, None)
                                if sub_head == "layer":
                                    fp_data["layer"] = next(tokens, "")
                                elif sub_head == "property":
                                    prop_key = next(tokens, "")
                                    prop_val = next(tokens, "")
                                    k_clean = prop_key.strip()
                                    fp_data["properties"][k_clean] = prop_val.strip()
                                    if k_clean == "Reference":
                                        fp_data["reference"] = prop_val.strip()
                                    elif k_clean == "Value":
                                        fp_data["value"] = prop_val.strip()
                                    elif k_clean == "Datasheet":
                                        fp_data["datasheet"] = prop_val.strip()
                                    elif (
                                        "lcsc" in k_clean.lower()
                                        and "name" not in k_clean.lower()
                                    ):
                                        cand = normalize_lcsc(prop_val)
                                        if cand and not fp_data["lcsc"]:
                                            fp_data["lcsc"] = cand
                                elif sub_head == "attr":
                                    # Read all attributes inside (attr ...)
                                    while sub_depth >= 3:
                                        at = next(tokens, None)
                                        if at is None or at == ")":
                                            sub_depth -= 1
                                            break
                                        if at == "dnp":
                                            fp_data["dnp"] = True
                                        elif at == "exclude_from_bom":
                                            fp_data["exclude_from_bom"] = True
                                        elif at == "exclude_from_pos_files":
                                            fp_data["exclude_from_pos"] = True
                        elif t == ")":
                            sub_depth -= 1
                    depth = 1
                    ref = fp_data["reference"]
                    if ref and not ref.startswith("#"):
                        footprints[ref] = fp_data
        elif tok == ")":
            depth -= 1

    return footprints


def kicad_env() -> Dict[str, str]:
    """Return clean environment for headless kicad-cli execution."""
    env = os.environ.copy()
    env.setdefault("LC_ALL", "C.UTF-8")
    env.setdefault("LANG", "C.UTF-8")
    return env
