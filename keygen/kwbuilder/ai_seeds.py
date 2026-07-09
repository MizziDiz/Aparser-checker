#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Optional AI seed generation via the Claude API.

Pluggable and toggleable via --ai. For each geo it asks Claude for localized
long-tail keyword variants of the base themes. Disabled unless the flag is set
and ANTHROPIC_API_KEY is present; otherwise returns [] and the builder relies on
theme expansion + Suggest.
"""

from __future__ import annotations

import json
import os
from typing import List

import httpx

from .models import Geo, Seed, Theme

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"


def _ask(themes: List[str], geo: Geo, per_theme: int, api_key: str) -> List[str]:
    prompt = (
        f"You generate SEO keyword seeds for link prospecting in {geo.language} "
        f"(country {geo.gl}). For each of these themes, give {per_theme} realistic, "
        f"commonly-searched keyword phrases in {geo.language}, lowercase, 2-6 words, "
        f"no duplicates, no numbering.\n\nThemes:\n"
        + "\n".join(f"- {t}" for t in themes)
        + "\n\nReturn a JSON array of strings only."
    )
    r = httpx.post(
        API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": MODEL, "max_tokens": 3000, "messages": [{"role": "user", "content": prompt}]},
        timeout=90.0,
    )
    r.raise_for_status()
    text = r.json()["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        text = text[4:] if text.lower().startswith("json") else text
    data = json.loads(text)
    return [str(x).strip() for x in data if str(x).strip()]


def generate_ai_seeds(
    themes: List[Theme],
    geos: List[Geo],
    enabled: bool,
    per_theme: int = 10,
) -> List[Seed]:
    if not enabled:
        return []
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[ai] --ai set but ANTHROPIC_API_KEY missing; skipping AI seeds.")
        return []

    theme_texts = [t.theme for t in themes]
    base_weight = sum(t.base_weight for t in themes) / max(1, len(themes))
    out: List[Seed] = []
    for geo in geos:
        try:
            phrases = _ask(theme_texts, geo, per_theme, api_key)
        except Exception as exc:  # pragma: no cover - network dependent
            print(f"[ai] {geo.unit} failed ({exc}); skipping.")
            continue
        for text in phrases:
            out.append(
                Seed(
                    geo_unit=geo.unit,
                    seed=text,
                    operators=list(geo.operators),
                    language=geo.language,
                    priority=geo.priority,
                    kpi=geo.kpi,
                    base_weight=base_weight,
                    weight_mult=geo.weight_mult,
                    volume_score=0.5,  # neutral demand prior for AI-invented seeds
                    source="ai",
                )
            )
    return out
