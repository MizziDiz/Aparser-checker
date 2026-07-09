#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Orchestrator: build the Keywords_Pipeline + Footprint_Families .xlsx.

Sources, in layers (each optional except theme expansion):
  1. theme expansion   — always on, offline
  2. translation       — --translate  (Claude API; localizes themes/modifiers)
  3. suggest scraper   — --suggest     (own async scraper, proxy-aware)
  4. ai seeds          — --ai          (Claude API; localized long-tail)
Then: merge/dedupe -> weight -> write xlsx.

Examples:
  # Offline, valid table right now:
  python -m kwbuilder.build --out-xlsx gsa_geo_pipeline_keywords_v1.xlsx

  # Localized themes + suggest harvest through proxies:
  python -m kwbuilder.build --out-xlsx out.xlsx --translate \
      --suggest --engines google,bing --proxy-file proxies.txt --suggest-alpha

  # Everything on, small smoke run:
  python -m kwbuilder.build --out-xlsx out.xlsx --translate --ai \
      --suggest --suggest-max-parents 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import ai_seeds, config, expand, normalize, suggest, translate, weighting
from .models import Seed


def build(args: argparse.Namespace) -> int:
    geos = config.load_geos(args.geo_plan)
    themes, modifiers, include_bare = config.load_themes(args.themes)
    footprints = config.load_footprints(args.footprints)

    if args.only_geos:
        wanted = {g.strip().upper() for g in args.only_geos.split(",") if g.strip()}
        geos = [g for g in geos if g.unit.upper() in wanted]
        if not geos:
            print(f"No geos match --only-geos={args.only_geos}", file=sys.stderr)
            return 2

    print(
        f"[plan] geos={len(geos)} themes={len(themes)} modifiers={len(modifiers)} "
        f"footprints={sum(1 for f in footprints if f.enabled)}"
    )

    # 2) translation maps (optional)
    theme_texts, modifier_texts = expand.collect_translatable(themes, modifiers)
    combined = translate.translate_maps(
        theme_texts, modifier_texts, [g.language for g in geos], enabled=args.translate
    )
    theme_map, modifier_map = translate.split_maps(combined, theme_texts, modifier_texts)
    if combined:
        print(f"[translate] localized languages: {sorted(combined)}")

    # 1) theme expansion (always)
    theme_seeds: List[Seed] = []
    for geo in geos:
        theme_seeds.extend(
            expand.build_theme_seeds(
                geo, themes, modifiers, include_bare, theme_map, modifier_map
            )
        )
    print(f"[expand] theme seeds: {len(theme_seeds)}")

    all_seeds: List[Seed] = list(theme_seeds)

    # 3) suggest (optional)
    if args.suggest:
        engines = [e.strip() for e in args.engines.split(",") if e.strip()]
        sug = suggest.expand_via_suggest(
            theme_seeds=theme_seeds,
            geos=geos,
            engines=engines,
            proxy_file=Path(args.proxy_file) if args.proxy_file else None,
            concurrency=args.suggest_concurrency,
            alpha=args.suggest_alpha,
            retries=args.suggest_retries,
            max_parents_per_geo=args.suggest_max_parents,
        )
        print(f"[suggest] harvested seeds: {len(sug)}")
        all_seeds.extend(sug)

    # 4) ai (optional)
    if args.ai:
        ai = ai_seeds.generate_ai_seeds(themes, geos, enabled=True, per_theme=args.ai_per_theme)
        print(f"[ai] generated seeds: {len(ai)}")
        all_seeds.extend(ai)

    # merge / dedupe
    merged = normalize.normalize_seeds(
        all_seeds, min_len=args.min_len, max_len=args.max_len, max_words=args.max_words
    )
    print(f"[merge] {len(all_seeds)} -> {len(merged)} unique seeds")

    if args.max_seeds_per_geo > 0:
        merged = _cap_per_geo(merged, args.max_seeds_per_geo)
        print(f"[merge] capped to {len(merged)} after per-geo limit {args.max_seeds_per_geo}")

    # weighting
    weighting.apply_weights(merged, volume_influence=args.volume_influence)

    if not merged:
        print("No seeds produced — check configs.", file=sys.stderr)
        return 1

    from .writer import write_workbook

    out_path = Path(args.out_xlsx)
    write_workbook(out_path, merged, footprints)
    print(f"[done] wrote {out_path} — {len(merged)} seeds, {len(footprints)} footprints")
    print(f"       next: python gsa_geo_pipeline.py --input-xlsx {out_path} --out-dir parser_batches")
    return 0


def _cap_per_geo(seeds: List[Seed], cap: int) -> List[Seed]:
    """Keep the top-`cap` seeds per geo by soft-weight inputs (base*mult*volume)."""
    def rank(s: Seed) -> float:
        v = s.volume_score if s.volume_score is not None else 0.0
        return s.base_weight * s.weight_mult * (1.0 + v)

    by_geo: dict[str, List[Seed]] = {}
    for s in seeds:
        by_geo.setdefault(s.geo_unit, []).append(s)
    out: List[Seed] = []
    for _, lst in by_geo.items():
        lst.sort(key=rank, reverse=True)
        out.extend(lst[:cap])
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Build the GSA/A-Parser keyword table (.xlsx)")
    p.add_argument("--out-xlsx", required=True, type=str)
    p.add_argument("--geo-plan", type=Path, default=None)
    p.add_argument("--themes", type=Path, default=None)
    p.add_argument("--footprints", type=Path, default=None)
    p.add_argument("--only-geos", type=str, default="", help="comma-separated geo units subset")

    # normalization
    p.add_argument("--min-len", type=int, default=2)
    p.add_argument("--max-len", type=int, default=80)
    p.add_argument("--max-words", type=int, default=8)
    p.add_argument("--max-seeds-per-geo", type=int, default=0, help="0 = unlimited")

    # weighting
    p.add_argument("--volume-influence", type=float, default=0.5)

    # translation
    p.add_argument("--translate", action="store_true")

    # suggest
    p.add_argument("--suggest", action="store_true")
    p.add_argument("--engines", type=str, default="google,bing")
    p.add_argument("--proxy-file", type=str, default="")
    p.add_argument("--suggest-concurrency", type=int, default=20)
    p.add_argument("--suggest-alpha", action="store_true", help="also query seed+a..z")
    p.add_argument("--suggest-retries", type=int, default=2)
    p.add_argument("--suggest-max-parents", type=int, default=0, help="cap parents/geo, 0=all")

    # ai
    p.add_argument("--ai", action="store_true")
    p.add_argument("--ai-per-theme", type=int, default=10)

    args = p.parse_args(argv)
    return build(args)


if __name__ == "__main__":
    raise SystemExit(main())
