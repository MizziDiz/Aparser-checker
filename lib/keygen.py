#!/usr/bin/env python3
"""
lib/keygen.py — оркестрация кейгена (gsa_geo_pipeline.py) → батчи в queries A-Parser.

По команде `--keygen`:
  1. Запускает gsa_geo_pipeline.py (из соседнего проекта) на заданном xlsx —
     он генерит батчи запросов «Seed Operator Footprint» в staging-папку.
  2. Раскладывает каждый батч в queries/<имя>/<имя>.txt (каждый батч = задание;
     совпадает с раскладкой results/<имя>/<имя>.txt для stats/autosend).
  3. Шлёт уведомление в Telegram, сколько батчей подготовлено.

Пути и параметры — в конфиге (keygen_*). queries_dir — общий с autosend/stats.
Пайплайну нужен openpyxl в том же Python (py -m pip install openpyxl).
Создание самих заданий в A-Parser (Pro) — вручную/через UI; здесь только файлы.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from aparser_monitor import DATA_DIR, send_telegram


def run_keygen(cfg: dict, logger: logging.Logger) -> None:
    script = cfg.get("keygen_script", "")
    xlsx = cfg.get("keygen_input_xlsx", "")
    qdir = cfg.get("queries_dir", "")
    if not (script and xlsx and qdir):
        logger.warning("keygen: не заданы keygen_script / keygen_input_xlsx / queries_dir")
        return
    script_p, xlsx_p, qroot = Path(script), Path(xlsx), Path(qdir)
    if not script_p.exists() or not xlsx_p.exists():
        logger.error(f"keygen: нет файла: {script_p if not script_p.exists() else xlsx_p}")
        return

    python = cfg.get("keygen_python", "") or sys.executable
    stage = DATA_DIR / "keygen_stage"
    stage.mkdir(parents=True, exist_ok=True)
    args = [
        python, str(script_p),
        "--input-xlsx", str(xlsx_p),
        "--out-dir", str(stage),
        "--batches", str(int(cfg.get("keygen_batches", 5) or 5)),
        "--target-mb", str(cfg.get("keygen_target_mb", 6) or 6),
        "--pages", str(int(cfg.get("keygen_pages", 25) or 25)),
        "--footprints-per-seed", str(int(cfg.get("keygen_footprints_per_seed", 24) or 24)),
        "--random-seed", str(int(time.time())),   # разный seed → свежие батчи каждый запуск
    ]
    logger.info(f"keygen: запуск пайплайна ({args[1]})")
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    except Exception as e:  # noqa: BLE001
        logger.error(f"keygen: не удалось запустить пайплайн: {e}")
        return
    if r.returncode != 0:
        logger.error(f"keygen: пайплайн завершился с ошибкой (код {r.returncode}): "
                     f"{(r.stderr or r.stdout or '')[:600]}")
        return

    # раскладываем батчи B*.txt в queries/<имя>/<имя>.txt (каждый = задание)
    moved = []
    for f in sorted(stage.glob("B*.txt")):
        dest = qroot / f.stem / f.name
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dest))
            moved.append(f.stem)
        except OSError as e:
            logger.error(f"keygen: не удалось положить {f.name} в queries: {e}")
    logger.info(f"keygen: подготовлено батчей {len(moved)} → {qroot}")
    if moved:
        preview = ", ".join(moved[:8]) + (" …" if len(moved) > 8 else "")
        send_telegram(cfg, f"🧩 <b>Кейген: подготовлено батчей {len(moved)}</b>\n"
                           f"{preview}\n→ {qroot}\n(создайте задания в A-Parser под эти файлы)")
