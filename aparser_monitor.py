#!/usr/bin/env python3
"""
aparser_monitor.py — уведомления в Telegram о состоянии заданий A-Parser.

Что делает за один запуск:
  1. Опрашивает JSON API A-Parser (getTaskList + getTaskState).
  2. Шлёт в Telegram сообщение, когда задание завершилось.
  3. Шлёт сообщение-тревогу, когда доля ошибок в задании превысила порог
     (по умолчанию 50%).
  4. Держит кулдаун (по умолчанию 8 часов) на повторные уведомления, чтобы
     не спамить, и запоминает уже отправленные — состояние в JSON-файле.

Скрипт РАЗОВЫЙ (stateless между запусками, всё в state-файле): запускайте его
по расписанию — Планировщик задач Windows или встроенный планировщик A-Parser,
например раз в 2–5 минут.

Настройка — через переменные окружения или файл aparser_monitor.config.json
рядом со скриптом (см. aparser_monitor.config.example.json).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "aparser_monitor.config.json"
STATE_PATH = HERE / "aparser_monitor.state.json"

DEFAULTS = {
    # http://IP:PORT/API — адрес API A-Parser (порт по умолчанию 9091)
    "aparser_url": "http://127.0.0.1:9091/API",
    "aparser_password": "",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "error_threshold": 0.5,   # доля ошибок, при которой шлём тревогу (0.5 = 50%)
    "cooldown_hours": 8,      # кулдаун на повторные уведомления одного типа/задания
    "min_requests": 20,       # не тревожим по проценту, пока запросов меньше этого
    "request_timeout": 30,
}


# --------------------------------------------------------------------------- #
# Конфиг и состояние
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    # переменные окружения имеют приоритет над файлом
    env_map = {
        "APARSER_URL": "aparser_url",
        "APARSER_PASSWORD": "aparser_password",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
        "APARSER_ERROR_THRESHOLD": "error_threshold",
        "APARSER_COOLDOWN_HOURS": "cooldown_hours",
    }
    for env_key, cfg_key in env_map.items():
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]
    # приведение типов для числовых параметров
    for k in ("error_threshold", "cooldown_hours", "min_requests", "request_timeout"):
        cfg[k] = float(cfg[k]) if k == "error_threshold" else int(float(cfg[k]))

    # aparser_password не обязателен: если API A-Parser настроен без пароля,
    # в запрос уходит пустая строка — это валидно.
    missing = [k for k in ("telegram_bot_token", "telegram_chat_id") if not cfg[k]]
    if missing:
        sys.exit(f"Не заданы обязательные параметры: {', '.join(missing)}. "
                 f"Заполните {CONFIG_PATH.name} или переменные окружения.")
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"alerts": {}}  # key -> unix ts последнего уведомления


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# A-Parser API
# --------------------------------------------------------------------------- #
def api(cfg: dict, action: str, data: dict | None = None) -> dict:
    payload = {"password": cfg["aparser_password"], "action": action}
    if data is not None:
        payload["data"] = data
    resp = requests.post(cfg["aparser_url"], json=payload, timeout=cfg["request_timeout"])
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"API '{action}' вернул ошибку: {body}")
    return body.get("data")


def _find_first(d, keys):
    """Рекурсивно ищет в dict первое значение по одному из имён keys."""
    if isinstance(d, dict):
        for k in keys:
            if k in d and isinstance(d[k], (int, float)):
                return d[k]
        for v in d.values():
            found = _find_first(v, keys)
            if found is not None:
                return found
    return None


def extract_counters(state: dict) -> tuple[int, int]:
    """
    Возвращает (успешно, ошибок) из ответа getTaskState.

    Имена счётчиков зависят от версии A-Parser, поэтому ищем по нескольким
    вероятным ключам. Если у вас поля называются иначе — проверьте реальный
    ответ getTaskState и поправьте списки ниже.
    """
    good = _find_first(state, ["success", "successCount", "good", "goodCount"]) or 0
    bad = _find_first(state, ["fail", "failed", "bad", "badCount", "errors", "errorCount"]) or 0
    return int(good), int(bad)


def is_completed(state: dict) -> bool:
    status = str(_find_first(state, ["status"]) or "").lower()
    if status:
        return status in ("completed", "complete", "done", "finished")
    # запасной вариант, если статус текстом не пришёл
    active = state.get("active") if isinstance(state, dict) else None
    return active is False


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def send_telegram(cfg: dict, text: str) -> bool:
    """Отправка в Telegram. Ошибки Telegram НЕ пробрасываем наружу, иначе они
    были бы приняты за недоступность A-Parser; просто логируем и возвращаем False."""
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": cfg["telegram_chat_id"], "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=cfg["request_timeout"],
        )
        resp.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(f"[warn] Telegram sendMessage не удался: {e}", file=sys.stderr)
        return False


# --------------------------------------------------------------------------- #
# Кулдаун
# --------------------------------------------------------------------------- #
def cooldown_ok(state: dict, key: str, cooldown_hours: int) -> bool:
    last = state["alerts"].get(key)
    if last is None:
        return True
    return (time.time() - last) >= cooldown_hours * 3600


def mark_sent(state: dict, key: str) -> None:
    state["alerts"][key] = time.time()


def prune_state(state: dict, cooldown_hours: int) -> None:
    """Убираем записи старше двух кулдаунов, чтобы файл не рос бесконечно."""
    cutoff = time.time() - cooldown_hours * 3600 * 2
    state["alerts"] = {k: v for k, v in state["alerts"].items() if v >= cutoff}


# --------------------------------------------------------------------------- #
# Основная логика
# --------------------------------------------------------------------------- #
def task_uid(task: dict) -> str:
    return str(_find_first(task, ["taskUid", "uid", "id"]) or task.get("uniquename", "?"))


def task_name(task: dict, state: dict) -> str:
    return str(task.get("preset") or _find_first(state, ["preset"])
               or task.get("uniquename") or task_uid(task))


def process(cfg: dict, state: dict) -> None:
    tasks: list[dict] = []
    # активные и завершённые задания
    for completed_flag in (0, 1):
        data = api(cfg, "getTaskList", {"completed": completed_flag})
        if isinstance(data, list):
            tasks.extend(data)

    for task in tasks:
        uid = task_uid(task)
        try:
            st = api(cfg, "getTaskState", {"taskUid": int(uid)}) if uid.isdigit() \
                else api(cfg, "getTaskState", {"taskUid": uid})
        except Exception as e:  # одно упавшее задание не должно ронять весь проход
            print(f"[warn] getTaskState({uid}) не удался: {e}", file=sys.stderr)
            continue
        if not isinstance(st, dict):
            continue

        good, bad = extract_counters(st)
        total = good + bad
        name = task_name(task, st)

        # 1) авария: слишком много ошибок
        if total >= cfg["min_requests"]:
            rate = bad / total
            if rate >= cfg["error_threshold"]:
                key = f"errors:{uid}"
                if cooldown_ok(state, key, cfg["cooldown_hours"]):
                    send_telegram(
                        cfg,
                        f"⚠️ <b>A-Parser: много ошибок</b>\n"
                        f"Задание: <b>{name}</b> (uid {uid})\n"
                        f"Ошибок: <b>{rate:.0%}</b> ({bad} из {total})\n"
                        f"Кулдаун {cfg['cooldown_hours']} ч.",
                    )
                    mark_sent(state, key)
                    print(f"[alert] errors {uid} rate={rate:.0%}")

        # 2) завершение задания (один раз на задание, с кулдауном)
        if is_completed(st):
            key = f"done:{uid}"
            if cooldown_ok(state, key, cfg["cooldown_hours"]):
                extra = f"\nОшибок: {bad} из {total} ({(bad/total):.0%})" if total else ""
                send_telegram(
                    cfg,
                    f"✅ <b>A-Parser: задание завершено</b>\n"
                    f"Задание: <b>{name}</b> (uid {uid}){extra}",
                )
                mark_sent(state, key)
                print(f"[done] {uid}")

    prune_state(state, cfg["cooldown_hours"])


def describe_failure(cfg: dict, err: Exception) -> str:
    """Человекочитаемое сообщение с разбором причины сбоя."""
    if isinstance(err, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        # реальная недоступность: сервер не принял соединение / не ответил вовремя
        return (f"🔴 <b>A-Parser недоступен</b>\n{cfg['aparser_url']}\n"
                f"Нет соединения: {type(err).__name__}")
    if isinstance(err, requests.exceptions.HTTPError):
        code = err.response.status_code if err.response is not None else "?"
        hint = ("проверьте порт и путь /API, включён ли API в настройках A-Parser"
                if code in (404, 405) else
                "проверьте пароль API (`aparser_password`)" if code in (401, 403) else
                "смотрите ответ сервера")
        return (f"🟠 <b>A-Parser API: ошибка HTTP {code}</b>\n{cfg['aparser_url']}\n"
                f"Это не обрыв связи — сервер ответил. {hint}.")
    if isinstance(err, ValueError):  # тело ответа — не JSON
        return (f"🟠 <b>A-Parser API: неожиданный ответ</b>\n{cfg['aparser_url']}\n"
                f"Ответ не является JSON — верный ли это адрес API?")
    # RuntimeError: API вернул success=0 и текст ошибки
    return f"🟠 <b>A-Parser API вернул ошибку</b>\n{cfg['aparser_url']}\n{err}"


def handle_unavailable(cfg: dict, state: dict, err: Exception) -> None:
    """A-Parser не ответил или ответил ошибкой: тревога (с кулдауном) и отметка «лежит»."""
    was_down = state.get("down", False)
    key = "down:global"
    # Шлём сразу при первом сбое, дальше — не чаще кулдауна.
    if not was_down or cooldown_ok(state, key, cfg["cooldown_hours"]):
        send_telegram(cfg, describe_failure(cfg, err))
        mark_sent(state, key)
    state["down"] = True
    print(f"[down] {type(err).__name__}: {err}", file=sys.stderr)


def handle_recovered(cfg: dict, state: dict) -> None:
    """Первый успешный опрос после простоя — сообщаем о восстановлении."""
    if state.get("down"):
        send_telegram(cfg, f"🟢 <b>A-Parser снова доступен</b>\n{cfg['aparser_url']}")
        print("[up] recovered")
    state["down"] = False


def main() -> int:
    cfg = load_config()
    state = load_state()
    try:
        try:
            process(cfg, state)
        except (requests.exceptions.RequestException, RuntimeError, ValueError) as e:
            # сеть/таймаут/HTTP-ошибка/невалидный ответ API — считаем недоступностью
            handle_unavailable(cfg, state, e)
        else:
            handle_recovered(cfg, state)
    finally:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
