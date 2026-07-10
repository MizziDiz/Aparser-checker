#!/usr/bin/env python3
"""
aparser_config_schema.py — единое описание полей конфигурации aparser_monitor.

Схема (CONFIG_FIELDS) + функции чтения/записи config.json используются и десктоп-GUI
(aparser_config_gui.py), и, в будущем, веб-интерфейсом — чтобы не дублировать
список полей, типы и подсказки. Модуль намеренно лёгкий (только json/pathlib),
без зависимостей от requests/playwright.

Тип поля: "str" | "password" | "int" | "float" | "bool".
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_FILENAME = "aparser_monitor.config.json"

# group, key, label, type, default, help
CONFIG_FIELDS: list[dict] = [
    # — Мониторинг (Pro, web-интерфейс) —
    {"group": "Мониторинг (Pro, web-UI)", "key": "aparser_ui_url", "type": "str",
     "default": "http://127.0.0.1:9092/", "label": "URL web-интерфейса A-Parser",
     "help": "Адрес интерфейса, напр. http://127.0.0.1:9092/"},
    {"group": "Мониторинг (Pro, web-UI)", "key": "aparser_ui_password", "type": "password",
     "default": "", "label": "Пароль web-интерфейса", "help": "Пароль страницы входа A-Parser"},

    # — Мониторинг (Enterprise, API) —
    {"group": "Мониторинг (Enterprise, API)", "key": "aparser_url", "type": "str",
     "default": "http://127.0.0.1:9091/API", "label": "URL API A-Parser",
     "help": "Только для Enterprise-версии с API"},
    {"group": "Мониторинг (Enterprise, API)", "key": "aparser_password", "type": "password",
     "default": "", "label": "Пароль API", "help": "Можно пусто, если API без пароля"},

    # — Telegram и связь —
    {"group": "Telegram", "key": "telegram_bot_token", "type": "password", "default": "",
     "label": "Токен бота", "help": "Нужен при прямой отправке и на сервере-релее"},
    {"group": "Telegram", "key": "telegram_chat_id", "type": "str", "default": "",
     "label": "Chat ID", "help": "Куда слать уведомления"},
    {"group": "Telegram", "key": "server_name", "type": "str", "default": "",
     "label": "Имя сервера (подпись)", "help": "Подпись в сообщениях; пусто — имя хоста"},
    {"group": "Telegram", "key": "telegram_proxy", "type": "str", "default": "",
     "label": "Прокси для Telegram", "help": "socks5://host:1080 или http://host:3128; пусто — напрямую"},

    # — Релей (обход блокировки Telegram через сервер локальной сети) —
    {"group": "Релей", "key": "telegram_relay_url", "type": "str", "default": "",
     "label": "URL релея (на клиентах)", "help": "напр. http://10.10.10.2:8899; пусто — не через релей"},
    {"group": "Релей", "key": "relay_secret", "type": "password", "default": "",
     "label": "Секрет релея", "help": "Общий у релея и клиентов"},
    {"group": "Релей", "key": "relay_port", "type": "int", "default": 8899,
     "label": "Порт релея (на релее)", "help": "Порт прослушивания при --relay"},
    {"group": "Релей", "key": "relay_bind", "type": "str", "default": "0.0.0.0",
     "label": "Адрес прослушивания релея", "help": "лучше IP локальной сети, напр. 10.10.10.2"},
    {"group": "Релей", "key": "relay_allowed_ips", "type": "str", "default": "",
     "label": "Разрешённые IP (на релее)", "help": "напр. 10.10.10.0/24; пусто — все (не рекомендуется)"},

    # — Пороги и поведение —
    {"group": "Пороги и поведение", "key": "error_threshold", "type": "float", "default": 0.5,
     "label": "Порог ошибок (0.5 = 50%)", "help": "Доля ошибок для тревоги"},
    {"group": "Пороги и поведение", "key": "min_requests", "type": "int", "default": 20,
     "label": "Мин. запросов для тревоги", "help": "Ниже — процент не считаем (шум на старте)"},
    {"group": "Пороги и поведение", "key": "cooldown_hours", "type": "int", "default": 8,
     "label": "Кулдаун уведомлений, ч", "help": "Не спамить одним типом чаще"},
    {"group": "Пороги и поведение", "key": "heartbeat_hours", "type": "float", "default": 6,
     "label": "Heartbeat «всё ок», ч", "help": "0 — выключить"},
    {"group": "Пороги и поведение", "key": "request_timeout", "type": "int", "default": 30,
     "label": "Таймаут запросов, с", "help": ""},
    {"group": "Пороги и поведение", "key": "ui_nav_timeout_ms", "type": "int", "default": 30000,
     "label": "Таймаут навигации UI, мс", "help": "логин/загрузка; для удалённых узлов больше"},
    {"group": "Пороги и поведение", "key": "ui_cards_timeout_ms", "type": "int", "default": 20000,
     "label": "Ожидание карточек очереди, мс", "help": "SPA дольше рендерится по WAN"},
    {"group": "Пороги и поведение", "key": "ui_page_change_ms", "type": "int", "default": 6000,
     "label": "Ожидание смены страницы, мс", "help": "пагинация Tasks Queue"},
    {"group": "Пороги и поведение", "key": "debug", "type": "bool", "default": False,
     "label": "Подробные логи (debug)", "help": "Дебаг-логи; можно и флагом --debug"},

    # — Автоперезапуск A-Parser —
    {"group": "Автоперезапуск A-Parser", "key": "aparser_exe_path", "type": "str", "default": "",
     "label": "Путь к exe A-Parser", "help": "Пусто — перезапуск выключен"},
    {"group": "Автоперезапуск A-Parser", "key": "restart_after_failures", "type": "int", "default": 3,
     "label": "Перезапуск после N недоступностей", "help": "0 — выключить"},
    {"group": "Автоперезапуск A-Parser", "key": "restart_cooldown_min", "type": "int", "default": 15,
     "label": "Кулдаун перезапуска, мин", "help": "Не перезапускать чаще"},

    # — Autosend —
    {"group": "Autosend", "key": "queries_dir", "type": "str", "default": "",
     "label": "Папка Queries", "help": "Ищется рекурсивно; пусто — autosend выключен"},
    {"group": "Autosend", "key": "results_dir", "type": "str", "default": "",
     "label": "Папка results", "help": "Ищется рекурсивно"},
    {"group": "Autosend", "key": "autosend_dest", "type": "str", "default": "",
     "label": "Назначение (UNC-шара)", "help": r"напр. \\SERVER2\share\incoming"},
    {"group": "Autosend", "key": "autosend_settle_min", "type": "int", "default": 2,
     "label": "Готов после N мин без изменений", "help": "Тогда отправляем"},
    {"group": "Autosend", "key": "autosend_cleanup_min", "type": "int", "default": 1440,
     "label": "Удалять после N мин без изменений", "help": "Отправить (если нет) и удалить"},

    # — Статистика (--stats) —
    {"group": "Статистика", "key": "stats_settle_min", "type": "int", "default": 2,
     "label": "Считать результат после N мин без изменений", "help": "использует queries_dir/results_dir"},
    {"group": "Статистика", "key": "stats_snapshots", "type": "bool", "default": True,
     "label": "Снимки метрик заданий в БД", "help": "пишутся в проходе монитора (time-series)"},
    {"group": "Статистика", "key": "stats_retention_days", "type": "int", "default": 30,
     "label": "Хранить статистику, дней", "help": "старше — удаляется; 0 — не удалять"},
    {"group": "Статистика", "key": "almost_done_pct", "type": "int", "default": 90,
     "label": "Уведомлять «почти готово» при N%", "help": "0 — не уведомлять"},
    {"group": "Статистика", "key": "eta_window_min", "type": "int", "default": 30,
     "label": "Окно оценки скорости для ETA, мин", "help": "по снимкам за это окно"},
    {"group": "Статистика", "key": "top_period_days", "type": "int", "default": 7,
     "label": "Период топ-сводки (--top), дней", "help": "топ зон/операторов за N дней"},
    {"group": "Статистика", "key": "top_limit", "type": "int", "default": 10,
     "label": "Позиций в топе", "help": "сколько строк в каждом топе"},

    # — Кейген (--keygen) —
    {"group": "Кейген", "key": "keygen_script", "type": "str", "default": "",
     "label": "Путь к gsa_geo_pipeline.py", "help": "пусто — кейген выключен"},
    {"group": "Кейген", "key": "keygen_input_xlsx", "type": "str", "default": "",
     "label": "Путь к xlsx с ключами", "help": "вход пайплайна"},
    {"group": "Кейген", "key": "keygen_python", "type": "str", "default": "",
     "label": "python для пайплайна", "help": "пусто — текущий; нужен openpyxl"},
    {"group": "Кейген", "key": "keygen_batches", "type": "int", "default": 5,
     "label": "Батчей за запуск", "help": ""},
    {"group": "Кейген", "key": "keygen_target_mb", "type": "int", "default": 6,
     "label": "Размер батча, МБ", "help": ""},
    {"group": "Кейген", "key": "keygen_pages", "type": "int", "default": 25,
     "label": "pages (в manifest пайплайна)", "help": ""},
    {"group": "Кейген", "key": "keygen_footprints_per_seed", "type": "int", "default": 24,
     "label": "Футпринтов на seed", "help": ""},

    # — Автопилот (--autopilot) —
    {"group": "Автопилот", "key": "autopilot_create_tasks", "type": "bool", "default": False,
     "label": "Авто-создание заданий", "help": "иначе только уведомление о простое (Phase 1)"},
    {"group": "Автопилот", "key": "autopilot_template_task", "type": "str", "default": "",
     "label": "Эталонное задание (шаблон)", "help": "заголовок/подстрока задания для клонирования"},
    {"group": "Автопилот", "key": "autopilot_start_task", "type": "bool", "default": True,
     "label": "Запускать созданное задание", "help": "сразу стартовать после создания"},
    {"group": "Автопилот", "key": "autopilot_max_new_tasks", "type": "int", "default": 1,
     "label": "Заданий за проход", "help": "сколько создавать за один запуск автопилота"},
    {"group": "Автопилот", "key": "autopilot_config_preset", "type": "str", "default": "",
     "label": "Config preset", "help": "напр. 200t; пусто — не менять"},
    {"group": "Автопилот", "key": "autopilot_dry_run", "type": "bool", "default": True,
     "label": "Dry-run (обкатка)", "help": "заполнить форму и снять скрин, но не добавлять задание"},
    {"group": "Автопилот", "key": "aparser_root", "type": "str", "default": "",
     "label": "Корень A-Parser", "help": "для путей запросов; пусто — из exe/queries_dir"},
]


def coerce(field: dict, value):
    """Приводит значение из формы к типу поля."""
    t = field["type"]
    s = "" if value is None else str(value).strip()
    if t == "bool":
        return bool(value) if isinstance(value, bool) else s.lower() in ("1", "true", "да", "on")
    if t == "int":
        return int(float(s)) if s else 0
    if t == "float":
        return float(s) if s else 0.0
    return s  # str / password


def load_values(path: Path) -> dict:
    """Значения для формы: дефолты из схемы, поверх — то, что есть в config.json."""
    values = {f["key"]: f["default"] for f in CONFIG_FIELDS}
    if path.exists():
        try:
            values.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return values


def save_values(path: Path, form: dict) -> None:
    """Пишет config.json: сохраняет прежние ключи (в т.ч. не из схемы), обновляя
    значения из формы с приведением типов."""
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    for f in CONFIG_FIELDS:
        if f["key"] in form:
            existing[f["key"]] = coerce(f, form[f["key"]])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
