#!/usr/bin/env python3
"""Майнинг сидов из HTML уже добытых гео-страниц — нативные термины без API.

Тянет главные страницы выборки доменов (по зонам = охват гео), вынимает кандидаты
из <meta keywords> / <title> / <meta description> / <h1-2>, чистит и оставляет
ЧАСТОТНЫЕ фразы (встречаются на >= min_freq разных сайтах = топикальные, не бренд-шум).
Новые (не в базе/пуле) дописывает в тот же пул сидов, что растит Suggest.
Само-усиление: харвест → майнинг сидов → харвест. Переиспользует фетчер harvest_signal.

  harvest_seedmine.py --dry [--limit N]   — показать топ намайненного, не записывать
  harvest_seedmine.py [--limit N]          — намайнить и дописать в пул
"""
from __future__ import annotations
import argparse, re, sqlite3, sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import harvest_signal as sig
import harvest_runner as run

OUT = REPO / "data" / "harvest"
DB = OUT / "harvest.db"
POOL = OUT / "seeds_expanded.txt"

# частые служебные слова латиницы/EU (фраза целиком из них — мусор)
STOP = set("""the and for with your you are our get best top online home page site the of to in
und der die das fur mit von den ist auf ein eine sie wir zu im
les des une pour avec sur est vous nous par le la du en
los las una para con por del que como el en
di il la le per con una del che come su
""".split())
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿←-⇿⬀-⯿←-⯿]")


def _clean(t: str) -> str:
    t = EMOJI.sub(" ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"&#?[a-z0-9]+;", " ", t)
    t = re.sub(r"[^\w\s\-]", " ", t, flags=re.U)
    return re.sub(r"\s+", " ", t).strip().lower()


def _phrases(html: str) -> list[str]:
    out: list[str] = []
    for m in re.findall(r'<meta[^>]+name=["\']keywords["\'][^>]+content=["\']([^"\']+)', html, re.I):
        out += [_clean(k) for k in m.split(",")]
    segs: list[str] = []
    t = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if t:
        segs += re.split(r"[|\-–—•·/:»«]", t.group(1))
    for m in re.findall(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', html, re.I):
        segs += re.split(r"[|\-–—•·/:.,»«]", m)
    for h in re.findall(r"<h[12][^>]*>(.*?)</h[12]>", html, re.I | re.S)[:4]:
        segs.append(h)
    out += [_clean(s) for s in segs]
    return out


def _good(seed: str, brand: set[str]) -> bool:
    w = seed.split()
    if not (1 <= len(w) <= 4) or not (4 <= len(seed) <= 40):
        return False
    if re.search(r"\d{2,}", seed):                       # годы/версии/телефоны
        return False
    if any(tok and tok in brand for tok in w):           # имя домена/бренда
        return False
    if all(x in STOP for x in w):                        # только стоп-слова
        return False
    return True


def mine(limit: int, min_freq: int) -> tuple[list[tuple[str, int]], int]:
    c = sqlite3.connect(DB)
    zones = [z for (z,) in c.execute(
        "SELECT zone FROM results WHERE zone!='' GROUP BY zone "
        "ORDER BY COUNT(DISTINCT domain) DESC LIMIT 30")]
    per = max(5, limit // max(1, len(zones)))
    doms: list[str] = []
    for z in zones:
        doms += [d for (d,) in c.execute(
            "SELECT domain FROM results WHERE zone=? GROUP BY domain LIMIT ?", (z, per))]
    c.close()
    doms = doms[:limit]

    def work(d: str):
        try:
            html = sig._read(f"http://{d}/")
        except Exception:
            return []
        brand = {x for x in re.split(r"[.\-]", d) if x}
        return list({p for p in _phrases(html) if _good(p, brand)})

    cnt: Counter = Counter()
    with ThreadPoolExecutor(max_workers=30) as ex:
        for ps in ex.map(work, doms):
            cnt.update(ps)
    known = {s.lower() for s in run.build_base_seeds()} | {s.lower() for s in run.load_pool()}
    fresh = [(p, n) for p, n in cnt.most_common() if n >= min_freq and p not in known]
    return fresh, len(doms)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=400, help="сколько страниц опросить")
    ap.add_argument("--min-freq", type=int, default=3, help="мин. число сайтов с фразой")
    ap.add_argument("--dry", action="store_true", help="показать, не записывать")
    a = ap.parse_args()
    fresh, n = mine(a.limit, a.min_freq)
    print(f"опрошено страниц: {n} | новых частотных сидов: {len(fresh)}")
    for p, k in fresh[:40]:
        print(f"  {k:>3}×  {p}")
    if not a.dry and fresh:
        with POOL.open("a", encoding="utf-8") as f:
            f.write("\n".join(p for p, _ in fresh) + "\n")
        print(f"→ дописано в пул: {len(fresh)} (пул теперь {len(run.load_pool())})")


if __name__ == "__main__":
    main()
