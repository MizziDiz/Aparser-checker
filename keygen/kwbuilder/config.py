#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Config loaders for geo plan, themes and footprint library."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

import yaml

from .models import Footprint, Geo, Theme

CONFIG_DIR = Path(__file__).parent / "config"


def _read_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data or {}


def load_geos(path: Path | None = None) -> List[Geo]:
    path = path or (CONFIG_DIR / "geo_plan.yaml")
    data = _read_yaml(path)
    kpi_by_priority: Dict[str, int] = (data.get("defaults") or {}).get(
        "kpi_by_priority", {}
    )
    geos: List[Geo] = []
    for row in data.get("geos", []):
        priority = str(row.get("priority", "P3")).strip()
        kpi = int(row.get("kpi", kpi_by_priority.get(priority, 200)))
        geos.append(
            Geo(
                unit=str(row["unit"]).strip(),
                language=str(row.get("language", "en")).strip(),
                hl=str(row.get("hl", "en")).strip(),
                gl=str(row.get("gl", "us")).strip(),
                operators=[str(o).strip() for o in (row.get("operators") or []) if str(o).strip()],
                priority=priority,
                kpi=kpi,
                weight_mult=float(row.get("weight_mult", 1.0)),
            )
        )
    if not geos:
        raise ValueError(f"No geos defined in {path}")
    return geos


def load_themes(path: Path | None = None) -> Tuple[List[Theme], List[str], bool]:
    path = path or (CONFIG_DIR / "themes.yaml")
    data = _read_yaml(path)
    themes = [
        Theme(theme=str(t["theme"]).strip(), base_weight=float(t.get("base_weight", 1.0)))
        for t in data.get("themes", [])
        if str(t.get("theme", "")).strip()
    ]
    modifiers = [str(m).strip() for m in data.get("modifiers", []) if str(m).strip()]
    include_bare = bool(data.get("include_bare", True))
    if not themes:
        raise ValueError(f"No themes defined in {path}")
    return themes, modifiers, include_bare


def load_footprints(path: Path | None = None) -> List[Footprint]:
    path = path or (CONFIG_DIR / "footprints.yaml")
    data = _read_yaml(path)
    out: List[Footprint] = []
    for family, block in (data.get("families") or {}).items():
        block = block or {}
        if not bool(block.get("enabled", True)):
            continue
        for fp in block.get("footprints", []):
            text = str(fp).strip()
            if not text:
                continue
            enabled = True
            if text.startswith("!"):  # per-line disable
                enabled = False
                text = text[1:].strip()
            out.append(Footprint(family=str(family).strip(), footprint=text, enabled=enabled))
    enabled_count = sum(1 for f in out if f.enabled)
    if enabled_count == 0:
        raise ValueError(f"No enabled footprints in {path}")
    return out
