#!/usr/bin/env python3
"""Сигнал постабельности целей харвеста — предквалификация до GSA.

Тянет главную страницу каждой цели (с контроллера .50, stdlib, без внешних
зависимостей) и матчит футпринты типов линк-целей: WordPress/блог-комменты,
форумы (phpBB/vBulletin/SMF/MyBB/XenForo…), гостевые, article-submit, Joomla/Drupal.
Пишет результат в ту же БД (`data/harvest/harvest.db`, таблица `signal`).

  harvest_signal.py [--limit N]   — просигналить до N ещё не проверенных целей (по умолчанию все)
  harvest_signal.py --report      — сводка: сколько целей постабельны и по каким типам

Метрика для разбора: доля целей с формой (blog-comment/forum/guestbook/article) —
это грубый прогноз acceptance в GSA (реальный acceptance даст сам GSA-тест).
"""
from __future__ import annotations
import argparse, re, sqlite3, ssl, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent
OUT = REPO / "data" / "harvest"
DB = OUT / "harvest.db"
TARGETS = OUT / "targets.txt"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# футпринты типов целей (по HTML главной страницы). STRONG = есть форма → постабельно.
SIGS: dict[str, list[str]] = {
    "blog-comment":   [r"wp-comments-post\.php", r'id=["\']respond', r"comment-form",
                       r"leave a (reply|comment)", r"notify me of (new|follow-up)", r"disqus"],
    "forum":          [r"phpbb", r"vbulletin", r"powered by smf", r"mybb", r"xenforo", r"invision",
                       r"viewtopic\.php", r"memberlist\.php", r"ucp\.php\?mode=register", r"discourse"],
    "guestbook":      [r"guestbook", r"sign my guestbook", r"gbook\.php", r"powered by php guestbook"],
    "article-submit": [r"submit (an )?article", r"add article", r"article dashboard", r"submit (a )?guest post"],
    "wordpress":      [r"wp-content", r"wp-includes", r"/wp-json", r'generator"\s+content="wordpress'],
    "joomla":         [r"/media/system/js", r'generator"\s+content="joomla', r"/components/com_"],
    "drupal":         [r"drupal\.settings", r"/sites/default/files", r'generator"\s+content="drupal'],
}
STRONG = {"blog-comment", "forum", "guestbook", "article-submit"}   # прямой признак формы

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE
_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ctx))


def db_conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS signal(
        domain TEXT PRIMARY KEY, status INTEGER, types TEXT, postable INTEGER, ts TEXT)""")
    return c


CMS_LIKELY = {"wordpress", "joomla", "drupal"}    # платформа есть → форма ВЕРОЯТНА (комменты на постах)


def classify(html: str) -> tuple[list[str], int]:
    """types + уровень: 2 = есть форма (сильный), 1 = CMS-платформа (вероятный), 0 = нет."""
    h = html.lower()
    types = [t for t, pats in SIGS.items() if any(re.search(p, h) for p in pats)]
    if any(t in STRONG for t in types):
        return types, 2
    if any(t in CMS_LIKELY for t in types):
        return types, 1
    return types, 0


def _get(url: str):
    return _opener.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=8)


_ASSET = re.compile(r"\.(jpe?g|png|gif|css|js|svg|ico|pdf|xml|zip|webp)(\?|$)", re.I)
# разделы/служебные — НЕ посты (там нет формы коммента)
_SECTION = re.compile(r"/(blog|category|categories|tag|tags|topics?|about|contact|privacy|terms|"
                      r"page|author|feed|shop|product|cart|login|register|sitemap|wp-\w+)/?$", re.I)


def _post_score(url: str) -> int:
    """Насколько ссылка похожа на ОТДЕЛЬНЫЙ пост (там форма коммента)."""
    if _SECTION.search(url):
        return 0
    if re.search(r"/20\d\d/\d\d/[^/]+", url) or re.search(r"[?&]p=\d+", url):
        return 3                                  # дата-URL или ?p=123
    last = url.rstrip("/").rsplit("/", 1)[-1]
    if last.count("-") >= 2 or len(last) >= 18:
        return 2                                  # длинный слаг = заголовок поста
    if re.search(r"/(20\d\d|blog|article|post|news|story)/[^/]+", url, re.I):
        return 1
    return 0


def _post_links(html: str, domain: str) -> list[str]:
    """Внутренние ссылки, отсортированные по «пост-подобности» (для deep-probe)."""
    cands = set()
    for href in re.findall(r'href=["\']([^"\'#]+)', html, re.I):
        if href.startswith("//"):
            href = "http:" + href
        elif href.startswith("/"):
            href = f"http://{domain}{href}"
        elif not href.startswith("http"):
            continue
        if domain not in href or _ASSET.search(href):
            continue
        cands.add(href)
    scored = sorted(((_post_score(l), l) for l in cands), reverse=True)
    return [l for s, l in scored if s > 0]


def _read(url: str) -> str:
    return _get(url).read(90000).decode("utf-8", "ignore")


def _deep_form(html: str, domain: str) -> list[str] | None:
    """Ищет форму коммента на пост-страницах (до ~3 фетчей: 2 поста + 1 нырок раздел→пост)."""
    for link in _post_links(html, domain)[:2]:
        try:
            h = _read(link)
        except Exception:
            continue
        t, tier = classify(h)
        if tier == 2:
            return t
        for l2 in _post_links(h, domain)[:1]:     # ссылка оказалась разделом → пост оттуда
            try:
                h2 = _read(l2)
            except Exception:
                continue
            t2, tier2 = classify(h2)
            if tier2 == 2:
                return t2
    return None


def probe(domain: str, deep: bool = True) -> tuple[str, int, list[str], int]:
    for url in (f"http://{domain}/", f"https://{domain}/"):
        try:
            r = _get(url)
            html = r.read(90000).decode("utf-8", "ignore")
            types, tier = classify(html)
            if deep and tier == 1:                # CMS без формы на главной → проверить пост
                ft = _deep_form(html, domain)
                if ft:
                    types = sorted(set(types) | set(ft))
                    tier = 2
            return domain, (getattr(r, "status", 0) or 200), types, tier
        except Exception:
            continue
    return domain, 0, [], 0


def run(limit: int | None) -> None:
    if not TARGETS.exists():
        print("нет targets.txt — сначала прогоны раннера"); return
    c = db_conn()
    done = {d for (d,) in c.execute("SELECT domain FROM signal")}
    todo = [d for d in TARGETS.read_text(encoding="utf-8").split() if d and d not in done]
    if limit:
        todo = todo[:limit]
    print(f"целей к проверке: {len(todo)} (уже проверено {len(done)})")
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    n = 0
    with ThreadPoolExecutor(max_workers=30) as ex:
        batch = []
        for domain, status, types, tier in ex.map(probe, todo):
            batch.append((domain, status, ",".join(types), tier, ts))
            n += 1
            if len(batch) >= 200:
                c.executemany("INSERT OR REPLACE INTO signal VALUES (?,?,?,?,?)", batch)
                c.commit(); batch = []
                print(f"  …{n}/{len(todo)}", flush=True)
        if batch:
            c.executemany("INSERT OR REPLACE INTO signal VALUES (?,?,?,?,?)", batch)
            c.commit()
    c.close()
    print(f"готово: просигналено {n}")
    report()


def report() -> None:
    if not DB.exists():
        print("нет БД"); return
    c = db_conn()
    tot = c.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
    if not tot:
        print("сигнал пуст"); return
    ok = c.execute("SELECT COUNT(*) FROM signal WHERE status>0").fetchone()[0]
    form = c.execute("SELECT COUNT(*) FROM signal WHERE postable=2").fetchone()[0]
    likely = c.execute("SELECT COUNT(*) FROM signal WHERE postable=1").fetchone()[0]
    base = ok or 1
    print(f"== СИГНАЛ ПОСТАБЕЛЬНОСТИ ==  проверено={tot}  ответили={ok} ({round(100*ok/tot)}%)")
    print(f"    ★ ФОРМА на главной (сильный):   {form}  ({round(100*form/base)}% ответивших)")
    print(f"    ○ CMS-платформа (вероятный):    {likely}  ({round(100*likely/base)}% ответивших)")
    print(f"    · ни того ни другого:           {ok-form-likely}")
    from collections import Counter
    cnt: Counter = Counter()
    for (t,) in c.execute("SELECT types FROM signal WHERE types!=''"):
        for x in t.split(","):
            cnt[x] += 1
    print("\n▸ По типам (целей, где найден признак):")
    for t, k in cnt.most_common():
        mark = "★" if t in STRONG else " "
        print(f"   {mark} {t:<15} {k}")
    # экспорт пред-квалифицированных целей (форма или CMS) для GSA-теста
    cand = [d for (d,) in c.execute("SELECT domain FROM signal WHERE postable>=1 ORDER BY postable DESC")]
    (OUT / "postable_targets.txt").write_text("\n".join(cand) + "\n", encoding="utf-8")
    print(f"\n→ postable_targets.txt: {len(cand)} пред-квалифицированных целей (форма+CMS)")
    c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="сколько целей проверить (0 = все)")
    ap.add_argument("--report", action="store_true", help="сводка сигнала")
    a = ap.parse_args()
    if a.report:
        report()
    else:
        run(a.limit or None)
