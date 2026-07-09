#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Async autocomplete (Suggest) scraper — own implementation, proxy-aware.

Pluggable and toggleable via --suggest. Expands seeds using Google / Bing /
DuckDuckGo autocomplete, localized per geo (hl/gl). Suggestion rank becomes a
volume_score (0..1) feeding weighting.py.

Proxies: one per line in a file passed via --proxy-file, e.g.
    http://user:pass@host:port
Round-robin rotation with per-proxy cooldown on error. Runs without proxies too
(direct), but you will get rate-limited fast on real volumes.
"""

from __future__ import annotations

import asyncio
import json
import string
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from urllib.parse import quote

import httpx

from .models import Geo, Seed

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def load_proxies(path: Optional[Path]) -> List[str]:
    if not path:
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


class ProxyPool:
    """Round-robin proxy rotation with async-safe cursor."""

    def __init__(self, proxies: Sequence[str]):
        self._proxies = list(proxies)
        self._i = 0
        self._lock = asyncio.Lock()

    async def next(self) -> Optional[str]:
        if not self._proxies:
            return None
        async with self._lock:
            p = self._proxies[self._i % len(self._proxies)]
            self._i += 1
            return p


def _google_url(q: str, geo: Geo) -> str:
    return (
        "https://suggestqueries.google.com/complete/search"
        f"?client=firefox&hl={geo.hl}&gl={geo.gl}&q={quote(q)}"
    )


def _bing_url(q: str, geo: Geo) -> str:
    return f"https://api.bing.com/osjson.aspx?query={quote(q)}&market={geo.hl}-{geo.gl}"


def _ddg_url(q: str, geo: Geo) -> str:
    return f"https://duckduckgo.com/ac/?q={quote(q)}&kl={geo.gl}-{geo.hl}"


ENGINES = {"google": _google_url, "bing": _bing_url, "ddg": _ddg_url}


def _parse(engine: str, payload: str) -> List[str]:
    try:
        data = json.loads(payload)
    except Exception:
        return []
    if engine == "ddg":
        return [d.get("phrase", "") for d in data if isinstance(d, dict)]
    # google / bing: [query, [suggestions], ...]
    if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
        return [str(x) for x in data[1]]
    return []


async def _fetch(
    engine: str,
    q: str,
    geo: Geo,
    pool: ProxyPool,
    retries: int,
) -> List[str]:
    url = ENGINES[engine](q, geo)
    for attempt in range(retries + 1):
        proxy = await pool.next()
        try:
            kwargs = {"headers": {"User-Agent": UA}, "timeout": 15.0}
            if proxy:
                kwargs["proxy"] = proxy
            async with httpx.AsyncClient(**kwargs) as c:
                r = await c.get(url)
            if r.status_code == 200:
                return _parse(engine, r.text)
        except Exception:
            pass
        await asyncio.sleep(0.3 * (attempt + 1))
    return []


def _prefixes(seed: str, alpha: bool) -> List[str]:
    qs = [seed]
    if alpha:
        qs += [f"{seed} {c}" for c in string.ascii_lowercase]
    return qs


async def _expand_seed(
    sem: asyncio.Semaphore,
    engine: str,
    parent: Seed,
    geo: Geo,
    pool: ProxyPool,
    alpha: bool,
    retries: int,
) -> List[Seed]:
    out: List[Seed] = []
    seen: set[str] = set()
    for q in _prefixes(parent.seed, alpha):
        async with sem:
            suggestions = await _fetch(engine, q, geo, pool, retries)
        n = max(1, len(suggestions))
        for rank, text in enumerate(suggestions):
            text = text.strip()
            low = text.lower()
            if not text or low in seen:
                continue
            seen.add(low)
            out.append(
                Seed(
                    geo_unit=geo.unit,
                    seed=text,
                    operators=list(geo.operators),
                    language=geo.language,
                    priority=geo.priority,
                    kpi=geo.kpi,
                    base_weight=parent.base_weight,
                    weight_mult=geo.weight_mult,
                    volume_score=round(1.0 - rank / n, 4),
                    source="suggest",
                    meta={"engine": engine, "parent": parent.seed},
                )
            )
    return out


async def _run(
    seeds_by_geo: Dict[str, List[Seed]],
    geos: Dict[str, Geo],
    engines: List[str],
    pool: ProxyPool,
    concurrency: int,
    alpha: bool,
    retries: int,
) -> List[Seed]:
    sem = asyncio.Semaphore(concurrency)
    tasks = []
    for unit, seeds in seeds_by_geo.items():
        geo = geos[unit]
        for parent in seeds:
            for engine in engines:
                tasks.append(_expand_seed(sem, engine, parent, geo, pool, alpha, retries))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: List[Seed] = []
    for r in results:
        if isinstance(r, list):
            out.extend(r)
    return out


def expand_via_suggest(
    theme_seeds: List[Seed],
    geos: List[Geo],
    engines: List[str],
    proxy_file: Optional[Path],
    concurrency: int = 20,
    alpha: bool = False,
    retries: int = 2,
    max_parents_per_geo: int = 0,
) -> List[Seed]:
    """Blocking entry point. Returns NEW suggest-sourced seeds (not merged).

    max_parents_per_geo: cap how many theme seeds to expand per geo (0 = all).
    Use a small cap for a quick smoke run before a full harvest.
    """
    geo_map = {g.unit: g for g in geos}
    by_geo: Dict[str, List[Seed]] = {}
    for s in theme_seeds:
        by_geo.setdefault(s.geo_unit, []).append(s)
    if max_parents_per_geo > 0:
        by_geo = {u: v[:max_parents_per_geo] for u, v in by_geo.items()}

    proxies = load_proxies(proxy_file)
    if not proxies:
        print("[suggest] no proxies loaded — running direct; expect rate limits.")
    pool = ProxyPool(proxies)
    return asyncio.run(
        _run(by_geo, geo_map, engines, pool, concurrency, alpha, retries)
    )
