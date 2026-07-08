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
import logging
import os
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "aparser_monitor.config.json"
STATE_PATH = HERE / "aparser_monitor.state.json"
LOG_PATH = HERE / "aparser_monitor.log"

DEFAULTS = {
    # http://IP:PORT/API — адрес API A-Parser (порт по умолчанию 9091)
    "aparser_url": "http://127.0.0.1:9091/API",
    "aparser_password": "",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_proxy": "",     # прокси для Telegram, если api.telegram.org недоступен напрямую
                              # напр. "socks5://user:pass@host:1080" или "http://host:3128"

    # Релей: слать сообщения через другой сервер локальной сети, у которого есть
    # доступ к Telegram (на нём запущен этот же скрипт в режиме --relay).
    "telegram_relay_url": "", # на серверах-клиентах: URL релея, напр. "http://192.168.1.5:8899"
    "relay_secret": "",       # общий секрет клиентов и релея (защита от посторонних)
    "relay_port": 8899,       # на сервере-релее: порт прослушивания
    "relay_bind": "0.0.0.0",  # на сервере-релее: адрес прослушивания
    "error_threshold": 0.5,   # доля ошибок, при которой шлём тревогу (0.5 = 50%)
    "cooldown_hours": 8,      # кулдаун на повторные уведомления одного типа/задания
    "min_requests": 20,       # не тревожим по проценту, пока запросов меньше этого
    "request_timeout": 30,
    "heartbeat_hours": 6,     # слать «всё ок» не чаще раза в N часов (0 — выключить)

    # Перезапуск A-Parser при недоступности (пусто/0 — выключено)
    "aparser_exe_path": "",       # raw-путь к exe A-Parser, напр. r"C:\A-Parser\aparser.exe"
    "restart_after_failures": 3,  # столько недоступностей подряд до перезапуска
    "restart_cooldown_min": 15,   # не перезапускать чаще, чем раз в N минут

    # Autosend: копирование готовых результатов на другой сервер (пусто — выключено)
    "queries_dir": "",            # raw-путь к папке Queries A-Parser (ищем рекурсивно)
    "results_dir": "",            # raw-путь к папке results A-Parser (ищем рекурсивно)
    "autosend_dest": "",          # raw UNC-путь назначения, напр. r"\\SERVER\share\in"
    "autosend_settle_min": 2,     # результат готов к отправке, если не менялся N минут
    "autosend_cleanup_min": 1440, # после N минут без изменений: отправить (если нет) и удалить

    "debug": False,               # подробные дебаг-логи (можно и флагом --debug)
}


# --------------------------------------------------------------------------- #
# Логирование и heartbeat
# --------------------------------------------------------------------------- #
def get_logger(debug: bool | None = None) -> logging.Logger:
    """Логгер: файл с ротацией (aparser_monitor.log) + вывод в консоль.
    debug=True включает уровень DEBUG (подробные логи), False — INFO. None не
    меняет уровень (для вызовов из вспомогательного кода)."""
    logger = logging.getLogger("aparser_monitor")
    if not logger.handlers:        # ещё не настроен
        logger.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    if debug is not None:
        logger.setLevel(logging.DEBUG if debug else logging.INFO)
    return logger


def want_debug(cfg: dict) -> bool:
    """Дебаг включён, если --debug в аргументах или "debug": true в конфиге."""
    return ("--debug" in sys.argv) or bool(cfg.get("debug", False))


def maybe_heartbeat(cfg: dict, state: dict, summary: str) -> None:
    """Шлёт в Telegram «всё ок» не чаще раза в cfg['heartbeat_hours'] (0 — выключено).
    Опирается на отметку времени в state — если state.json не сохраняется, heartbeat
    не отправится (безопасный отказ вместо спама), а save_state залогирует проблему.
    Вызывать только по итогу УСПЕШНОГО прогона."""
    hours = float(cfg.get("heartbeat_hours", 0) or 0)
    if hours <= 0:
        return
    now = time.time()
    last = state.get("heartbeat_ts")
    if last is not None and (now - last) < hours * 3600:
        return                                  # ещё рано
    if last is not None:                        # первый прогон только ставит отметку
        send_telegram(cfg, f"🟢 <b>A-Parser мониторинг: всё ок</b>\n{summary}")
        get_logger().info(f"heartbeat отправлен — {summary}")
    state["heartbeat_ts"] = now


# --------------------------------------------------------------------------- #
# Перезапуск A-Parser (при недоступности)
# --------------------------------------------------------------------------- #
def _restart_aparser(cfg: dict, logger: logging.Logger) -> bool:
    """Завершает процесс A-Parser по имени exe из конфига и запускает его заново."""
    exe = Path(cfg.get("aparser_exe_path", ""))
    try:
        # /T — вместе с дочерними процессами; отсутствие процесса не считаем ошибкой
        subprocess.run(["taskkill", "/F", "/T", "/IM", exe.name],
                       capture_output=True, timeout=30)
    except Exception as e:
        logger.warning(f"taskkill {exe.name}: {e}")
    time.sleep(2)
    try:
        os.startfile(str(exe))     # запуск как двойным кликом, отвязанно от монитора
        logger.info(f"A-Parser перезапущен: {exe}")
        return True
    except Exception as e:
        logger.error(f"Не удалось запустить {exe}: {e}")
        return False


def maybe_restart(cfg: dict, state: dict, logger: logging.Logger) -> None:
    """Перезапускает A-Parser после серии недоступностей, с антидребезгом по времени.
    Вызывать из обработчика недоступности (down_count уже увеличен)."""
    exe = cfg.get("aparser_exe_path", "")
    need = int(cfg.get("restart_after_failures", 0) or 0)
    if not exe or need <= 0 or state.get("down_count", 0) < need:
        return
    cd_min = int(cfg.get("restart_cooldown_min", 0) or 0)
    last = state.get("last_restart_ts")
    if last is not None and (time.time() - last) < cd_min * 60:
        logger.info("Перезапуск пропущен: недавно уже перезапускали (cooldown).")
        return
    logger.warning(f"A-Parser недоступен {state.get('down_count')} проверок — перезапуск.")
    if _restart_aparser(cfg, logger):
        state["last_restart_ts"] = time.time()
        state["down_count"] = 0
        send_telegram(cfg, f"🔁 <b>A-Parser перезапущен</b>\n"
                           f"Был недоступен {need}+ проверок подряд.")


# --------------------------------------------------------------------------- #
# Конфиг и состояние
# --------------------------------------------------------------------------- #
def read_config_file() -> dict:
    """Читает aparser_monitor.config.json, при кривом JSON — понятная ошибка."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"Ошибка в {CONFIG_PATH.name}: некорректный JSON — {e.msg} "
                 f"(строка {e.lineno}, символ {e.colno}). Проверьте запятые/кавычки; "
                 f"комментарии в JSON не допускаются.")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(read_config_file())
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
    cfg["error_threshold"] = float(cfg["error_threshold"])
    cfg["heartbeat_hours"] = float(cfg.get("heartbeat_hours", 0) or 0)
    for k in ("cooldown_hours", "min_requests", "request_timeout", "relay_port"):
        cfg[k] = int(float(cfg[k]))

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
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        # если состояние не пишется — не работают heartbeat, кулдаун и детект завершения
        get_logger().error(f"Не удалось сохранить {STATE_PATH.name}: {e}. "
                           f"Проверьте права на запись в каталог скрипта.")


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
def _telegram_proxies(cfg: dict):
    """proxies для requests, если задан telegram_proxy (иначе None — прямое соединение)."""
    p = cfg.get("telegram_proxy", "")
    return {"http": p, "https": p} if p else None


def send_telegram_direct(cfg: dict, text: str) -> None:
    """Прямая отправка в Telegram (с учётом telegram_proxy). Бросает исключение при
    ошибке — используется и на релее. Не проходит через telegram_relay_url."""
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": cfg["telegram_chat_id"], "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=cfg["request_timeout"],
        proxies=_telegram_proxies(cfg),
    )
    resp.raise_for_status()


def send_via_relay(cfg: dict, text: str) -> None:
    """Отправка через сервер-релей локальной сети (telegram_relay_url). Бросает
    исключение при ошибке."""
    url = cfg["telegram_relay_url"].rstrip("/") + "/send"
    resp = requests.post(
        url,
        json={"secret": cfg.get("relay_secret", ""), "text": text},
        timeout=cfg["request_timeout"],
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"релей вернул ошибку: {body}")


def send_telegram(cfg: dict, text: str) -> bool:
    """Отправка сообщения: напрямую или через релей (если задан telegram_relay_url).
    Ошибки НЕ пробрасываем наружу (иначе примутся за недоступность A-Parser) —
    логируем и возвращаем False."""
    try:
        if cfg.get("telegram_relay_url"):
            send_via_relay(cfg, text)
        else:
            send_telegram_direct(cfg, text)
        return True
    except (requests.exceptions.RequestException, RuntimeError, ValueError) as e:
        print(f"[warn] отправка в Telegram не удалась: {e}", file=sys.stderr)
        return False


def test_telegram(cfg: dict) -> int:
    """Дебаг-команда (--test-telegram): шлёт тестовое сообщение и печатает подробный
    результат — HTTP-код и ответ Telegram, чтобы отличить проблему токена/chat_id/сети."""
    relay = cfg.get("telegram_relay_url", "")
    if relay:
        print(f"через релей: {relay}")
        ok = send_telegram(cfg, "✅ aparser_monitor: проверка связи через релей")
        if ok:
            print("OK — сообщение принято релеем и отправлено, проверьте чат.")
            return 0
        print("ОШИБКА: релей недоступен или отклонил запрос (проверьте telegram_relay_url, "
              "relay_secret, что релей запущен и порт открыт в локальной сети).")
        return 1
    proxy = cfg.get("telegram_proxy", "")
    print(f"chat_id={cfg['telegram_chat_id']!r}, "
          f"token=…{str(cfg['telegram_bot_token'])[-6:]} (последние 6 символов), "
          f"proxy={proxy or 'нет (прямое соединение)'}")
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": cfg["telegram_chat_id"],
                  "text": "✅ aparser_monitor: проверка связи с ботом",
                  "parse_mode": "HTML"},
            timeout=cfg.get("request_timeout", 30),
            proxies=_telegram_proxies(cfg),
        )
        ok = r.status_code == 200 and r.json().get("ok", False)
        if ok:
            print("OK — тестовое сообщение отправлено, проверьте чат.")
            return 0
        print(f"ОШИБКА: HTTP {r.status_code}. Ответ Telegram: {r.text[:400]}")
        print("Подсказки: 404 — неверный токен; 400 'chat not found' — неверный "
              "chat_id или боту не писали /start; 403 — бот заблокирован в чате.")
        return 1
    except requests.exceptions.RequestException as e:
        print(f"ОШИБКА сети/таймаута: {type(e).__name__}: {e}.")
        print("api.telegram.org недоступен напрямую (частая ситуация на RU-серверах). "
              "Задайте прокси в конфиге: \"telegram_proxy\": \"socks5://host:1080\" "
              "(для socks нужен: py -m pip install pysocks) или \"http://host:3128\".")
        return 1


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


def process(cfg: dict, state: dict) -> str:
    tasks: list[dict] = []
    # активные и завершённые задания
    for completed_flag in (0, 1):
        data = api(cfg, "getTaskList", {"completed": completed_flag})
        if isinstance(data, list):
            tasks.extend(data)

    over = 0
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
                over += 1
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
    return f"заданий {len(tasks)}, с ошибками>{cfg['error_threshold']:.0%}: {over}"


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


def handle_unavailable(cfg: dict, state: dict, err: Exception, logger: logging.Logger) -> None:
    """A-Parser не ответил или ответил ошибкой: тревога (с кулдауном), отметка «лежит»
    и — только при обрыве связи — возможный перезапуск A-Parser."""
    was_down = state.get("down", False)
    key = "down:global"
    # Шлём сразу при первом сбое, дальше — не чаще кулдауна.
    if not was_down or cooldown_ok(state, key, cfg["cooldown_hours"]):
        send_telegram(cfg, describe_failure(cfg, err))
        mark_sent(state, key)
    state["down"] = True
    # Перезапускаем только когда A-Parser реально не отвечает (обрыв/таймаут),
    # а не когда ответил ошибкой HTTP/JSON (тогда процесс жив, дело в конфиге).
    if isinstance(err, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        state["down_count"] = state.get("down_count", 0) + 1
        maybe_restart(cfg, state, logger)


def handle_recovered(cfg: dict, state: dict) -> None:
    """Первый успешный опрос после простоя — сообщаем о восстановлении."""
    if state.get("down"):
        send_telegram(cfg, f"🟢 <b>A-Parser снова доступен</b>\n{cfg['aparser_url']}")
    state["down_count"] = 0
    state["down"] = False


def main() -> int:
    cfg = load_config()
    log = get_logger(want_debug(cfg))   # уровень DEBUG при --debug / "debug": true
    if "--test-telegram" in sys.argv:
        return test_telegram(cfg)
    if "--relay" in sys.argv:
        from aparser_relay import run_relay
        return run_relay(cfg, log)
    state = load_state()
    try:
        try:
            summary = process(cfg, state)
        except (requests.exceptions.RequestException, RuntimeError, ValueError) as e:
            # сеть/таймаут/HTTP-ошибка/невалидный ответ API — считаем недоступностью
            handle_unavailable(cfg, state, e, log)
            log.warning(f"NOT OK — {type(e).__name__}: {e}")
        else:
            handle_recovered(cfg, state)
            log.info(f"OK — {summary}")
            maybe_heartbeat(cfg, state, summary)
        # Autosend не зависит от доступности A-Parser — файловая операция
        try:
            from aparser_autosend import run_autosend
            run_autosend(cfg, state, log)
        except Exception as e:
            log.error(f"autosend упал: {type(e).__name__}: {e}")
    finally:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
