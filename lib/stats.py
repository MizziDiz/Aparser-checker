#!/usr/bin/env python3
"""
lib/stats.py — статистика по результатам и запросам A-Parser в SQLite.

За проход (--stats):
  1. Обходит results_dir рекурсивно; каждый «устоявшийся» файл результата
     разбирает один раз (инкрементально по сигнатуре mtime:size).
  2. Из результата (URL по строке) достаёт доменные зоны через tldextract.
  3. Из соответствующего query-файла (queries_dir/<тот же путь> или по имени)
     достаёт операторы запросов и их значения:
        site:.com            → operator=site,        value=.com
        instreamset:(url):.org → operator=instreamset, param=url, value=.org
  4. Пишет в data/aparser_stats.db (таблицы domain_zones, query_operators,
     results_meta). Повторно тот же результат не разбирает.

Нужен tldextract: py -m pip install tldextract (работает офлайн из бандла).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

from aparser_monitor import DATA_DIR

STATS_DB = DATA_DIR / "aparser_stats.db"

# оператор[:(параметр)]:значение → site:.com | inurl:foo | instreamset:(url):.org
OP_RE = re.compile(r"(\w+):(?:\(([^)]*)\):)?(\S+)")
# считаем операторами только известные слова (иначе поймаем случайные «токен:...»)
KNOWN_OPS = {"site", "inurl", "intitle", "intext", "inbody", "instreamset",
             "insubject", "inpostauthor", "inposttitle", "url", "host", "domain"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS results_meta(
  result_key   TEXT PRIMARY KEY,
  task         TEXT,
  query_file   TEXT,
  result_lines INTEGER,
  domains_total INTEGER,
  ts_parsed    INTEGER,
  sig          TEXT
);
CREATE TABLE IF NOT EXISTS domain_zones(
  result_key TEXT, zone TEXT, count INTEGER,
  PRIMARY KEY(result_key, zone)
);
CREATE TABLE IF NOT EXISTS query_operators(
  result_key TEXT, operator TEXT, param TEXT, value TEXT, count INTEGER,
  PRIMARY KEY(result_key, operator, param, value)
);
"""


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(STATS_DB)
    con.executescript(SCHEMA)
    return con


def _signature(p: Path) -> str:
    st = p.stat()
    return f"{int(st.st_mtime)}:{st.st_size}"


def _settled(p: Path, settle_min: float) -> bool:
    try:
        return (time.time() - p.stat().st_mtime) >= settle_min * 60
    except OSError:
        return False


def parse_operators(query_file: Path) -> Counter:
    """{(operator, param, value): count} по всем строкам query-файла."""
    ops: Counter = Counter()
    try:
        with query_file.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                for m in OP_RE.finditer(line):
                    op = m.group(1).lower()
                    if op in KNOWN_OPS:
                        ops[(op, m.group(2) or "", m.group(3))] += 1
    except OSError:
        pass
    return ops


def parse_zones(result_file: Path, extract) -> tuple[Counter, int, int]:
    """(зоны, всего_строк, доменов_распознано) по файлу результата (URL по строке)."""
    zones: Counter = Counter()
    lines = domains = 0
    try:
        with result_file.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                lines += 1
                suffix = extract(line).suffix
                if suffix:
                    zones[suffix] += 1
                    domains += 1
    except OSError:
        pass
    return zones, lines, domains


def _find_query(queries_dir: Path, rel: Path, name: str) -> Path | None:
    cand = queries_dir / rel
    if cand.exists():
        return cand
    for root, _dirs, files in os.walk(queries_dir):
        if name in files:
            return Path(root) / name
    return None


def run_stats(cfg: dict, logger: logging.Logger) -> None:
    rd = cfg.get("results_dir", "")
    if not rd:
        return
    results_dir = Path(rd)
    queries_dir = Path(cfg.get("queries_dir", "") or rd)
    settle = float(cfg.get("stats_settle_min", 2) or 0)
    if not results_dir.exists():
        logger.warning(f"stats: нет папки results: {results_dir}")
        return
    try:
        import tldextract
    except ImportError:
        logger.error("stats: нужен tldextract — py -m pip install tldextract")
        return
    extract = tldextract.TLDExtract(suffix_list_urls=[])  # офлайн, из встроенного списка

    con = _connect()
    done = {k: s for k, s in con.execute("SELECT result_key, sig FROM results_meta")}
    parsed = 0
    for root, _dirs, files in os.walk(results_dir):
        for fn in files:
            rf = Path(root) / fn
            if not _settled(rf, settle):
                continue
            key = str(rf.relative_to(results_dir))
            sig = _signature(rf)
            if done.get(key) == sig:
                continue
            zones, lines, domains = parse_zones(rf, extract)
            qf = _find_query(queries_dir, Path(key), fn)
            ops = parse_operators(qf) if qf else Counter()
            task = Path(key).parts[0] if Path(key).parts else key
            con.execute("DELETE FROM domain_zones WHERE result_key=?", (key,))
            con.execute("DELETE FROM query_operators WHERE result_key=?", (key,))
            con.executemany("INSERT INTO domain_zones VALUES(?,?,?)",
                            [(key, z, c) for z, c in zones.items()])
            con.executemany("INSERT INTO query_operators VALUES(?,?,?,?,?)",
                            [(key, o, p, v, c) for (o, p, v), c in ops.items()])
            con.execute("INSERT OR REPLACE INTO results_meta VALUES(?,?,?,?,?,?,?)",
                        (key, task, str(qf) if qf else "", lines, domains,
                         int(time.time()), sig))
            con.commit()
            parsed += 1
            logger.info(f"stats: {key} — строк {lines}, доменов {domains}, "
                        f"операторов {sum(ops.values())}")
    con.close()
    if parsed:
        logger.info(f"stats: обработано результатов: {parsed}")
