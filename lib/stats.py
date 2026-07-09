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
CREATE TABLE IF NOT EXISTS task_snapshots(
  ts INTEGER, task TEXT, status TEXT,
  done INTEGER, total INTEGER, failed_pct REAL,
  speed_cur INTEGER, speed_avg INTEGER,
  results_uniq INTEGER, results_all INTEGER
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON task_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snap_task ON task_snapshots(task, ts);
CREATE INDEX IF NOT EXISTS idx_meta_ts ON results_meta(ts_parsed);
CREATE INDEX IF NOT EXISTS idx_zone ON domain_zones(zone);
CREATE INDEX IF NOT EXISTS idx_op ON query_operators(operator);
"""


def _connect() -> sqlite3.Connection:
    STATS_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(STATS_DB, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")     # два писателя (монитор + --stats)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


# --------------------------------------------------------------------------- #
# Снимки метрик заданий + ретенция
# --------------------------------------------------------------------------- #
def _get_meta_int(con, key: str, default: int = 0) -> int:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    try:
        return int(row[0]) if row else default
    except (TypeError, ValueError):
        return default


def cleanup_old(con, days: int) -> None:
    """Удаляет данные старше N дней (снимки заданий и разобранные результаты)."""
    cutoff = int(time.time()) - days * 86400
    con.execute("DELETE FROM task_snapshots WHERE ts < ?", (cutoff,))
    con.execute("DELETE FROM domain_zones WHERE result_key IN "
                "(SELECT result_key FROM results_meta WHERE ts_parsed < ?)", (cutoff,))
    con.execute("DELETE FROM query_operators WHERE result_key IN "
                "(SELECT result_key FROM results_meta WHERE ts_parsed < ?)", (cutoff,))
    con.execute("DELETE FROM results_meta WHERE ts_parsed < ?", (cutoff,))
    con.commit()


def maybe_cleanup(con, days: int) -> None:
    """Чистка не чаще раза в 6 часов (по отметке в meta)."""
    if days <= 0:
        return
    if time.time() - _get_meta_int(con, "last_cleanup") >= 6 * 3600:
        cleanup_old(con, days)
        con.execute("INSERT OR REPLACE INTO meta VALUES('last_cleanup', ?)", (str(int(time.time())),))
        con.commit()


def progress_info(cfg: dict, cards: list[dict]) -> list[dict]:
    """По каждому заданию с total>0, done>0 считает остаток и ETA (сек) из скорости,
    оценённой по снимкам за окно eta_window_min (Δdone/Δвремя). ETA=None, если данных
    ещё нет. Вызывать ПОСЛЕ record_snapshots (в БД уже есть свежий снимок)."""
    window = float(cfg.get("eta_window_min", 30) or 30)
    now = int(time.time())
    out: list[dict] = []
    con = _connect()
    try:
        for c in cards:
            total, done = c.get("total", 0), c.get("done", 0)
            if total <= 0 or done <= 0:
                continue
            remaining = total - done
            row = con.execute(
                "SELECT ts, done FROM task_snapshots WHERE task=? AND ts>=? AND ts<? "
                "AND done<=? ORDER BY ts ASC LIMIT 1",
                (c.get("title", "?"), now - int(window * 60), now, done)).fetchone()
            eta = None
            if row:
                ts0, done0 = row
                dt, dd = now - ts0, done - done0
                if dt > 0 and dd > 0:
                    eta = remaining * dt / dd            # remaining / (dd/dt)
            out.append({"title": c.get("title", "?"), "done": done, "total": total,
                        "remaining": remaining, "pct": done / total,
                        "eta_sec": eta, "status": c.get("status", "")})
    finally:
        con.close()
    return out


def record_snapshots(cfg: dict, cards: list[dict], logger: logging.Logger) -> None:
    """Пишет снимок метрик по прогрессирующим заданиям (done>0). Вызывается из
    обычного прохода монитора. Заодно запускает ретенцию (с троттлингом)."""
    days = int(cfg.get("stats_retention_days", 30) or 0)
    rows = [(int(time.time()), c.get("title", "?"), c.get("status", ""),
             c.get("done", 0), c.get("total", 0), c.get("failed_pct"),
             c.get("speed_cur", 0), c.get("speed_avg", 0),
             c.get("results_uniq", 0), c.get("results_all", 0))
            for c in cards if c.get("done", 0) > 0]
    con = _connect()
    try:
        if rows:
            con.executemany(
                "INSERT INTO task_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
            logger.debug(f"stats: снимков заданий записано {len(rows)}")
        maybe_cleanup(con, days)
    finally:
        con.close()


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
    maybe_cleanup(con, int(cfg.get("stats_retention_days", 30) or 0))
    con.close()
    if parsed:
        logger.info(f"stats: обработано результатов: {parsed}")
