#!/usr/bin/env python3
"""Майнинг из out_country_buckets: тянет HTML реальных гео-сайтов и извлекает
(1) CMS-футпринты — какие постабельные движки доминируют по гео (что усилить/добавить),
(2) нативные ключи — дописывает новые в пул сидов (само-усиление под целевые языки).

  harvest_bucketmine.py --dry [--per N]   — показать находки, пул не трогать
  harvest_bucketmine.py [--per N]          — намайнить и дописать ключи в пул
"""
from __future__ import annotations

import argparse
import logging
import random
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import harvest_signal as sig
import harvest_runner as run
from harvest_seedmine import _phrases, _good

log = logging.getLogger("bucketmine")
BUCKETS = Path("/srv/share/Split/out_country_buckets")

# целевые бакеты (файл → язык для ключей)
GEOS = {"latam": "es", "Argentina": "es", "Colombia": "es", "Chile": "es", "brazil": "pt",
        "Portugal": "pt", "turkish": "tr", "France": "fr", "Poland": "pl", "japanese": "ja",
        "thai": "th", "vietnam": "vi", "Indonesia": "id", "Malaysia": "ms", "china-mix": "zh",
        "australia": "en", "africa": "en"}

# постабельные CMS: имя → есть ли у нас футпринт (по GSA_FOOTPRINTS)
CMS_RX = re.compile(
    r"\b(wordpress|joomla|drupal|phpbb|vbulletin|mybb|xenforo|invision|ip\.board|mediawiki|"
    r"dokuwiki|pmwiki|tiki|moinmoin|discuz|smf|punbb|fluxbb|yabb|vanilla|phorum|bbpress|"
    r"coppermine|4images|piwigo|gnuboard|serendipity|b2evolution|movable type|nucleus|"
    r"pligg|scuttle|phpld|wpforo|flarum|nodebb|phpfox|elgg|dolphin|oxwall)\b", re.I)
GEN_RX = re.compile(r'<meta[^>]+name=["\']?generator["\']?[^>]+content=["\']([^"\']{2,40})', re.I)
POW_RX = re.compile(r'(?:powered by|desarrollado por|desenvolvido por|propulsé par)\s+([A-Za-z][\w.\- ]{1,18})', re.I)


def _sample(name: str, per: int) -> list[str]:
    f = BUCKETS / f"{name}.txt"
    if not f.exists():
        return []
    lines = [l.strip() for l in f.read_text(encoding="utf-8", errors="ignore").splitlines() if l.startswith("http")]
    return random.sample(lines, min(per, len(lines)))


def _probe(url: str) -> tuple[list[str], list[str], list[str]]:
    """(cms-имена, generator/powered-by-строки, ключи) с одной страницы."""
    try:
        html = sig._read(url)
    except Exception:
        return [], [], []
    cms = [m.lower() for m in CMS_RX.findall(html)]
    sigs = [g.strip() for g in GEN_RX.findall(html)] + [p.strip() for p in POW_RX.findall(html)]
    brand = set(re.split(r"[./\-]", url))
    keys = [p for p in _phrases(html) if _good(p, brand)]
    return cms, sigs, keys


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="майнинг футпринтов+ключей из out_country_buckets")
    ap.add_argument("--dry", action="store_true", help="не дописывать ключи в пул")
    ap.add_argument("--per", type=int, default=25, help="сколько URL на гео опросить")
    a = ap.parse_args()
    if not BUCKETS.exists():
        log.error("нет каталога buckets: %s", BUCKETS)
        return

    have_fp = " ".join(run.GSA_FOOTPRINTS).lower()          # что уже покрываем
    cms_all: Counter[str] = Counter()
    key_freq: Counter[str] = Counter()
    fetched = 0
    for name, lang in GEOS.items():
        urls = _sample(name, a.per)
        if not urls:
            continue
        cms_geo: Counter[str] = Counter()
        with ThreadPoolExecutor(max_workers=20) as ex:
            for cms, _sigs, keys in ex.map(_probe, urls):
                if cms or keys:
                    fetched += 1
                cms_geo.update(cms)
                key_freq.update(keys)
        cms_all.update(cms_geo)
        top = ", ".join(f"{k}={v}" for k, v in cms_geo.most_common(4))
        log.info("  %-11s (%s): CMS %s", name, lang, top or "—")

    log.info("\n=== CMS по всем гео (что доминирует) ===")
    for cms, n in cms_all.most_common(15):
        covered = "✓" if cms.replace(" ", "") in have_fp.replace(" ", "") or cms in have_fp else "← НЕТ футпринта"
        log.info("  %-14s %4d  %s", cms, n, covered)

    known = {s.lower() for s in run.build_base_seeds()} | {s.lower() for s in run.load_pool()}
    deny = run._BAD_SEED
    # UI-шум (навигация/чром) — не топикальные ключи
    STOP = {"warning", "archives", "comment", "comments", "login", "register", "search",
            "menu", "home", "about", "contact", "reply", "posted", "categories", "tags",
            "admin", "password", "username", "submit", "cancel", "loading", "read more",
            "next", "previous", "post navigation", "leave a reply", "recent posts", "powered by"}
    def ok(p: str) -> bool:
        pl = p.lower()
        if p in known or len(p.split()) > 5 or deny.search(pl) or not any(c.isalpha() for c in p):
            return False
        return pl not in STOP and not any(pl.startswith(s) or pl.endswith(s) for s in STOP)
    fresh = [p for p, n in key_freq.most_common() if n >= 3 and ok(p)]
    log.info("\n=== новые нативные ключи (частотные, топ-20) ===")
    log.info("  %s", ", ".join(fresh[:20]) or "—")

    if fresh and not a.dry:
        with run.POOL.open("a", encoding="utf-8") as f:
            f.write("\n".join(fresh) + "\n")
        log.info("-> дописано в пул: %d ключей (пул теперь %d)", len(fresh), len(run.load_pool()))
    log.info("опрошено страниц: %d", fetched)


if __name__ == "__main__":
    main()
