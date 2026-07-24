#!/usr/bin/env python3
"""HTML-анализ выборки: наш парсинг vs два других апарсера. Полный prospect-скоринг
(github.com/MizziDiz/common-crawl-prospect-scoring): фетч страницы → движок+футпринты →
семейство и score (55 URL / 70 движок / 90 оба). Сравнивает долю проспектов и типы.
"""
from __future__ import annotations
import glob, random, sqlite3, sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "scoring"))
sys.path.insert(0, str(REPO))
import harvest_signal as sig
from cc_links.prospects import classify_prospect

N = int(sys.argv[1]) if len(sys.argv) > 1 else 250
SH = "/srv/share/Aparser results"


def sample_ours(n):
    # в БД хранится ХЭШ URL + домен (полный путь не сохраняем by design) → берём домены
    # и фетчим главную. Конкуренты (sample_file) читаются полными URL, поэтому наша
    # выборка слегка НЕДООЦЕНИВАЕТ футпринты вглубь сайта — это предел хэш-хранилища, не баг.
    c = sqlite3.connect(f"file:{REPO}/data/harvest/harvest.db?mode=ro", uri=True)
    urls = ["http://" + d for (d,) in c.execute(
        "SELECT DISTINCT domain FROM results ORDER BY RANDOM() LIMIT ?", (n * 3,))]
    c.close()
    return random.sample(urls, min(n, len(urls)))


def sample_file(pattern, n):
    for f in sorted(glob.glob(f"{SH}/{pattern}")):
        lines = [l.strip() for l in open(f, encoding="utf-8", errors="ignore") if l.startswith("http")]
        return random.sample(lines, min(n, len(lines)))
    return []


def analyze(url):
    try:
        html = sig._read(url)
    except Exception:
        return (False, 0, None)
    try:
        matches = classify_prospect(html, url)
    except Exception:
        matches = []
    if matches:
        best = max(matches, key=lambda m: m.score)
        return (True, best.score, best.family)
    return (True, 0, None)


def run(name, urls):
    total = len(urls)
    fetched = prospects = 0
    fam = Counter()
    scores = []
    with ThreadPoolExecutor(max_workers=25) as ex:
        for ok, score, family in ex.map(analyze, urls):
            if ok:
                fetched += 1
            if score >= 55 and family:
                prospects += 1
                fam[family] += 1
                scores.append(score)
    avg = round(sum(scores) / len(scores)) if scores else 0
    fp = round(100 * prospects / fetched, 1) if fetched else 0
    print(f"\n▸ {name}: выборка {total}, ответили {fetched} ({round(100*fetched/total)}%)")
    print(f"   ПРОСПЕКТОВ (score≥55): {prospects} = {fp}% ответивших | ср.score {avg}")
    print(f"   по семействам: {', '.join(f'{k}={v}' for k,v in fam.most_common()) or '—'}")
    return {"name": name, "fetched": fetched, "prospects": prospects, "pct": fp, "avg": avg}


if __name__ == "__main__":
    print(f"выборка по {N} URL на источник, фетч HTML + классификация...")
    res = []
    res.append(run("НАШ парсинг", sample_ours(N)))
    res.append(run("yandex (апарсер)", sample_file("*yandex*10mb.txt", N)))
    res.append(run("brave (апарсер)", sample_file("*brave*10mb.txt", N)))
    print("\n=== СВОДКА (доля проспектов) ===")
    for r in res:
        print(f"  {r['name']:<20} {r['pct']:>5}% проспектов, ср.score {r['avg']}")
