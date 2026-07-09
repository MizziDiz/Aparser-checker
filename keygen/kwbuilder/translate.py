#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Optional theme/modifier translation via the Claude API.

Pluggable and toggleable: disabled unless --translate is passed AND
ANTHROPIC_API_KEY is set. When disabled or unavailable it returns empty maps,
so the builder falls back to English text and still produces a valid table.

Results are cached on disk so repeated runs cost nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"  # cheap + fast; good enough for keyword text
CACHE_PATH = Path(__file__).parent / "config" / ".translation_cache.json"


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")


def _translate_batch(texts: List[str], language: str, api_key: str) -> Dict[str, str]:
    prompt = (
        f"Translate each of the following short SEO keyword phrases from English "
        f"into {language}. Keep them natural as search queries, lowercase, no "
        f"explanations. Return a JSON object mapping each original English phrase "
        f"to its translation.\n\nPhrases:\n" + "\n".join(f"- {t}" for t in texts)
    )
    resp = httpx.post(
        API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"].strip()
    # tolerate code fences
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        text = text[4:] if text.lower().startswith("json") else text
    return json.loads(text)


def translate_maps(
    theme_texts: List[str],
    modifier_texts: List[str],
    languages: List[str],
    enabled: bool,
) -> Dict[str, Dict[str, str]]:
    """Return {lang: {english: localized}} covering themes + modifiers.

    English (and any language for which translation is skipped) is simply left
    out of the map; callers treat a missing entry as 'use the original'.
    """
    if not enabled:
        return {}
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[translate] --translate set but ANTHROPIC_API_KEY missing; using English.")
        return {}

    all_texts = theme_texts + modifier_texts
    cache = _load_cache()
    result: Dict[str, Dict[str, str]] = {}
    for lang in languages:
        if lang == "en":
            continue
        lang_cache = cache.setdefault(lang, {})
        missing = [t for t in all_texts if t not in lang_cache]
        if missing:
            try:
                got = _translate_batch(missing, lang, api_key)
                for k, v in got.items():
                    if isinstance(v, str) and v.strip():
                        lang_cache[k] = v.strip().lower()
            except Exception as exc:  # pragma: no cover - network dependent
                print(f"[translate] {lang} failed ({exc}); using English for the rest.")
        result[lang] = {t: lang_cache[t] for t in all_texts if t in lang_cache}
    _save_cache(cache)
    return result


def split_maps(
    combined: Dict[str, Dict[str, str]], theme_texts: List[str], modifier_texts: List[str]
):
    """Split the combined lang map into (theme_map, modifier_map) for expand.py."""
    theme_set, mod_set = set(theme_texts), set(modifier_texts)
    theme_map: Dict[str, Dict[str, str]] = {}
    mod_map: Dict[str, Dict[str, str]] = {}
    for lang, m in combined.items():
        theme_map[lang] = {k: v for k, v in m.items() if k in theme_set}
        mod_map[lang] = {k: v for k, v in m.items() if k in mod_set}
    return theme_map, mod_map
