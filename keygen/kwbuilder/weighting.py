#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multi-signal soft_weight computation.

soft_weight blends whatever signals are enabled and available:
    - base_weight   : theme intrinsic weight (always present)
    - weight_mult   : geo priority multiplier (always present)
    - volume_score  : 0..1 demand signal from Suggest rank / Ahrefs / Semrush
                      (optional; None when no such source ran for this seed)

Each external signal is pluggable and toggleable upstream: if it did not run,
volume_score stays None and the weight degrades gracefully to theme x geo only.
Final value is clamped to the 0.1..2.0 range the pipeline expects.
"""

from __future__ import annotations

from typing import Iterable, List

from .models import Seed

WEIGHT_MIN = 0.1
WEIGHT_MAX = 2.0


def apply_weights(seeds: Iterable[Seed], volume_influence: float = 0.5) -> List[Seed]:
    """Compute final soft_weight in place.

    volume_influence in 0..1: how much a present volume_score pulls the weight
    up (1.0 = volume can add up to +100% before clamping; 0 = ignore volume).
    """
    out: List[Seed] = []
    for s in seeds:
        w = s.base_weight * s.weight_mult
        if s.volume_score is not None:
            # volume_score 0..1 -> multiplier 1 .. (1 + volume_influence)
            w *= 1.0 + volume_influence * max(0.0, min(1.0, s.volume_score))
        s.soft_weight = round(max(WEIGHT_MIN, min(w, WEIGHT_MAX)), 3)
        out.append(s)
    return out
