#!/usr/bin/env python3
"""
aparser_monitor_ui.py — Pro-совместимый монитор A-Parser через web-интерфейс.

В A-Parser Pro нет HTTP API (он только в Enterprise), поэтому состояние читается
из web-интерфейса с помощью Playwright (браузер без окна). Что делает за проход:
  1. Логинится (POST /auth) и читает нижнюю статус-строку
     «Parsing | Tasks: 1/44 | Threads: 200» → число активных заданий. Когда оно
     падает с >0 до 0 — парсинг завершён, шлём ✅.
  2. Заходит в раздел Tasks Queue, обходит страницы и разбирает карточки заданий.
     В каждой карточке A-Parser уже показывает «Failed queries: 147801 99.8%» —
     если доля ошибок >= порога, шлём ⚠️ по этому заданию.
  3. Недоступность интерфейса → 🔴, восстановление → 🟢.
Кулдаун/дедуп/Telegram — общие с aparser_monitor.py.

Проверено по дампам A-Parser Pro v1.2.3293 (ExtJS, логин input[name=password]).

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

Селекторы проверены на сборке 1.2.x. Если интерфейс другой — сверьте по --dump и
поправьте константы LOGIN_*/NEXT_PAGE и регэкспы CARD_*/FAILED_RE.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

# переиспользуем конфиг/состояние/кулдаун/Telegram из основного скрипта
from aparser_monitor import (
    CONFIG_PATH, DEFAULTS, load_state, save_state,
    send_telegram, cooldown_ok, mark_sent, prune_state,
)

HERE = Path(__file__).resolve().parent

# ── Реальные точки интерфейса A-Parser Pro (v1.2.x) ───────────────────────────
LOGIN_PASSWORD = 'input[name="password"]'   # поле пароля на странице /auth
LOGIN_SUBMIT = 'input[type="submit"]'        # кнопка «Log in»
NEXT_PAGE = ".x-tbar-page-next"              # кнопка «следующая страница» в Tasks Queue
# «Tasks: <активные>/<всего>» из нижней статус-строки
STATUS_RE = re.compile(r"Tasks:\s*(\d+)\s*/\s*(\d+)")
# поля внутри карточки задания в Tasks Queue
CARD_SPLIT_RE = re.compile(r"(#\d+\s*-\s*\S+)")           # заголовок карточки «#209 - aparser»
FAILED_RE = re.compile(r"Failed queries:\s*(\d+)\s*([\d.]+)\s*%")   # «147801 99.8%»
DONE_RE = re.compile(r"Queries done/all:\s*(\d+)\s*/\s*(\d+)")      # «148034/588410»
CARD_STATUS_RE = re.compile(r"Status:\s*([A-Za-z]+)")              # «work» / «waiting» / …


def load_ui_config() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update({"aparser_ui_url": "http://127.0.0.1:9091/", "aparser_ui_password": ""})
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    cfg["cooldown_hours"] = int(float(cfg["cooldown_hours"]))
    cfg["error_threshold"] = float(cfg["error_threshold"])
    cfg["min_requests"] = int(float(cfg["min_requests"]))
    for k in ("telegram_bot_token", "telegram_chat_id"):
        if not cfg.get(k):
            sys.exit(f"Не задан обязательный параметр: {k}. Заполните {CONFIG_PATH.name}.")
    return cfg


def open_ui(pw, cfg, headless: bool = True):
    """Открывает интерфейс, логинится при необходимости, ждёт отрисовки статус-строки."""
    browser = pw.chromium.launch(headless=headless)
    page = browser.new_page()
    page.goto(cfg["aparser_ui_url"], wait_until="domcontentloaded", timeout=30000)
    # страница пароля показывается формой с input[name=password]
    if page.query_selector(LOGIN_PASSWORD):
        page.fill(LOGIN_PASSWORD, cfg["aparser_ui_password"])
        page.click(LOGIN_SUBMIT)
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    # ждём, пока ExtJS отрисует нижнюю статус-строку "Tasks: N/N"
    page.wait_for_function(
        r"() => document.body && /Tasks:\s*\d+\s*\/\s*\d+/.test(document.body.innerText)",
        timeout=30000,
    )
    return browser, page


def read_status(page) -> tuple[bool, int | None, int | None]:
    """Возвращает (идёт_ли_парсинг, активных_заданий, всего_в_очереди)."""
    text = page.inner_text("body")
    m = STATUS_RE.search(text)
    if not m:
        return ("Parsing" in text, None, None)
    return ("Parsing" in text, int(m.group(1)), int(m.group(2)))


def parse_cards(text: str) -> list[dict]:
    """Разбирает текст страницы Tasks Queue на карточки заданий."""
    parts = CARD_SPLIT_RE.split(text)
    cards = []
    for i in range(1, len(parts) - 1, 2):
        title, body = parts[i].strip(), parts[i + 1]
        fm, dm, sm = FAILED_RE.search(body), DONE_RE.search(body), CARD_STATUS_RE.search(body)
        cards.append({
            "title": title,
            "failed_pct": float(fm.group(2)) if fm else None,
            "done": int(dm.group(1)) if dm else 0,
            "status": sm.group(1) if sm else "",
        })
    return cards


def collect_cards(page, max_pages: int = 25) -> list[dict]:
    """Обходит страницы Tasks Queue и собирает уникальные карточки. Стоп — когда
    следующая страница не даёт новых заданий (последняя) или исчерпан лимит."""
    # перейти в раздел Tasks Queue, если ещё не там
    try:
        page.get_by_text("Tasks Queue", exact=True).first.click(timeout=5000)
        page.wait_for_timeout(1200)
    except Exception:
        pass
    cards, seen, prev_titles = [], set(), None
    for _ in range(max_pages):
        page.wait_for_timeout(700)
        titles = []
        for c in parse_cards(page.inner_text("body")):
            titles.append(c["title"])
            if c["title"] not in seen:
                seen.add(c["title"])
                cards.append(c)
        if titles == prev_titles:      # страница не сменилась → это была последняя
            break
        prev_titles = titles
        nxt = page.query_selector(NEXT_PAGE)
        if not nxt:
            break
        try:
            nxt.click()
        except Exception:
            break
    return cards


# --------------------------------------------------------------------------- #
# Уведомления
# --------------------------------------------------------------------------- #
def notify_completion(cfg, state, parsing: bool, active: int, total: int) -> None:
    """Событие «парсинг завершён» = активные задания упали с >0 до 0."""
    prev = state.get("ui_active_prev")
    if prev is not None and prev > 0 and active == 0:
        key = "done:ui"
        if cooldown_ok(state, key, cfg["cooldown_hours"]):
            send_telegram(
                cfg,
                f"✅ <b>A-Parser: парсинг завершён</b>\n"
                f"Активных заданий не осталось (было {prev}, всего в очереди {total}).",
            )
            mark_sent(state, key)
            print("[done] parsing finished")
    state["ui_active_prev"] = active
    prune_state(state, cfg["cooldown_hours"])
    print(f"[ok] parsing={parsing} active={active}/{total} prev={prev}")


def notify_errors(cfg, state, cards: list[dict]) -> None:
    """Тревога по карточкам, где доля ошибок >= порога (учитываем только задания,
    где обработано хотя бы min_requests запросов, чтобы не ловить шум на старте)."""
    threshold_pct = cfg["error_threshold"] * 100
    for c in cards:
        if c["failed_pct"] is None or c["done"] < cfg["min_requests"]:
            continue
        if c["failed_pct"] >= threshold_pct:
            key = f"errors:{c['title']}"
            if cooldown_ok(state, key, cfg["cooldown_hours"]):
                send_telegram(
                    cfg,
                    f"⚠️ <b>A-Parser: много ошибок</b>\n"
                    f"Задание: <b>{c['title']}</b> (status: {c['status'] or '?'})\n"
                    f"Ошибок: <b>{c['failed_pct']:.1f}%</b> при {c['done']} обработанных запросах",
                )
                mark_sent(state, key)
                print(f"[alert] {c['title']} failed={c['failed_pct']}%")


def handle_ui_down(cfg, state, err) -> None:
    was_down = state.get("down", False)
    key = "down:global"
    if not was_down or cooldown_ok(state, key, cfg["cooldown_hours"]):
        send_telegram(cfg, f"🔴 <b>A-Parser: интерфейс недоступен</b>\n{cfg['aparser_ui_url']}\n"
                           f"{type(err).__name__}: {err}")
        mark_sent(state, key)
    state["down"] = True
    print(f"[down] {type(err).__name__}: {err}", file=sys.stderr)


def run(cfg, state) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, page = open_ui(pw, cfg)
        try:
            parsing, active, total = read_status(page)
            cards = collect_cards(page)
        finally:
            browser.close()
    if active is None:
        raise RuntimeError("Не удалось прочитать статус-строку 'Tasks: N/N'")
    # восстановление доступности
    if state.get("down"):
        send_telegram(cfg, f"🟢 <b>A-Parser: интерфейс снова доступен</b>\n{cfg['aparser_ui_url']}")
    state["down"] = False
    notify_completion(cfg, state, parsing, active, total)
    notify_errors(cfg, state, cards)


def check(cfg) -> None:
    """Диагностика: показать, что скрипт видит (статус-строка + разбор карточек),
    ничего не отправляя в Telegram. Запускать после настройки логина."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, page = open_ui(pw, cfg)
        try:
            parsing, active, total = read_status(page)
            cards = collect_cards(page)
        finally:
            browser.close()
    print(f"Статус-строка: parsing={parsing} active={active}/{total}")
    print(f"Найдено карточек: {len(cards)} (порог ошибок {cfg['error_threshold']:.0%}, "
          f"min_requests={cfg['min_requests']})")
    for c in cards:
        flag = ""
        if c["failed_pct"] is not None and c["done"] >= cfg["min_requests"] \
                and c["failed_pct"] >= cfg["error_threshold"] * 100:
            flag = "  ← сработает тревога"
        print(f"  {c['title']:<20} status={c['status'] or '?':<8} "
              f"done={c['done']:<9} failed={c['failed_pct']}%{flag}")


# --------------------------------------------------------------------------- #
# Вспомогательные режимы
# --------------------------------------------------------------------------- #
def dump(cfg) -> None:
    """Разовый снимок (headless) после автологина — HTML + скриншот текущего экрана."""
    from playwright.sync_api import sync_playwright
    out_html, out_png = HERE / "ui_dump.html", HERE / "ui_dump.png"
    with sync_playwright() as pw:
        browser, page = open_ui(pw, cfg)
        out_html.write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(out_png), full_page=True)
        parsing, active, total = read_status(page)
        browser.close()
    print(f"Статус-строка: parsing={parsing} active={active}/{total}")
    print(f"HTML:     {out_html}")
    print(f"Скриншот: {out_png}")


def interactive(cfg) -> None:
    """Открывает ВИДИМЫЙ браузер и сохраняет каждое новое состояние SPA отдельным
    файлом в ui_dumps/ (+ manifest.jsonl). Нужно для разбора новых разделов, напр.
    Tasks Queue, чтобы добавить чтение Good/Bad. Выход — закрыть окно браузера."""
    from playwright.sync_api import sync_playwright
    dumps_dir = HERE / "ui_dumps"
    dumps_dir.mkdir(exist_ok=True)
    manifest = dumps_dir / "manifest.jsonl"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(cfg["aparser_ui_url"])
        print("Браузер открыт. Залогиньтесь и пройдите по нужным разделам (напр. Tasks\n"
              f"Queue) — каждое новое состояние сохраняется в {dumps_dir.name}/.\n"
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
                sig = (url, len(html))  # маршрут SPA + размер разметки
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
    if "--interactive" in sys.argv or "-i" in sys.argv:
        interactive(cfg)
        return 0
    if "--dump" in sys.argv:
        dump(cfg)
        return 0
    if "--check" in sys.argv:
        check(cfg)
        return 0
    state = load_state()
    try:
        try:
            run(cfg, state)
        except Exception as e:  # недоступность интерфейса/таймаут/сбой браузера/логина
            handle_ui_down(cfg, state, e)
    finally:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
