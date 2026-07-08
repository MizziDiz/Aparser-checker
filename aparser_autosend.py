#!/usr/bin/env python3
"""
aparser_autosend.py — копирование готовых результатов A-Parser на другой сервер.

Логика (матчинг по именам из Queries):
  1. Берём имена файлов из папки Queries (`queries_dir`).
  2. В папке results (`results_dir`) ищем одноимённые результаты — файл или папку
     (совпадение по полному имени или по имени без расширения). Внутрь совпавшей
     папки не спускаемся.
  3. Если результат «устоялся» (не менялся `autosend_settle_min` минут — значит
     запись завершена) и его ещё не отправляли в этой версии — копируем в
     `autosend_dest` (обычно UNC-путь \\SERVER\share\...).
  4. Отправленное запоминаем в state (по относительному пути + mtime/размер), чтобы
     не слать повторно; при изменении результата отправим заново.

Все три пути (`queries_dir`, `results_dir`, `autosend_dest`) задаются в конфиге в
raw-формате. Если что-то из них пусто — autosend выключен.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from aparser_monitor import send_telegram


def _query_names(queries_dir: Path) -> set[str]:
    """Имена задач из Queries — и с расширением, и без (для матчинга папок/файлов)."""
    names: set[str] = set()
    if not queries_dir.exists():
        return names
    for q in queries_dir.iterdir():
        if q.is_file():
            names.add(q.name)
            names.add(q.stem)
    return names


def _find_results(results_dir: Path, names: set[str]) -> list[Path]:
    """Ищет в results совпадающие по имени результаты (файлы и папки), не спускаясь
    внутрь совпавших папок."""
    found: list[Path] = []
    if not names or not results_dir.exists():
        return found
    for root, dirs, files in os.walk(results_dir):
        for d in list(dirs):
            if d in names or Path(d).stem in names:
                found.append(Path(root) / d)
                dirs.remove(d)          # не рекурсируем в уже совпавшую папку
        for f in files:
            if f in names or Path(f).stem in names:
                found.append(Path(root) / f)
    return found


def _signature(p: Path) -> str:
    """Версия результата: для файла — mtime+размер, для папки — mtime."""
    st = p.stat()
    return f"{int(st.st_mtime)}:{st.st_size}" if p.is_file() else f"{int(st.st_mtime)}"


def _settled(p: Path, settle_min: float) -> bool:
    try:
        return (time.time() - p.stat().st_mtime) >= settle_min * 60
    except OSError:
        return False


def run_autosend(cfg: dict, state: dict, logger: logging.Logger) -> None:
    """Копирует готовые результаты в назначение. Файловая операция, от доступности
    A-Parser не зависит; безопасна к повторным вызовам (дедуп по state)."""
    qd, rd, dest = cfg.get("queries_dir", ""), cfg.get("results_dir", ""), cfg.get("autosend_dest", "")
    if not (qd and rd and dest):
        return
    queries_dir, results_dir, dest_dir = Path(qd), Path(rd), Path(dest)
    settle = float(cfg.get("autosend_settle_min", 2) or 0)
    sent: dict = state.setdefault("autosent", {})

    names = _query_names(queries_dir)
    if not names:
        logger.warning(f"autosend: в {queries_dir} нет файлов Queries — нечего искать.")
        return

    sent_now = []
    for r in _find_results(results_dir, names):
        if not _settled(r, settle):
            continue
        try:
            key = str(r.relative_to(results_dir))
            sig = _signature(r)
        except OSError:
            continue
        if sent.get(key) == sig:            # эта версия уже отправлена
            continue
        try:
            target = dest_dir / r.name
            if r.is_dir():
                shutil.copytree(r, target, dirs_exist_ok=True)
            else:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(r, target)
            sent[key] = sig
            sent_now.append(r.name)
            logger.info(f"autosend: {key} → {target}")
        except OSError as e:
            logger.error(f"autosend не удалось скопировать {r}: {e}")

    if sent_now:
        preview = ", ".join(sent_now[:10]) + (" …" if len(sent_now) > 10 else "")
        send_telegram(cfg, f"📤 <b>A-Parser: результаты отправлены ({len(sent_now)})</b>\n"
                           f"{preview}\n→ {dest}")
