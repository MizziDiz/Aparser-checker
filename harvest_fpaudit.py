#!/usr/bin/env python3
"""Аудит футпринтов харвеста: по harvest.db считает выхлоп каждого footprint
(source=footprint) и ведёт denylist мёртвых, который раннер исключает из трека.

Критерий «мёртвый» = мало уникальных URL при достаточном числе прогонов
(футпринт гоняли, а он почти ничего не находит → блок/устарел). Проспекты
НЕ критерий: локализованные comment/guestbook-футпринты возвращают реальные
постабельные страницы, у которых URL не содержит скор-паттерна (0 URL-проспектов),
но для GSA они ценны — их НЕ выкидываем.

  harvest_fpaudit.py            — отчёт + обновить data/harvest/fp_denylist.txt
  harvest_fpaudit.py --dry      — только отчёт, denylist не трогать
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent
DB = REPO / "data" / "harvest" / "harvest.db"
DENYLIST = REPO / "data" / "harvest" / "fp_denylist.txt"

MIN_RUNS = 4          # футпринт должен быть прогнан хотя бы столько раз, чтобы судить
DEAD_URLS = 3         # < этого уник. URL при MIN_RUNS прогонах → мёртвый

log = logging.getLogger("fpaudit")


def audit(db: Path = DB) -> tuple[list[tuple[str, int, int, int]], list[str]]:
    """Возвращает (строки_отчёта, мёртвые_футпринты). Строка = (fp, urls, prosp, runs)."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT seed, COUNT(DISTINCT uhash), "
        "COUNT(DISTINCT CASE WHEN score>=55 THEN uhash END), COUNT(DISTINCT ts) "
        "FROM results WHERE source='footprint' GROUP BY seed ORDER BY 2 DESC"
    ).fetchall()
    conn.close()
    report = [(fp, urls, pros or 0, runs) for fp, urls, pros, runs in rows]
    dead = [fp for fp, urls, _pros, runs in report if runs >= MIN_RUNS and urls < DEAD_URLS]
    return report, dead


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="аудит выхлопа футпринтов")
    ap.add_argument("--dry", action="store_true", help="только отчёт, denylist не менять")
    a = ap.parse_args()
    if not DB.exists():
        log.error("нет harvest.db: %s", DB)
        return

    report, dead = audit()
    log.info("футпринтов в БД: %d | продуктивных: %d | мёртвых (<%d URL при >=%d прог): %d",
             len(report), sum(1 for _, u, _, _ in report if u >= DEAD_URLS), DEAD_URLS, MIN_RUNS, len(dead))
    log.info("топ-10 по URL:")
    for fp, urls, pros, runs in report[:10]:
        log.info("  %5d URL / %3d просп / %2d прог  %s", urls, pros, runs, fp[:44])
    if dead:
        log.info("МЁРТВЫЕ (в denylist):")
        for fp in dead:
            log.info("  ✗ %s", fp)

    if not a.dry:
        prev = set(DENYLIST.read_text(encoding="utf-8").split("\n")) if DENYLIST.exists() else set()
        allbad = sorted(prev | set(dead) - {""})
        DENYLIST.write_text("\n".join(allbad) + ("\n" if allbad else ""), encoding="utf-8")
        log.info("-> denylist: %s (%d футпринтов)", DENYLIST, len(allbad))


if __name__ == "__main__":
    main()
