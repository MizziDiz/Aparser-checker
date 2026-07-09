#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared dataclasses for the keyword table builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Geo:
    unit: str
    language: str
    hl: str
    gl: str
    operators: List[str]
    priority: str
    kpi: int
    weight_mult: float


@dataclass
class Theme:
    theme: str
    base_weight: float


@dataclass
class Footprint:
    family: str
    footprint: str
    enabled: bool = True


@dataclass
class Seed:
    geo_unit: str
    seed: str
    operators: List[str]
    language: str
    priority: str
    kpi: int
    # weighting inputs / output
    base_weight: float = 1.0          # from theme
    weight_mult: float = 1.0          # from geo
    volume_score: Optional[float] = None  # 0..1 from a volume/suggest signal
    soft_weight: float = 1.0          # final, clamped 0.1..2.0
    source: str = "theme"             # theme | suggest | ai
    meta: dict = field(default_factory=dict)

    def key(self) -> str:
        # dedupe identity: same seed text in same geo is one row
        return f"{self.geo_unit}\x1f{self.seed.strip().lower()}"
