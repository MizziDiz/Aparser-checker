#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write the two-sheet .xlsx consumed by gsa_geo_pipeline.py.

Sheet headers must match what the pipeline's norm_header() expects:
  Keywords_Pipeline : pipeline_id, geo_unit, seed, operators, soft_weight,
                      kpi_weekly_target, original_priority, geo_language
  Footprint_Families: footprint_id, family, footprint, enabled
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from openpyxl import Workbook

from .models import Footprint, Seed

KW_HEADERS = [
    "pipeline_id",
    "geo_unit",
    "seed",
    "operators",
    "soft_weight",
    "kpi_weekly_target",
    "original_priority",
    "geo_language",
]
FP_HEADERS = ["footprint_id", "family", "footprint", "enabled"]


def write_workbook(path: Path, seeds: List[Seed], footprints: List[Footprint]) -> None:
    wb = Workbook()

    ws_kw = wb.active
    ws_kw.title = "Keywords_Pipeline"
    ws_kw.append(KW_HEADERS)
    for pid, s in enumerate(seeds, start=1):
        ws_kw.append(
            [
                pid,
                s.geo_unit,
                s.seed,
                "|".join(s.operators),
                s.soft_weight,
                s.kpi,
                s.priority,
                s.language,
            ]
        )

    ws_fp = wb.create_sheet("Footprint_Families")
    ws_fp.append(FP_HEADERS)
    for fid, fp in enumerate(footprints, start=1):
        ws_fp.append([fid, fp.family, fp.footprint, 1 if fp.enabled else 0])

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
