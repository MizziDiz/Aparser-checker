#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Seed cleaning, filtering and cross-source dedupe (the 'merger')."""

from __future__ import annotations

import re
from typing import Iterable, List

from .models import Seed

_WS = re.compile(r"\s+")
# Strip characters that would corrupt a query line but keep operators/quotes
# that legitimately appear in seeds. Seeds are the left side of the query only.
_STRIP = re.compile(r'[\r\n\t"|]+')


def clean_text(text: str) -> str:
    text = _STRIP.sub(" ", str(text))
    text = _WS.sub(" ", text).strip()
    return text


def normalize_seeds(
    seeds: Iterable[Seed],
    min_len: int = 2,
    max_len: int = 80,
    max_words: int = 8,
) -> List[Seed]:
    """Clean, length/word-filter and dedupe by (geo, lowercased seed).

    When two sources produce the same seed, keep the one with the higher
    volume_score (Suggest/AI carry demand signal); fall back to first seen.
    """
    best: dict[str, Seed] = {}
    for s in seeds:
        s.seed = clean_text(s.seed)
        if not s.seed:
            continue
        if not (min_len <= len(s.seed) <= max_len):
            continue
        if len(s.seed.split()) > max_words:
            continue
        k = s.key()
        cur = best.get(k)
        if cur is None:
            best[k] = s
            continue
        # merge: prefer richer signal, and remember it came from multiple sources
        new_score = s.volume_score if s.volume_score is not None else -1.0
        cur_score = cur.volume_score if cur.volume_score is not None else -1.0
        winner, loser = (s, cur) if new_score > cur_score else (cur, s)
        srcs = set(winner.meta.get("sources", [winner.source]))
        srcs.add(loser.source)
        winner.meta["sources"] = sorted(srcs)
        best[k] = winner
    return list(best.values())
