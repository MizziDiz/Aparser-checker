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
    for k in ("telegram_bot_token", "telegram_chat_id"):
        if not cfg.get(k):
            sys.exit(f"Не задан обязательный параметр: {k}. Заполните {CONFIG_PATH.name}.")
    return cfg


def open_ui(pw, cfg, headless: bool = True):
    """Открывает интерфейс, логинится при необходимости, ждёт загрузки приложения."""
    browser = pw.chromium.launch(headless=headless)
    page = browser.new_page()
    page.goto(cfg["aparser_ui_url"], wait_until="domcontentloaded", timeout=30000)
    if page.query_selector(LOGIN_PASSWORD):
        page.fill(LOGIN_PASSWORD, cfg["aparser_ui_password"])
        page.click(LOGIN_SUBMIT)
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    # ждём, пока ExtJS отрисует нижнюю статус-строку (признак загрузки приложения)
    page.wait_for_function(
        r"() => document.body && /Tasks:\s*\d+\s*\/\s*\d+/.test(document.body.innerText)",
        timeout=30000,
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


def _card_from_raw(raw: dict) -> dict:
    f = raw.get("fields", {})
    fm = FAILED_PCT_RE.search(_field(f, LABELS_FAILED))
    dm = FIRST_INT_RE.search(_field(f, LABELS_DONE))
    return {
        "title": raw.get("title", "?"),
        "failed_pct": float(fm.group(1)) if fm else None,
        "done": int(dm.group(1)) if dm else 0,
        "status": _field(f, LABELS_STATUS).strip(),
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


def collect_cards(page, max_pages: int = 25) -> list[dict]:
    """Заходит в Tasks Queue, обходит все страницы, собирает уникальные карточки.
    Стоп — когда следующая страница не даёт новых заданий (последняя)."""
    try:
        page.get_by_text("Tasks Queue", exact=True).first.click(timeout=5000)
    except Exception:
        pass
    cards, seen = [], set()
    for _ in range(max_pages):
        wait_cards_ready(page)                 # дождаться заполнения живых полей
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
        for _ in range(25):                    # до ~5 c
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
    """Событие «парсинг завершён» = число незавершённых заданий упало с >0 до 0."""
    prev = state.get("ui_active_prev")
    if prev is not None and prev > 0 and active == 0:
        key = "done:ui"
        if cooldown_ok(state, key, cfg["cooldown_hours"]):
            send_telegram(
                cfg,
                f"✅ <b>A-Parser: парсинг завершён</b>\n"
                f"Незавершённых заданий не осталось (было {prev}).",
            )
            mark_sent(state, key)
            print("[done] parsing finished")
    state["ui_active_prev"] = active
    prune_state(state, cfg["cooldown_hours"])
    print(f"[ok] active={active}/{total} prev={prev}")


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
            cards = collect_cards(page)
        finally:
            browser.close()
    if state.get("down"):
        send_telegram(cfg, f"🟢 <b>A-Parser: интерфейс снова доступен</b>\n{cfg['aparser_ui_url']}")
    state["down"] = False
    state["down_count"] = 0
    active = active_count(cards)
    notify_completion(cfg, state, active, len(cards))
    over = notify_errors(cfg, state, cards)
    return f"заданий {len(cards)}, активных {active}, с ошибками>{cfg['error_threshold']:.0%}: {over}"


# --------------------------------------------------------------------------- #
# Вспомогательные режимы
# --------------------------------------------------------------------------- #
def check(cfg) -> None:
    """Диагностика: показать разобранные карточки и кто вызовет тревогу, без отправки."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, page = open_ui(pw, cfg)
        try:
            cards = collect_cards(page)
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
        wait_cards_ready(page)
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
