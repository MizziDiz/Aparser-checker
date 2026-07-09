# kwbuilder — keyword table generator for `gsa_geo_pipeline.py`

Generates the two-sheet `.xlsx` (`Keywords_Pipeline` + `Footprint_Families`)
that `gsa_geo_pipeline.py` turns into A-Parser/GSA query batches. Sources are
layered and each optional layer is toggled by a flag, so you always get a valid
table even with no network, no proxies and no API keys.

## Layers

| Layer | Flag | Needs | What it adds |
|---|---|---|---|
| Theme expansion | (always on) | — | `themes × modifiers` seeds per geo, offline |
| Translation | `--translate` | `ANTHROPIC_API_KEY` | localizes themes/modifiers per `geo_language` (cached) |
| Suggest scraper | `--suggest` | proxies (recommended) | real autocomplete from Google/Bing/DDG, rank → `soft_weight` |
| AI seeds | `--ai` | `ANTHROPIC_API_KEY` | localized long-tail per theme |

Then: **merge/dedupe** (cross-source) → **multi-signal weighting** → **write xlsx**.

## Config (`kwbuilder/config/`)

- `geo_plan.yaml` — the source of truth. Each geo binds language, `hl`/`gl`
  locale, ccTLD `operators`, priority, KPI and weight multiplier. 24 geos ship;
  add/remove freely.
- `themes.yaml` — base EN themes + modifiers. **Replace with your niche.**
- `footprints.yaml` — GSA footprint library grouped by family. Disable a family
  with `enabled: false`, or a single line by prefixing it with `!`.

## Usage

```bash
# 1) Offline — valid table right now
python -m kwbuilder.build --out-xlsx gsa_geo_pipeline_keywords_v1.xlsx

# 2) Localized + real demand via proxies
python -m kwbuilder.build --out-xlsx out.xlsx \
    --translate \
    --suggest --engines google,bing --proxy-file proxies.txt --suggest-alpha \
    --max-seeds-per-geo 2000

# 3) Everything on, small smoke run
python -m kwbuilder.build --out-xlsx out.xlsx --translate --ai \
    --suggest --suggest-max-parents 3

# then feed the existing pipeline
python gsa_geo_pipeline.py --input-xlsx out.xlsx --out-dir parser_batches
```

`proxies.txt`: one proxy per line, e.g. `http://user:pass@host:port`.

## Useful flags

- `--only-geos US,DE,FR` — build a subset.
- `--max-seeds-per-geo N` — cap per geo, keeping the highest-weighted seeds.
- `--suggest-alpha` — also query `seed a`..`seed z` for much wider harvest.
- `--suggest-max-parents N` — cap parents per geo (quick smoke before full run).
- `--volume-influence 0..1` — how strongly Suggest/AI demand pulls `soft_weight`.

## Weighting

`soft_weight = clamp(base_weight × geo_weight_mult × (1 + influence × volume_score), 0.1, 2.0)`

`volume_score` (0..1) comes from Suggest rank / AI prior; absent → weight
degrades gracefully to `theme × geo`. Plug Ahrefs/Semrush later by setting
`Seed.volume_score` before `weighting.apply_weights` (see `weighting.py`).
```
