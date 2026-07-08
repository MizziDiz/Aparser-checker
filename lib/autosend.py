#!/usr/bin/env python3
"""
aparser_autosend.py — отправка готовых результатов A-Parser на другой сервер + уборка.

Структура A-Parser: задача в queries/<имя>/<имя>.txt, её результат в
results/<имя>/<имя>.txt (имена совпадают). Папки queries и results ищутся
РЕКУРСИВНО (во всех подпапках).

Логика по каждому файлу задачи (queries):
  1. Находим соответствующий результат в results — сначала по зеркальному пути
     (results/<тот же относительный путь>), иначе рекурсивным поиском по имени.
  2. Если результат «устоялся» (не менялся `autosend_settle_min` минут — запись
     завершена) и эта версия ещё не отправлялась — копируем в `autosend_dest`
     (обычно UNC-путь) и пишем запись в журнал отправок.
  3. Если результат давно без изменений (`autosend_cleanup_min` минут): убеждаемся,
     что он отправлен (если нет — отправляем), затем УДАЛЯЕМ и файл задачи (queries),
     и файл результата (results); пустые подпапки тоже подчищаем.

Журнал отправок (aparser_sent.jsonl) пишется на диск и читается на старте — чтобы
не потерять, что уже отправлено, даже если state.json сбросился.

Все пути задаются в конфиге в raw-формате. Пусто в любом из трёх → autosend выключен.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

from aparser_monitor import send_telegram, DATA_DIR

SENT_LOG = DATA_DIR / "aparser_sent.jsonl"


# --------------------------------------------------------------------------- #
# Журнал отправок (durable)
# --------------------------------------------------------------------------- #
def load_sent(logger: logging.Logger) -> dict[str, str]:
    """Читает журнал: {ключ_результата: подпись последней отправленной версии}."""
    sent: dict[str, str] = {}
    if not SENT_LOG.exists():
        return sent
    try:
        for line in SENT_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("action", "sent") == "sent" and "key" in rec:
                sent[rec["key"]] = rec.get("sig", "")
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"autosend: не прочитан журнал {SENT_LOG.name}: {e}")
    return sent


def append_sent_log(rec: dict) -> None:
    rec = {"ts": int(time.time()), **rec}
    with SENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Вспомогательное
# --------------------------------------------------------------------------- #
def _signature(p: Path) -> str:
    """Версия результата: для файла — mtime+размер, для папки — mtime."""
    st = p.stat()
    return f"{int(st.st_mtime)}:{st.st_size}" if p.is_file() else f"{int(st.st_mtime)}"


def _age_min(p: Path) -> float:
    try:
        return (time.time() - p.stat().st_mtime) / 60
    except OSError:
        return 0.0


def _find_result(results_dir: Path, rel: Path, name: str) -> Path | None:
    """Результат по зеркальному пути; если нет — рекурсивный поиск по имени."""
    mirror = results_dir / rel
    if mirror.exists():
        return mirror
    for root, dirs, files in os.walk(results_dir):
        if name in files or name in dirs:
            return Path(root) / name
    return None


def _rm_empty_parents(path: Path, stop: Path) -> None:
    """Удаляет опустевшие родительские папки вверх до stop (не включая её)."""
    parent = path.parent
    while parent != stop and stop in parent.parents:
        try:
            parent.rmdir()          # rmdir падает, если папка не пуста — это ок
        except OSError:
            break
        parent = parent.parent


def _delete(path: Path, stop: Path, logger: logging.Logger) -> bool:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        _rm_empty_parents(path, stop)
        return True
    except OSError as e:
        logger.error(f"autosend: не удалось удалить {path}: {e}")
        return False


# --------------------------------------------------------------------------- #
# Основная логика
# --------------------------------------------------------------------------- #
def run_autosend(cfg: dict, state: dict, logger: logging.Logger) -> None:
    qd, rd, dest = cfg.get("queries_dir", ""), cfg.get("results_dir", ""), cfg.get("autosend_dest", "")
    if not (qd and rd and dest):
        return
    queries_dir, results_dir, dest_dir = Path(qd), Path(rd), Path(dest)
    if not queries_dir.exists():
        logger.warning(f"autosend: папка queries не найдена: {queries_dir}")
        return
    settle = float(cfg.get("autosend_settle_min", 2) or 0)
    cleanup = float(cfg.get("autosend_cleanup_min", 0) or 0)
    sent = load_sent(logger)
    sent_now, deleted = [], 0

    # рекурсивно по всем файлам задач
    for root, _dirs, files in os.walk(queries_dir):
        for qname in files:
            qfile = Path(root) / qname
            rel = qfile.relative_to(queries_dir)
            result = _find_result(results_dir, rel, qname)
            if result is None:
                logger.debug(f"autosend: для {rel} результат ещё не найден")
                continue
            age = _age_min(result)
            if age < settle:
                logger.debug(f"autosend: {result.name} ещё пишется (age={age:.1f}м)")
                continue
            rkey = str(result.relative_to(results_dir))
            sig = _signature(result)
            already = sent.get(rkey) == sig

            # 1) отправка, если ещё не отправляли эту версию
            if not already:
                try:
                    target = dest_dir / result.name
                    if result.is_dir():
                        shutil.copytree(result, target, dirs_exist_ok=True)
                    else:
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(result, target)
                    append_sent_log({"key": rkey, "sig": sig, "dest": str(target), "action": "sent"})
                    sent[rkey] = sig
                    already = True
                    sent_now.append(result.name)
                    logger.info(f"autosend: отправлен {rkey} → {target}")
                except OSError as e:
                    logger.error(f"autosend: не скопирован {result}: {e}")
                    continue

            # 2) уборка: давно без изменений и уже отправлен → удаляем задачу и результат
            if cleanup > 0 and age >= cleanup and already:
                ok_r = _delete(result, results_dir, logger)
                ok_q = _delete(qfile, queries_dir, logger)
                if ok_r or ok_q:
                    deleted += 1
                    append_sent_log({"key": rkey, "sig": sig, "action": "deleted"})
                    logger.info(f"autosend: удалены задача и результат {rkey} (age={age:.0f}м)")

    if sent_now:
        preview = ", ".join(sent_now[:10]) + (" …" if len(sent_now) > 10 else "")
        send_telegram(cfg, f"📤 <b>A-Parser: результаты отправлены ({len(sent_now)})</b>\n"
                           f"{preview}\n→ {dest}")
    if deleted:
        logger.info(f"autosend: удалено пар задача/результат: {deleted}")
