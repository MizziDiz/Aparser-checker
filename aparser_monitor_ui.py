#!/usr/bin/env python3
"""
aparser_monitor_ui.py — Pro-совместимый монитор A-Parser через web-интерфейс.

В A-Parser Pro нет HTTP API (он только в Enterprise), поэтому состояние читается
из web-интерфейса с помощью Playwright (браузер без окна). Что делает за проход:
  1. Логинится (POST /auth), заходит в Tasks Queue и обходит страницы, разбирая
     карточки заданий. Из каждой карточки берёт готовые поля:
     «Failed queries: 147801 99.8%», «Queries done/all: 148034/588410», «Status: work».
  2. ✅ Парсинг завершён — когда число незавершённых заданий падает с >0 до 0.
  3. ⚠️ Много ошибок — если доля ошибок в задании >= порога (при >= min_requests
     обработанных запросах, чтобы не ловить шум на старте).
  4. 🔴 Интерфейс недоступен / 🟢 снова доступен.
Кулдаун/дедуп/Telegram — общие с aparser_monitor.py.

Поля карточки читаются структурно: значение берётся из элемента
.x-form-display-field, а подпись — из парного элемента по id (…-inputEl ↔ …-labelEl),
узлы обходятся в порядке документа. Это устойчивее регэкспа по склеенному тексту.

Проверено по дампам A-Parser Pro v1.2.3293 (ExtJS).

Требуется:
    pip install playwright
    playwright install chromium

Настройка — тот же aparser_monitor.config.json, плюс ключи для UI:
    "aparser_ui_url":      "http://127.0.0.1:9092/"   (адрес web-интерфейса)
    "aparser_ui_password": "..."                        (пароль интерфейса)

Режимы:
    python aparser_monitor_ui.py                # рабочий проход (headless) по расписанию
    python aparser_monitor_ui.py --check        # диагностика: что видит скрипт, без отправки
    python aparser_monitor_ui.py --dump         # разовый снимок (HTML+PNG) после логина
    python aparser_monitor_ui.py --interactive  # видимый браузер: дампы разделов в ui_dumps/

Если у вас другой билд A-Parser и --check парсит неверно — сверьте по --dump и
поправьте константы LOGIN_*/NEXT_PAGE/CARDS_JS.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path

# переиспользуем конфиг/состояние/кулдаун/Telegram/логи из основного скрипта
from aparser_monitor import (
    CONFIG_PATH, DATA_DIR, DEFAULTS, load_state, save_state, read_config_file,
    send_telegram, cooldown_ok, mark_sent, prune_state,
    get_logger, want_debug, maybe_heartbeat, maybe_restart, test_telegram,
)
from lib.autosend import run_autosend

HERE = Path(__file__).resolve().parent

# ── Реальные точки интерфейса A-Parser Pro (v1.2.x) ───────────────────────────
LOGIN_PASSWORD = 'input[name="password"]'    # поле пароля на странице /auth
LOGIN_SUBMIT = 'input[type="submit"]'         # кнопка «Log in»
NEXT_PAGE = ".x-tbar-page-next"               # «следующая страница» в Tasks Queue
# из значений полей парсим числа
FAILED_PCT_RE = re.compile(r"([\d.]+)\s*%")   # «147801 99.8%» → 99.8
FIRST_INT_RE = re.compile(r"(\d+)")           # «148034/588410 25.2%» → 148034
# подписи полей в карточке — интерфейс A-Parser бывает на EN и на RU, поддерживаем оба
LABELS_STATUS = ("Status:", "Статус:")
LABELS_FAILED = ("Failed queries:", "Неудачных запросов:")
LABELS_DONE = ("Queries done/all:", "Запросы заверш./всего:")
LABELS_SPEED = ("Speed cur/avg:", "Скорость текущая/общая:")
LABELS_RESULTS = ("Results unique/all:", "Результатов уник/всего:")
TWO_INTS_RE = re.compile(r"(\d+)\s*/\s*(\d+)")   # «148034/588410 25.2%» → (148034, 588410)
# статусы «задание завершено» (всё остальное считаем активным — консервативно,
# чтобы случайно не объявить завершение раньше времени)
DONE_STATUSES = {"completed", "complete", "done", "finished"}
# JS: собрать карточки Tasks Queue как {title, fields{подпись: значение}} в порядке
# документа. Значение — текст .x-form-display-field, подпись — парный …-labelEl.
CARDS_JS = r"""
() => {
  const nodes = document.querySelectorAll('.x-title-text, .x-form-display-field');
  const cards = []; let cur = null;
  for (const n of nodes) {
    const t = (n.innerText || '').trim();
    if (n.classList.contains('x-title-text')) {
      if (/^#\d+\s*-/.test(t)) { cur = {title: t, fields: {}}; cards.push(cur); }
    } else if (cur && (n.id || '').endsWith('-inputEl')) {
      const lab = document.getElementById(n.id.replace(/-inputEl$/, '-labelEl'));
      const label = lab ? (lab.innerText || '').trim() : '';
      if (label) cur.fields[label] = t;
    }
  }
  return cards;
}
"""
# Живые поля State в карточке сначала рендерятся плейсхолдером «Display Field» и
# заполняются реальными данными на следующем тике обновления A-Parser. Ждём, пока
# плейсхолдеры не исчезнут, иначе прочитаем пустые значения.
PLACEHOLDER = "Display Field"
CARDS_READY_JS = r"""
() => {
  const vals = [...document.querySelectorAll('.x-form-display-field')]
    .map(e => (e.innerText || '').trim());
  return vals.length === 0 || !vals.some(v => v === 'Display Field');
}
"""


def load_ui_config() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update({"aparser_ui_url": "http://127.0.0.1:9091/", "aparser_ui_password": ""})
    cfg.update(read_config_file())
    cfg["cooldown_hours"] = int(float(cfg["cooldown_hours"]))
    cfg["error_threshold"] = float(cfg["error_threshold"])
    cfg["min_requests"] = int(float(cfg["min_requests"]))
    cfg["heartbeat_hours"] = float(cfg.get("heartbeat_hours", 0) or 0)
    cfg["relay_port"] = int(float(cfg.get("relay_port", 8899) or 8899))
    for k, dflt in (("ui_nav_timeout_ms", 30000), ("ui_cards_timeout_ms", 20000),
                    ("ui_page_change_ms", 6000)):
        cfg[k] = int(float(cfg.get(k, dflt) or dflt))
    for k in ("telegram_bot_token", "telegram_chat_id"):
        if not cfg.get(k):
            sys.exit(f"Не задан обязательный параметр: {k}. Заполните {CONFIG_PATH.name}.")
    return cfg


def open_ui(pw, cfg, headless: bool = True):
    """Открывает интерфейс, логинится при необходимости, ждёт загрузки приложения."""
    nav = int(cfg.get("ui_nav_timeout_ms", 30000) or 30000)
    browser = pw.chromium.launch(headless=headless)
    page = browser.new_page()
    page.goto(cfg["aparser_ui_url"], wait_until="domcontentloaded", timeout=nav)
    if page.query_selector(LOGIN_PASSWORD):
        page.fill(LOGIN_PASSWORD, cfg["aparser_ui_password"])
        page.click(LOGIN_SUBMIT)
        page.wait_for_load_state("domcontentloaded", timeout=nav)
    # ждём, пока ExtJS отрисует нижнюю статус-строку (признак загрузки приложения)
    page.wait_for_function(
        r"() => document.body && /Tasks:\s*\d+\s*\/\s*\d+/.test(document.body.innerText)",
        timeout=nav,
    )
    return browser, page


# --------------------------------------------------------------------------- #
# Чтение карточек Tasks Queue
# --------------------------------------------------------------------------- #
def _field(fields: dict, labels: tuple[str, ...]) -> str:
    """Значение поля по одной из подписей (EN/RU); плейсхолдер считаем пустым."""
    for lab in labels:
        if lab in fields:
            v = fields[lab]
            return "" if v == PLACEHOLDER else v
    return ""


def _two_ints(text: str) -> tuple[int, int]:
    m = TWO_INTS_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _card_from_raw(raw: dict) -> dict:
    f = raw.get("fields", {})
    fm = FAILED_PCT_RE.search(_field(f, LABELS_FAILED))
    done, total = _two_ints(_field(f, LABELS_DONE))
    speed_cur, speed_avg = _two_ints(_field(f, LABELS_SPEED))
    res_uniq, res_all = _two_ints(_field(f, LABELS_RESULTS))
    return {
        "title": raw.get("title", "?"),
        "failed_pct": float(fm.group(1)) if fm else None,
        "done": done,
        "total": total,
        "status": _field(f, LABELS_STATUS).strip(),
        "speed_cur": speed_cur,
        "speed_avg": speed_avg,
        "results_uniq": res_uniq,
        "results_all": res_all,
    }


def extract_cards(page) -> list[dict]:
    return [_card_from_raw(c) for c in page.evaluate(CARDS_JS)]


def debug_log_cards(page) -> None:
    """При DEBUG логирует сырые поля карточек, у которых не прочитался статус —
    помогает понять, почему на некоторых серверах статусы пустые."""
    log = get_logger()
    if not log.isEnabledFor(logging.DEBUG):
        return
    raw = page.evaluate(CARDS_JS)
    log.debug(f"страница Tasks Queue: карточек {len(raw)}")
    for rc in raw:
        fields = rc.get("fields", {})
        st = _field(fields, LABELS_STATUS).strip()
        if not st:
            log.debug(f"  пустой статус у {rc.get('title')!r}: fields={fields}")


def wait_cards_ready(page, timeout: int = 12000) -> None:
    """Ждёт, пока живые поля карточек заполнятся (исчезнет плейсхолдер «Display Field»).
    По таймауту не падает — читаем что успело прогрузиться."""
    try:
        page.wait_for_function(CARDS_READY_JS, timeout=timeout)
    except Exception:
        pass


def active_count(cards: list[dict]) -> int:
    """Сколько заданий ещё не завершено (work/waitSlot/…)."""
    return sum(1 for c in cards if (c["status"] or "").lower() not in DONE_STATUSES)


def collect_cards(page, cfg: dict | None = None, max_pages: int = 25) -> list[dict]:
    """Заходит в Tasks Queue, обходит все страницы, собирает уникальные карточки.
    Стоп — когда следующая страница не даёт новых заданий (последняя).
    Таймауты берутся из cfg (для удалённых узлов — увеличенные)."""
    cfg = cfg or {}
    cards_to = int(cfg.get("ui_cards_timeout_ms", 20000) or 20000)
    change_ms = int(cfg.get("ui_page_change_ms", 6000) or 6000)
    click_to = int(cfg.get("ui_nav_timeout_ms", 30000) or 30000)
    try:
        page.get_by_text("Tasks Queue", exact=True).first.click(timeout=min(click_to, 10000))
    except Exception:
        pass
    cards, seen = [], set()
    attempts = max(1, change_ms // 200)
    for _ in range(max_pages):
        wait_cards_ready(page, cards_to)       # дождаться заполнения живых полей
        debug_log_cards(page)                  # при --debug: сырые поля проблемных карточек
        cur = extract_cards(page)
        titles = [c["title"] for c in cur]
        for c in cur:
            if c["title"] not in seen:
                seen.add(c["title"])
                cards.append(c)
        nxt = page.query_selector(NEXT_PAGE)
        if not nxt:
            break
        try:
            nxt.click()
        except Exception:
            break
        # ждём, пока список заданий реально сменится; если не сменился —
        # это была последняя страница (кнопка next неактивна)
        changed = False
        for _ in range(attempts):
            page.wait_for_timeout(200)
            if [c["title"] for c in extract_cards(page)] != titles:
                changed = True
                break
        if not changed:
            break
    return cards


# --------------------------------------------------------------------------- #
# Уведомления
# --------------------------------------------------------------------------- #
def notify_completion(cfg, state, active: int, total: int) -> None:
    """Событие «парсинг завершён» = незавершённых заданий было >0 и УСТОЙЧИВО стало 0.
    Защита от ложного нуля: единичное пустое чтение (гонка рендера ExtJS / сбой по WAN)
    завершением НЕ считается — требуется подтверждение `completion_confirm_reads`
    проходами подряд (по умолчанию 2). Иначе флейковый 0 при живых заданиях слал ложное
    «завершено» (частый случай при удалённом чтении)."""
    confirm = max(1, int(cfg.get("completion_confirm_reads", 2) or 2))
    prev = state.get("ui_active_prev")
    seen = state.get("ui_seen_active", False)
    streak = state.get("ui_zero_streak", 0)
    if active > 0:
        seen = True
        streak = 0
    else:
        streak += 1
    # завершение только когда: раньше видели активные, теперь 0 подтверждён N раз подряд
    if seen and active == 0 and streak >= confirm:
        key = "done:ui"
        if cooldown_ok(state, key, cfg["cooldown_hours"]):
            send_telegram(
                cfg,
                f"✅ <b>A-Parser: парсинг завершён</b>\n"
                f"Незавершённых заданий не осталось (подтверждено {streak} проверками).",
            )
            mark_sent(state, key)
            print("[done] parsing finished")
        seen = False                            # не повторять, пока не появятся новые задания
    state["ui_seen_active"] = seen
    state["ui_zero_streak"] = streak
    state["ui_active_prev"] = active
    prune_state(state, cfg["cooldown_hours"])
    print(f"[ok] active={active}/{total} prev={prev} zero_streak={streak}")


def notify_errors(cfg, state, cards: list[dict]) -> int:
    """Тревога по карточкам, где доля ошибок >= порога (и обработано >= min_requests).
    Возвращает число заданий, превысивших порог (для сводки в лог)."""
    threshold_pct = cfg["error_threshold"] * 100
    over = 0
    for c in cards:
        if c["failed_pct"] is None or c["done"] < cfg["min_requests"]:
            continue
        if c["failed_pct"] >= threshold_pct:
            over += 1
            key = f"errors:{c['title']}"
            if cooldown_ok(state, key, cfg["cooldown_hours"]):
                send_telegram(
                    cfg,
                    f"⚠️ <b>A-Parser: много ошибок</b>\n"
                    f"Задание: <b>{c['title']}</b> (status: {c['status'] or '?'})\n"
                    f"Ошибок: <b>{c['failed_pct']:.1f}%</b> при {c['done']} обработанных запросах",
                )
                mark_sent(state, key)
    return over


def _fmt_eta(sec) -> str:
    if not sec or sec <= 0:
        return "—"
    sec = int(sec)
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h}ч {m:02d}м" if h else f"{m}м"


def notify_progress(cfg, state, cards) -> str:
    """Уведомление «почти готово» (порог almost_done_pct) с ETA и краткая сводка
    остатка для heartbeat/лога. ETA считается по снимкам task_snapshots."""
    thr = float(cfg.get("almost_done_pct", 90) or 0) / 100
    try:
        from lib.stats import progress_info
        info = progress_info(cfg, cards)
    except Exception as e:  # noqa: BLE001
        get_logger().error(f"stats: ETA не посчитан: {type(e).__name__}: {e}")
        return ""
    parts = []
    for it in info:
        if (it["status"] or "").lower() in DONE_STATUSES:
            continue
        eta = _fmt_eta(it["eta_sec"])
        if thr > 0 and it["pct"] >= thr:
            key = f"progress:{it['title']}"
            if cooldown_ok(state, key, cfg["cooldown_hours"]):
                send_telegram(cfg, f"⏳ <b>{it['title']} почти готово</b>\n"
                                   f"{it['pct']:.0%}, осталось {it['remaining']}, ETA {eta}")
                mark_sent(state, key)
        parts.append(f"{it['title']}: ост.{it['remaining']}, ETA {eta}")
    return "; ".join(parts[:3])


def handle_ui_down(cfg, state, err, logger) -> None:
    was_down = state.get("down", False)
    key = "down:global"
    if not was_down or cooldown_ok(state, key, cfg["cooldown_hours"]):
        send_telegram(cfg, f"🔴 <b>A-Parser: интерфейс недоступен</b>\n{cfg['aparser_ui_url']}\n"
                           f"{type(err).__name__}: {err}")
        mark_sent(state, key)
    state["down"] = True
    state["down_count"] = state.get("down_count", 0) + 1
    maybe_restart(cfg, state, logger)   # перезапуск A-Parser после серии недоступностей


def run(cfg, state) -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, page = open_ui(pw, cfg)
        try:
            cards = collect_cards(page, cfg)
        finally:
            browser.close()
    if state.get("down"):
        send_telegram(cfg, f"🟢 <b>A-Parser: интерфейс снова доступен</b>\n{cfg['aparser_ui_url']}")
    state["down"] = False
    state["down_count"] = 0
    active = active_count(cards)
    notify_completion(cfg, state, active, len(cards))
    over = notify_errors(cfg, state, cards)
    summary = f"заданий {len(cards)}, активных {active}, с ошибками>{cfg['error_threshold']:.0%}: {over}"
    # снимки метрик заданий + остаток/ETA (не ломают прогон при сбое)
    if cfg.get("stats_snapshots", True):
        try:
            from lib.stats import record_snapshots
            record_snapshots(cfg, cards, get_logger())
            progress = notify_progress(cfg, state, cards)
            if progress:
                summary += f" | {progress}"
        except Exception as e:  # noqa: BLE001
            get_logger().error(f"stats: снимок/ETA не записан: {type(e).__name__}: {e}")
    return summary


# --------------------------------------------------------------------------- #
# Автопилот Phase 2: авто-создание заданий под наборы батчей (через Task preset)
# Набор = подпапка queries/<группа>/ с файлами батчей; одно задание = один набор
# (все .txt набора идут в источник запросов одного задания).
# --------------------------------------------------------------------------- #
class TaskCreateNotReady(RuntimeError):
    """Не удалось найти элемент формы создания задания (другой язык UI / изменилась
    вёрстка). Автопилот трактует это как «создать не смог» и откатывается на Phase 1."""


class TaskCreateDryRun(RuntimeError):
    """Dry-run: форма заполнена и снят скрин, но задание намеренно НЕ добавлено."""


# Реализация create_task_from_set готова (по дампам Task Editor v1.2.3293). Первый
# боевой запуск всё равно держите в autopilot_dry_run=true и сверьте скрин-превью:
# статическая вёрстка не проверяет живое поведение ExtJS (комбо/загрузка пресета).
CREATE_SELECTORS_READY = True

# Подписи полей/навигации/кнопок редактора — интерфейс A-Parser бывает на EN и на RU
# (на части серверов русский). Держим оба варианта; резолвер сопоставляет любой из них.
LBL_CONFIG_PRESET = ("Config preset", "Конфиг потоков")
LBL_TASK_PRESET = ("Task preset", "Задание")
LBL_SELECT_FILE = ("Select File", "Выберите файл")
NAV_TASK_EDITOR = ("Task Editor", "Редактор заданий")
BTN_ADD_TASK = ("Add Task", "Добавить задание")
BTN_RUN = ("Run", "Запустить", "Создать")

# JS: по одной из подписей поля (EN/RU) вернуть id его input (пара …-labelEl/…-inputEl
# с общим префиксом — тот же инвариант ExtJS, что в CARDS_JS; устойчивее плавающих id).
FIELD_INPUT_JS = r"""
(labels) => {
  const wants = labels.map(s => s.replace(/:$/, '').trim());
  for (const l of document.querySelectorAll('[id$="-labelEl"]')) {
    const t = (l.innerText || l.textContent || '').replace(/:$/, '').trim();
    if (wants.includes(t)) {
      const inp = document.getElementById(l.id.replace(/-labelEl$/, '') + '-inputEl');
      if (inp) return inp.id;
    }
  }
  return null;
}
"""

# JS: выбрать радио «Queries from: File» / «Запросы из: Файл». Имя группы у разных
# сборок разное (queriesFrom / from), и «Файл» есть и у результатов («Сохранить в»).
# Поэтому берём группу радио, где есть И File/Файл, И Text/Текст (это источник запросов),
# и кликаем в ней File/Файл (клик по boxLabel — чтобы ExtJS зарегистрировал).
SELECT_FILE_RADIO_JS = r"""
() => {
  const boxLabel = (inp) => {
    const base = (inp.id || '').replace(/-inputEl$/, '');
    const lab = base ? document.getElementById(base + '-boxLabelEl') : null;
    return { el: lab, t: (lab ? lab.innerText : '').trim() };
  };
  const groups = {};
  for (const r of document.querySelectorAll('input[type=radio]')) {
    (groups[r.name] = groups[r.name] || []).push(r);
  }
  for (const name in groups) {
    const grp = groups[name];
    const labs = grp.map(boxLabel);
    const hasFile = labs.some(x => /^(file|файл)$/i.test(x.t));
    const hasText = labs.some(x => /^(text|текст)$/i.test(x.t));
    if (hasFile && hasText) {
      for (let i = 0; i < grp.length; i++) {
        if (/^(file|файл)$/i.test(labs[i].t)) { (labs[i].el || grp[i]).click(); return true; }
      }
    }
  }
  return false;
}
"""

# JS: клик по кнопке загрузки Task preset (иконка справа от поля «Task preset»/«Задание»).
# Не критично: часть сборок грузит пресет уже при выборе в комбо — отсутствие кнопки не ошибка.
LOAD_PRESET_JS = r"""
() => {
  const lab = [...document.querySelectorAll('[id$="-labelEl"]')]
    .find(l => /^(task preset|задание)$/i.test((l.innerText||'').replace(/:$/,'').trim()));
  if (!lab) return false;
  const row = lab.closest('.x-form-item, .x-field, tr, .x-box-inner') || lab.parentElement;
  const btn = row && (row.querySelector('.x-btn') ||
              (row.parentElement && row.parentElement.querySelector('.x-btn')));
  if (btn) { btn.click(); return true; }
  return false;
}
"""


# JS: задать значение ExtJS-компонента по id (для readonly-комбо «Выберите файл» —
# в него нельзя печатать; setValue принимает массив путей, т.к. комбо мультизначное).
SET_COMBO_VALUE_JS = r"""
(args) => {
  const [cmpId, value] = args;
  if (typeof Ext === 'undefined') return false;
  const c = Ext.getCmp(cmpId);
  if (!c || !c.setValue) return false;
  c.setValue(value);
  return true;
}
"""


def _set_file_field(page, labels, paths: list[str]) -> None:
    """Задаёт readonly-комбо «Выберите файл»/«Select File» через ExtJS setValue
    (печать невозможна). paths — список путей (комбо мультизначное)."""
    iid = page.evaluate(FIELD_INPUT_JS, list(labels))
    if not iid:
        raise TaskCreateNotReady(f"поле {labels} не найдено — сверьте язык UI/дампы")
    cmp_id = iid[:-len("-inputEl")] if iid.endswith("-inputEl") else iid
    if not page.evaluate(SET_COMBO_VALUE_JS, [cmp_id, paths]):
        raise TaskCreateNotReady(f"не удалось задать {labels} через ExtJS setValue")


def _click_text(page, variants, timeout: int = 8000) -> bool:
    """Клик по первому найденному тексту из вариантов (EN/RU). True — если кликнули."""
    for t in variants:
        try:
            page.get_by_text(t, exact=True).first.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


def _field_input(page, labels):
    iid = page.evaluate(FIELD_INPUT_JS, list(labels))
    if not iid:
        raise TaskCreateNotReady(f"поле по подписи {labels} не найдено — сверьте язык UI/дампы")
    return page.locator(f"#{iid}")


def _set_combo(page, labels, value: str) -> None:
    """ExtJS-комбо: вписать значение и выбрать одноимённый пункт из выпадающего списка."""
    inp = _field_input(page, labels)
    inp.click()
    inp.fill(value)
    page.wait_for_timeout(300)
    try:
        page.locator(".x-boundlist-item").get_by_text(value, exact=True).first.click(timeout=2500)
    except Exception:
        inp.press("Enter")                      # запасной путь, если списка нет


def _aparser_rel_path(cfg, path: Path) -> str:
    """Путь к файлу запросов так, как его ждёт UI: относительно корня A-Parser,
    прямыми слэшами (напр. 'queries/brave/B0001_….txt')."""
    p = Path(path)
    root = cfg.get("aparser_root", "") or (
        str(Path(cfg["aparser_exe_path"]).parent) if cfg.get("aparser_exe_path") else "")
    for base in (root, str(Path(cfg["queries_dir"]).parent) if cfg.get("queries_dir") else ""):
        if base:
            try:
                return p.resolve().relative_to(Path(base).resolve()).as_posix()
            except (ValueError, OSError):
                pass
    return p.name


def create_task_from_set(page, cfg, set_name: str, files: list[Path], start: bool) -> None:
    """Создаёт задание под набор в Task Editor: грузит Task preset (autopilot_template_task),
    ставит источник запросов = все файлы набора (через запятую), добавляет в очередь и
    (если start) запускает. Парсер/конфиг/потоки/имя результата ($queriesfile) — от пресета.

    При autopilot_dry_run форма заполняется и снимается скрин, но задание НЕ добавляется
    (raise TaskCreateDryRun). Не найден элемент — TaskCreateNotReady (откат на Phase 1)."""
    if not CREATE_SELECTORS_READY:
        raise TaskCreateNotReady("create_task_from_set отключён (CREATE_SELECTORS_READY)")
    template = cfg.get("autopilot_template_task", "")
    if not template:
        raise TaskCreateNotReady("не задан autopilot_template_task (имя Task preset)")
    if not files:
        raise TaskCreateNotReady(f"набор {set_name} пуст")
    log = get_logger()

    nav_to = int(cfg.get("ui_nav_timeout_ms", 30000) or 30000)
    if not _click_text(page, NAV_TASK_EDITOR, timeout=min(nav_to, 10000)):  # 1) открыть редактор
        raise TaskCreateNotReady("не найден пункт меню 'Task Editor'/'Редактор заданий'")
    page.wait_for_timeout(800)

    cpre = cfg.get("autopilot_config_preset", "")               # 2) пресеты
    if cpre:
        _set_combo(page, LBL_CONFIG_PRESET, cpre)
    _set_combo(page, LBL_TASK_PRESET, template)
    page.evaluate(LOAD_PRESET_JS)                               # применить пресет (если есть кнопка)
    page.wait_for_timeout(800)

    if not page.evaluate(SELECT_FILE_RADIO_JS):                 # 3) Queries from: File / Запросы из: Файл
        raise TaskCreateNotReady("радио 'File'/'Файл' (queriesFrom) не найдено")
    rel = [_aparser_rel_path(cfg, f) for f in files]            # все файлы набора (мультизначное комбо)
    _set_file_field(page, LBL_SELECT_FILE, rel)
    log.info(f"autopilot: форма под набором {set_name}: preset={template}, файлов={len(files)}")
    # File name = $queriesfile наследуется от пресета — не трогаем.

    if bool(cfg.get("autopilot_dry_run", True)):               # обкатка: только скрин
        shot = DATA_DIR / "ui_dumps" / f"autopilot_preview_{set_name}.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot), full_page=True)
        raise TaskCreateDryRun(f"dry-run: превью под {set_name} → {shot}; задание не добавлено")

    if not _click_text(page, BTN_ADD_TASK, timeout=8000):      # 4) в очередь
        raise TaskCreateNotReady("кнопка 'Add Task'/'Добавить задание' не найдена")
    page.wait_for_timeout(1000)
    if start:                                                  # 5) запустить (best-effort)
        _click_text(page, BTN_RUN, timeout=3000)


def _query_sets(cfg) -> dict[str, list[Path]]:
    """{имя_набора: [файлы .txt]} — набор = подпапка queries/<группа>/ с батч-файлами.
    Раскладка кейгена (папка с одним файлом) — частный случай набора из одного файла."""
    qd = cfg.get("queries_dir", "")
    out: dict[str, list[Path]] = {}
    if not qd or not Path(qd).exists():
        return out
    for d in sorted(Path(qd).iterdir()):
        if d.is_dir():
            files = sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() == ".txt")
            if files:
                out[d.name] = files
    return out


def _task_exists_for(name: str, cards: list[dict]) -> bool:
    """Есть ли уже задание под этот набор — ищем имя набора в заголовке карточки очереди."""
    b = name.lower()
    return any(b in (c.get("title", "") or "").lower() for c in cards)


def sets_needing_task(cfg, cards: list[dict]) -> list[tuple[str, list[Path]]]:
    """Наборы, под которые ещё НЕТ задания в очереди и НЕТ готового результата
    (папка results/<имя_набора>/). Порядок — по имени набора (детерминированно)."""
    rd = cfg.get("results_dir", "")
    done = {d.name for d in Path(rd).iterdir() if d.is_dir()} if rd and Path(rd).exists() else set()
    return [(name, files) for name, files in _query_sets(cfg).items()
            if name not in done and not _task_exists_for(name, cards)]


def _autopilot_create(cfg, state, page, pending: list[tuple[str, list[Path]]], log) -> None:
    """Пытается создать задания под наборы без задания; при недоступности авто-создания
    ведёт себя как Phase 1 (уведомление). Проверяет появление заданий в очереди."""
    create = bool(cfg.get("autopilot_create_tasks", False))
    template = cfg.get("autopilot_template_task", "")
    start = bool(cfg.get("autopilot_start_task", True))
    max_new = max(1, int(cfg.get("autopilot_max_new_tasks", 1) or 1))
    names = [n for n, _ in pending]
    log.info(f"autopilot: заданий нет, наборов без задания {len(pending)}: {', '.join(names[:8])}")

    def _notify_idle(reason: str) -> None:
        if cooldown_ok(state, "autopilot:idle", cfg["cooldown_hours"]):
            send_telegram(cfg, f"🟡 <b>A-Parser простаивает</b>\n"
                               f"Наборов без задания: {len(pending)}. Нужно создать задание "
                               f"({reason}).")
            mark_sent(state, "autopilot:idle")

    if not create:
        _notify_idle("авто-создание выключено — autopilot_create_tasks")
        return
    if not template:
        _notify_idle("не задан autopilot_template_task")
        return

    created = 0
    for name, files in pending[:max_new]:
        try:
            create_task_from_set(page, cfg, name, files, start)
            created += 1
            log.info(f"autopilot: создано задание под набор {name}")
        except TaskCreateDryRun as e:
            log.info(f"autopilot: {e}")          # обкатка: скрин снят, задание не добавлено
            return
        except TaskCreateNotReady as e:
            log.warning(f"autopilot: авто-создание недоступно: {e}")
            _notify_idle("авто-создание не отработало — сверьте UI/дампы")
            return
        except Exception as e:  # noqa: BLE001
            log.error(f"autopilot: не удалось создать задание под {name}: {type(e).__name__}: {e}")
            break

    if created:
        cards2 = collect_cards(page, cfg)                    # проверка: задания реально в очереди
        confirmed = [n for n, _ in pending[:max_new] if _task_exists_for(n, cards2)]
        send_telegram(cfg, f"🤖 <b>A-Parser: автопилот создал задания ({len(confirmed)})</b>\n"
                           f"{', '.join(confirmed) or '—'}"
                           f"\n{'Запущены.' if start else 'Созданы на паузе.'}")
        prune_state(state, cfg["cooldown_hours"])


def run_autopilot(cfg, state, log) -> None:
    """При простое A-Parser: есть батчи без задания → создать задание (Phase 2, клон
    эталона) либо уведомить; нет батчей → запустить кейген. Режим --autopilot."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, page = open_ui(pw, cfg)
        try:
            cards = collect_cards(page, cfg)
            active = active_count(cards)
            if active > 0:
                log.info(f"autopilot: активных заданий {active} — ничего не делаем")
                return
            pending = sets_needing_task(cfg, cards)
            if pending:
                _autopilot_create(cfg, state, page, pending, log)
            else:
                log.info("autopilot: ни заданий, ни наборов — запускаю кейген")
                from lib.keygen import run_keygen
                run_keygen(cfg, log)
        finally:
            browser.close()


def run_autopilot_test(cfg, log) -> None:
    """Живая обкатка автосоздания на первом батче без задания: ВИДИМЫЙ браузер,
    принудительный dry-run (форма заполняется, снимается скрин, задание НЕ добавляется).
    Для проверки селекторов на реальной машине перед включением autopilot_create_tasks."""
    from playwright.sync_api import sync_playwright
    cfg = dict(cfg)
    cfg["autopilot_dry_run"] = True                 # тест всегда безопасен
    with sync_playwright() as pw:
        browser, page = open_ui(pw, cfg, headless=False)
        try:
            pending = sets_needing_task(cfg, collect_cards(page, cfg))
            if not pending:
                log.info("autopilot-test: нет наборов без задания — нечего создавать")
                return
            name, files = pending[0]
            log.info(f"autopilot-test: заполняю форму под набор {name} ({len(files)} файлов)")
            try:
                create_task_from_set(page, cfg, name, files, start=False)
            except TaskCreateDryRun as e:
                log.info(f"autopilot-test: OK — {e}")
            except TaskCreateNotReady as e:
                log.error(f"autopilot-test: элемент не найден — {e}")
            page.wait_for_timeout(2000)             # дать посмотреть на форму
        finally:
            browser.close()


def check(cfg) -> None:
    """Диагностика: показать разобранные карточки и кто вызовет тревогу, без отправки."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, page = open_ui(pw, cfg)
        try:
            cards = collect_cards(page, cfg)
        finally:
            browser.close()
    active = active_count(cards)
    print(f"Карточек: {len(cards)}, незавершённых (active): {active} "
          f"(порог ошибок {cfg['error_threshold']:.0%}, min_requests={cfg['min_requests']})")
    for c in cards:
        fp = f"{c['failed_pct']}%" if c["failed_pct"] is not None else "—"
        flag = ""
        if c["failed_pct"] is not None and c["done"] >= cfg["min_requests"] \
                and c["failed_pct"] >= cfg["error_threshold"] * 100:
            flag = "  ← сработает тревога"
        print(f"  {c['title']:<20} status={c['status'] or '?':<10} "
              f"done={c['done']:<9} failed={fp}{flag}")


def dump(cfg) -> None:
    """Заходит в Tasks Queue и выгружает диагностику текущего состояния:
    HTML страницы, скриншот, сырой результат CARDS_JS и outerHTML первых карточек.
    Нужно для разбора, когда --check парсит карточку неверно."""
    from playwright.sync_api import sync_playwright
    out_html, out_png = DATA_DIR / "ui_dump.html", DATA_DIR / "ui_dump.png"
    out_cards, out_first = DATA_DIR / "ui_cards.json", DATA_DIR / "ui_first_cards.html"
    with sync_playwright() as pw:
        browser, page = open_ui(pw, cfg)
        try:
            page.get_by_text("Tasks Queue", exact=True).first.click(timeout=5000)
        except Exception:
            pass
        wait_cards_ready(page, int(cfg.get("ui_cards_timeout_ms", 20000) or 20000))
        out_html.write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(out_png), full_page=True)
        raw = page.evaluate(CARDS_JS)
        # outerHTML первых двух карточек-панелей (по заголовку #NNN) — для инспекции
        first_html = page.evaluate(
            r"""() => {
              const titles = [...document.querySelectorAll('.x-title-text')]
                .filter(h => /^#\d+\s*-/.test((h.innerText||'').trim()));
              return titles.slice(0, 2)
                .map(h => (h.closest('.x-panel') || h.parentElement).outerHTML).join('\n\n');
            }"""
        )
        browser.close()
    out_cards.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    out_first.write_text(first_html or "", encoding="utf-8")
    print(f"HTML:            {out_html}")
    print(f"Скриншот:        {out_png}")
    print(f"Сырые карточки:  {out_cards}  (пришлите этот файл)")
    print(f"Первые карточки: {out_first}")
    print(f"\nВсего карточек в CARDS_JS: {len(raw)}")
    if raw:
        print("Поля первой карточки:")
        for k, v in raw[0].get("fields", {}).items():
            print(f"  {k!r}: {v!r}")


def interactive(cfg) -> None:
    """Открывает ВИДИМЫЙ браузер и сохраняет каждое новое состояние SPA отдельным
    файлом в ui_dumps/ (+ manifest.jsonl). Выход — закрыть окно браузера."""
    from playwright.sync_api import sync_playwright
    dumps_dir = DATA_DIR / "ui_dumps"
    dumps_dir.mkdir(parents=True, exist_ok=True)
    manifest = dumps_dir / "manifest.jsonl"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(cfg["aparser_ui_url"])
        print("Браузер открыт. Залогиньтесь и пройдите по нужным разделам —\n"
              f"каждое новое состояние сохраняется в {dumps_dir.name}/.\n"
              "Закройте окно браузера (или Ctrl+C), чтобы выйти.\n")
        seen: set = set()
        n = 0
        try:
            while not page.is_closed():
                try:
                    html, url = page.content(), page.url
                except Exception:
                    if page.is_closed():
                        break
                    time.sleep(1.0)
                    continue
                sig = (url, len(html))
                if sig not in seen:
                    seen.add(sig)
                    n += 1
                    stem = f"dump_{n:03d}"
                    (dumps_dir / f"{stem}.html").write_text(html, encoding="utf-8")
                    try:
                        page.screenshot(path=str(dumps_dir / f"{stem}.png"), full_page=True)
                    except Exception:
                        pass
                    rec = {"n": n, "file": f"{stem}.html", "ts": int(time.time()),
                           "url": url, "title": page.title(), "html_len": len(html)}
                    with manifest.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    print(f"[{n:03d}] {stem}.html  ({len(html)} симв.)  {url}")
                time.sleep(1.2)
        except KeyboardInterrupt:
            print("\nОстановлено вручную.")
        finally:
            try:
                browser.close()
            except Exception:
                pass
    print(f"\nГотово. Сохранено состояний: {n}. Дампы и manifest.jsonl — в {dumps_dir}.")


def main() -> int:
    # --config <путь>: работать с конкретным конфигом узла (и его state/логом рядом).
    # Позволяет из одной точки (контроллера) вести несколько узлов, не смешивая состояние.
    if "--config" in sys.argv:
        i = sys.argv.index("--config")
        if i + 1 >= len(sys.argv):
            sys.exit("--config требует путь к файлу конфига")
        import aparser_monitor as _am
        _am.set_config_path(sys.argv[i + 1])
    cfg = load_ui_config()
    log = get_logger(want_debug(cfg))   # уровень DEBUG при --debug / "debug": true
    if "--test-telegram" in sys.argv:
        return test_telegram(cfg)
    if "--relay" in sys.argv:
        from lib.relay import run_relay
        return run_relay(cfg, log)
    if "--interactive" in sys.argv or "-i" in sys.argv:
        interactive(cfg)
        return 0
    if "--dump" in sys.argv:
        dump(cfg)
        return 0
    if "--check" in sys.argv:
        check(cfg)
        return 0
    if "--stats" in sys.argv:
        from lib.stats import run_stats
        run_stats(cfg, log)
        return 0
    if "--top" in sys.argv:
        from lib.stats import run_top
        run_top(cfg, log)
        return 0
    if "--keygen" in sys.argv:
        from lib.keygen import run_keygen
        run_keygen(cfg, log)
        return 0
    if "--autopilot-test" in sys.argv:
        run_autopilot_test(cfg, log)
        return 0
    if "--autopilot" in sys.argv:
        state = load_state()
        try:
            run_autopilot(cfg, state, log)
        finally:
            save_state(state)
        return 0
    if "--autosend" in sys.argv:
        # Только отправка результатов на шару (без UI-мониторинга и детекта завершения).
        # Для узлов при централизованном мониторинге: монитор ведёт контроллер, а
        # autosend (файловая операция с локальными результатами) остаётся на узле.
        state = load_state()
        try:
            run_autosend(cfg, state, log)
        finally:
            save_state(state)
        return 0
    state = load_state()
    try:
        try:
            summary = run(cfg, state)
        except Exception as e:  # недоступность интерфейса/таймаут/сбой браузера/логина
            handle_ui_down(cfg, state, e, log)
            log.warning(f"NOT OK — {type(e).__name__}: {e}")
        else:
            log.info(f"OK — {summary}")
            maybe_heartbeat(cfg, state, summary)
        # Autosend не зависит от доступности A-Parser — файловая операция
        try:
            run_autosend(cfg, state, log)
        except Exception as e:
            log.error(f"autosend упал: {type(e).__name__}: {e}")
    finally:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
