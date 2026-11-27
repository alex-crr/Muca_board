#!/usr/bin/env python3
"""Manage a multi-version GitHub Pages tree (`gh-pages` storage directory):

Layout:
  <pages-root>/
    .nojekyll
    index.html          -> instant redirect to ./latest/ (or ./main/ if no tags yet)
    versions.json       -> manifest powering the topbar version/branch switcher
    latest/             -> mirror of latest stable v* tag (or main if no tags yet)
    v1.0.0/             -> immutable tag release docs
    main/               -> rolling main branch docs
    branches/<slug>/    -> optional branch preview docs (pruned when branch is deleted)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


def _semver_key(tag: str):
    parts = re.findall(r"\d+|[a-zA-Z]+", tag.lstrip("v"))
    return [int(p) if p.isdigit() else p for p in parts]


def _load_versions(pages_root: Path) -> List[Dict[str, Any]]:
    vf = pages_root / "versions.json"
    if not vf.is_file():
        return []
    try:
        return json.loads(vf.read_text(encoding="utf-8")).get("versions", [])
    except Exception:
        return []


def _write_root_files(pages_root: Path, versions: List[Dict[str, Any]]) -> None:
    # Remove any entries whose target directory no longer exists
    valid = [v for v in versions if (pages_root / v["id"]).is_dir()]

    # Sort cleanly: latest -> tags (newest semver first) -> main -> branches (alpha)
    latest_entries = [v for v in valid if v["type"] == "latest"]
    tags = sorted(
        [v for v in valid if v["type"] == "tag"],
        key=lambda v: _semver_key(v["id"]),
        reverse=True,
    )
    main_entries = [v for v in valid if v["type"] == "branch" and v["id"] == "main"]
    other_branches = sorted(
        [v for v in valid if v["type"] == "branch" and v["id"] != "main"],
        key=lambda v: v["id"].lower(),
    )
    ordered = latest_entries + tags + main_entries + other_branches

    default_target = "latest" if latest_entries else (ordered[0]["id"] if ordered else "main")

    (pages_root / "versions.json").write_text(
        json.dumps({"default": default_target, "versions": ordered}, indent=2),
        encoding="utf-8",
    )
    (pages_root / ".nojekyll").write_text("", encoding="utf-8")
    (pages_root / "index.html").write_text(
        f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="0; url=./{default_target}/">
  <title>Redirecting…</title>
  <script>window.location.replace("./{default_target}/");</script>
</head>
<body style="background:#09090b;color:#a1a1aa;font-family:sans-serif;padding:40px">
  Redirecting to <a href="./{default_target}/" style="color:#10b981">{default_target}</a>…
</body>
</html>
""",
        encoding="utf-8",
    )


def _rewrite_root_rel_for_latest(latest_dir: Path, source_version_id: str) -> None:
    """When mirroring a site into `latest/`, ensure `data-current` and `data-root-rel` match `latest`."""
    for html_file in latest_dir.glob("*.html"):
        if html_file.name == "ibom-raw.html":
            continue
        txt = html_file.read_text(encoding="utf-8")
        txt = txt.replace(f'data-current="{source_version_id}"', 'data-current="latest"')
        txt = re.sub(r'data-root-rel="[^"]*"', 'data-root-rel=".."', txt)
        html_file.write_text(txt, encoding="utf-8")


def cmd_publish(args: argparse.Namespace) -> int:
    pages_root = Path(args.pages_root).resolve()
    site_src = Path(args.site_dir).resolve()
    pages_root.mkdir(parents=True, exist_ok=True)

    version_id = args.version_id.strip("/")
    target_dir = pages_root / version_id
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(site_src, target_dir)

    versions = [v for v in _load_versions(pages_root) if v["id"] != version_id]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    versions.append(
        {
            "id": version_id,
            "label": args.label,
            "type": args.ref_type,  # "tag" | "branch"
            "sha": args.sha,
            "updated_at": now,
        }
    )

    # Update /latest/ if explicitly requested, OR if no tag has ever been published yet
    has_any_tag = any(v["type"] == "tag" for v in versions)
    should_update_latest = args.update_latest or (not has_any_tag and version_id == "main")
    if should_update_latest:
        latest_dir = pages_root / "latest"
        if latest_dir.exists():
            shutil.rmtree(latest_dir)
        shutil.copytree(site_src, latest_dir)
        _rewrite_root_rel_for_latest(latest_dir, version_id)
        versions = [v for v in versions if v["id"] != "latest"]
        versions.append(
            {
                "id": "latest",
                "label": f"{args.label} (latest)",
                "type": "latest",
                "sha": args.sha,
                "updated_at": now,
            }
        )

    _write_root_files(pages_root, versions)
    print(f"[PAGES] Published '{version_id}' (update_latest={should_update_latest}) into {pages_root}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    pages_root = Path(args.pages_root).resolve()
    version_id = args.version_id.strip("/")
    target_dir = pages_root / version_id
    if target_dir.exists():
        shutil.rmtree(target_dir)
        print(f"[PAGES] Pruned {target_dir}")
    versions = [v for v in _load_versions(pages_root) if v["id"] != version_id]
    _write_root_files(pages_root, versions)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Manage versioned GitHub Pages tree.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_pub = sub.add_parser("publish", help="Publish a version directory and update versions.json")
    p_pub.add_argument("--pages-root", required=True)
    p_pub.add_argument("--site-dir", required=True)
    p_pub.add_argument("--version-id", required=True, help="e.g. main, v1.0.0, branches/feat-usb")
    p_pub.add_argument("--label", required=True, help="Human label shown in dropdown")
    p_pub.add_argument("--ref-type", choices=["tag", "branch"], required=True)
    p_pub.add_argument("--sha", default="")
    p_pub.add_argument("--update-latest", action="store_true")

    p_pr = sub.add_parser("prune", help="Delete a branch preview directory and update versions.json")
    p_pr.add_argument("--pages-root", required=True)
    p_pr.add_argument("--version-id", required=True)

    args = ap.parse_args()
    if args.cmd == "publish":
        return cmd_publish(args)
    if args.cmd == "prune":
        return cmd_prune(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
