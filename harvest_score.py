#!/usr/bin/env python3
"""Скоринг добытых URL по модели common-crawl-prospect-scoring (github.com/MizziDiz).

Их скоринг: score_url = 90 (HTML-движок И URL совпали) · 70 (движок) · 55 (URL-футпринт) ·
20 (платформа) · 0. Здесь доступна только URL-эвиденция (без HTML-движка/outlinks) →
score=55 при совпадении футпринта семейства (forum/blog_comment/guestbook/wiki/directory/
trackback/article_submit/image_comment/profile_page/social_bookmark).
Код правил взят из вендоренного пакета scoring/cc_links.

  harvest_score.py            — проскорить URL базы, отчёт + scored_prospects.txt
  harvest_score.py --share    — плюс положить проспекты на шару (<node>_prospects.txt)

Пути/идентификатор узла берутся из окружения (плейсхолдеры — в .env.example):
  HARVEST_SHARE_DIR  — каталог шары для выгрузки проспектов
  HARVEST_NODE_ID    — префикс имени файла на шаре (по умолчанию "node")
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "scoring"))
from cc_links.prospects import load_prospect_rules

log = logging.getLogger("harvest_score")

DB = REPO / "data" / "harvest" / "harvest.db"
OUT = REPO / "data" / "harvest" / "scored_prospects.txt"
SHARE = Path(os.environ.get("HARVEST_SHARE_DIR", "/srv/share/prospects"))
NODE_ID = os.environ.get("HARVEST_NODE_ID", "node")
URL_SCORE = 55                          # балл URL-совпадения футпринта (по модели)


def build_url_terms(footprints: list[str] | None = None) -> list[tuple[str, str]]:
    """(term, family) для URL-скоринга: term-подстрока → семейство проспекта."""
    _, rules = load_prospect_rules(footprints)
    terms: list[tuple[str, str]] = []
    for rule in rules:
        for term in rule.get("signals", {}).get("url_contains", []):
            term = term.lower()
            if term != "#respond":        # слишком широкий якорь
                terms.append((term, rule["family"]))
    return terms


def score_url(url: str, terms: list[tuple[str, str]]) -> tuple[int, list[str]]:
    """URL-часть модели: URL_SCORE при совпадении футпринта (движок/outlinks недоступны)."""
    u = (url or "").lower()
    fams = sorted({fam for term, fam in terms if term in u})
    return (URL_SCORE, fams) if fams else (0, [])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="URL-скоринг проспектов по модели cc_links")
    ap.add_argument("--share", action="store_true", help="выгрузить проспекты на шару")
    ap.add_argument("--min-score", type=int, default=URL_SCORE)
    a = ap.parse_args()

    if not DB.exists():
        log.error("нет harvest.db: %s", DB)
        return
    terms = build_url_terms()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    famc: Counter[str] = Counter()
    tot = kept = 0
    with OUT.open("w", encoding="utf-8") as f:
        for (url,) in conn.execute("SELECT DISTINCT url FROM results"):
            score, fams = score_url(url, terms)
            tot += 1
            if score >= a.min_score and fams:
                famc[fams[0]] += 1
                f.write(f"{score}\t{fams[0]}\t{url}\n")
                kept += 1
    conn.close()

    pct = round(100 * kept / tot, 1) if tot else 0.0
    log.info("URL проскорено: %d | проспектов (score>=%d): %d (%.1f%%)", tot, a.min_score, kept, pct)
    log.info("по семействам: %s", ", ".join(f"{k}={v}" for k, v in famc.most_common()))
    log.info("-> %s", OUT)

    if a.share and SHARE.exists():
        urls = sorted({l.rstrip("\n").split("\t", 2)[-1] for l in OUT.read_text(encoding="utf-8").splitlines()})
        dest = SHARE / f"{NODE_ID}_prospects.txt"
        dest.write_text("\n".join(urls) + "\n", encoding="utf-8")
        log.info("-> шара: %s (%d URL)", dest.name, len(urls))


if __name__ == "__main__":
    main()
