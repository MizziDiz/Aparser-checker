#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline seed expansion: themes x modifiers -> raw seed strings per geo.

This is the always-available source. Suggest / AI sources add to what this
produces; they never replace it, so the builder yields a valid table even with
no network, no proxies and no API keys.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .models import Geo, Seed, Theme


def build_theme_seeds(
    geo: Geo,
    themes: List[Theme],
    modifiers: List[str],
    include_bare: bool,
    theme_text_by_lang: Dict[str, Dict[str, str]] | None = None,
    modifier_text_by_lang: Dict[str, Dict[str, str]] | None = None,
) -> List[Seed]:
    """Expand themes for one geo.

    theme_text_by_lang / modifier_text_by_lang: optional translation maps
    {lang: {english_text: localized_text}}. When missing, English is used, so
    this works fully offline.
    """
    lang = geo.language
    t_map = (theme_text_by_lang or {}).get(lang, {})
    m_map = (modifier_text_by_lang or {}).get(lang, {})

    seeds: List[Seed] = []
    for th in themes:
        theme_text = t_map.get(th.theme, th.theme)
        variants: List[str] = []
        if include_bare:
            variants.append(theme_text)
        for mod in modifiers:
            mod_text = m_map.get(mod, mod)
            variants.append(f"{theme_text} {mod_text}")
        for text in variants:
            seeds.append(
                Seed(
                    geo_unit=geo.unit,
                    seed=text,
                    operators=list(geo.operators),
                    language=lang,
                    priority=geo.priority,
                    kpi=geo.kpi,
                    base_weight=th.base_weight,
                    weight_mult=geo.weight_mult,
                    source="theme",
                )
            )
    return seeds


def collect_translatable(
    themes: List[Theme], modifiers: List[str]
) -> Tuple[List[str], List[str]]:
    """Return (theme_texts, modifier_texts) that translate.py may localize."""
    return [t.theme for t in themes], list(modifiers)
