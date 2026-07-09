# КОНТЕКСТ ПРОЕКТА: aparser_monitor (Aparser-checker)

Документ для передачи контекста в новый диалог/среду. Самодостаточный.

## Что это
Мониторинг и автоматизация **A-Parser** (SEO-скрапер) с уведомлениями в Telegram + конвейер генерации ключей. Разрабатывается итеративно, каждое изменение коммитится и пушится в GitHub.

## Репозиторий и окружение
- **GitHub:** `MizziDiz/Aparser-checker` — **ПУБЛИЧНЫЙ**. `gh` авторизован как MizziDiz. `git push` работает через Git Credential Manager.
- **Локально (рабочая машина):** `C:\Users\namit\Documents\cc-links\aparser_monitor`
- **На серверах:** `C:\Monitor`
- **Текущая версия:** v2.1.0 (Latest). Теги: v1.0.0, v2.0.0, v2.0.1, v2.0.2, v2.1.0.
- Коммиты заканчивать: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Ветка `main`.

## Инфраструктура пользователя (важно!)
- **A-Parser Pro** (НЕ Enterprise) → **HTTP API недоступен** (даёт 404 на `/API`). Основной монитор — через **web-интерфейс** (Playwright).
- Несколько **Windows-серверов в локальной сети** `10.10.10.0/24`. Сервер-**релей** `10.10.10.2` (у него есть доступ к Telegram), клиенты `10.10.10.13`, `10.10.10.14` и др.
- **Telegram заблокирован** на большинстве серверов (RU) → используется **релей** (или прокси).
- Web-интерфейс A-Parser — **SPA на ExtJS**; на части серверов **русский**, на части английский.
- Python 3.10/3.12. Проверено на A-Parser **Pro v1.2.3293**.

## Структура репозитория
```
aparser_monitor_ui.py     # ГЛАВНАЯ точка входа: монитор Pro (web-UI) + все режимы
aparser_monitor.py        # монитор API (Enterprise) + ОБЩИЙ КОД (Telegram/конфиг/state/лог/рестарт/heartbeat)
aparser_config_gui.py     # редактор настроек (Tkinter → exe)
aparser-config.exe        # собранный редактор (gitignored, собирается локально)
lib/
  autosend.py             # отправка результатов на UNC-шару + уборка
  relay.py                # сервер-релей Telegram (build_relay/run_relay)
  stats.py                # статистика (зоны/операторы/снимки) + остаток/ETA
  keygen.py               # оркестрация кейгена → батчи в queries
  config_schema.py        # ЕДИНАЯ схема полей конфига (для GUI и будущего веба)
keygen/                   # ВЛОЖЕННЫЙ генератор ключей (gsa_geo_pipeline.py + kwbuilder/ + xlsx)
config.example.json       # шаблон конфига
build-config-exe.bat      # сборка exe (PyInstaller, exe кладётся в корень)
requirements.txt          # requests, playwright, tldextract
README.md, ROADMAP.md
data/                     # РАБОЧИЕ ДАННЫЕ (gitignored, авто-создаётся):
                          #   aparser_monitor.config.json, .state.json, .log,
                          #   aparser_sent.jsonl, aparser_stats.db, ui_dumps/, keygen_stage/
```

## Команды/режимы (`aparser_monitor_ui.py`)
| Команда | Действие |
|---|---|
| (без флага) | рабочий проход монитора |
| `--check` | диагностика: разобранные карточки, без отправки |
| `--dump` | снимок Tasks Queue: HTML+PNG+`ui_cards.json` |
| `--interactive` | видимый браузер, копит дампы всех экранов в `data/ui_dumps/` |
| `--debug` | подробные логи (или `"debug": true`) |
| `--test-telegram` | тест канала (прямой/через релей) |
| `--relay` | поднять сервер-релей (долгоживущий, ONSTART) |
| `--stats` | собрать статистику из query/result-файлов в SQLite |
| `--keygen` | сгенерить батчи и разложить в queries |
| `--autopilot` | при простое: нет батчей → кейген; есть батчи → уведомление |

## Ключевые технические факты
- **Логин A-Parser UI:** `POST /auth`, поле `input[name="password"]`, кнопка `input[type="submit"]`.
- **Карточки Tasks Queue:** ExtJS. Значение читается из `.x-form-display-field` (id `…-inputEl`), подпись — из парного `…-labelEl`, узлы обходятся в порядке документа (`CARDS_JS` в ui-скрипте). Поддержка **EN+RU подписей** (`Status:`/`Статус:`, `Failed queries:`/`Неудачных запросов:`, `Queries done/all:`/`Запросы заверш./всего:`, `Speed cur/avg:`, `Results unique/all:`).
- **Плейсхолдер `Display Field`** — живые поля заполняются на следующем тике; `wait_cards_ready` ждёт их.
- **Завершение** = число незавершённых заданий (`active_count`, статус не в `{completed,complete,done,finished}`) упало с >0 до 0.
- **Статусы A-Parser** (`work`, `waitSlot`, …) — внутренние коды, не локализуются.
- **Автосенд/статистика/кейген** используют раскладку `queries/<имя>/<имя>.txt` ↔ `results/<имя>/<имя>.txt`.
- **ETA:** скорость считается по снимкам `task_snapshots` (Δdone/Δвремя), единицы неважны.

## Важные конфиг-ключи (все в `config.example.json`)
- Мониторинг: `aparser_ui_url`, `aparser_ui_password` (Pro); `aparser_url`, `aparser_password` (API).
- Telegram: `telegram_bot_token`, `telegram_chat_id`, `server_name` (подпись сервера в сообщениях), `telegram_proxy`.
- Релей: `telegram_relay_url` (клиенты), `relay_secret`, `relay_port` (8899), `relay_bind`, `relay_allowed_ips` (напр. `10.10.10.0/24`).
- Пороги: `error_threshold` (0.5), `cooldown_hours` (8), `min_requests` (20), `heartbeat_hours` (6).
- Рестарт: `aparser_exe_path`, `restart_after_failures` (3), `restart_cooldown_min` (15).
- Autosend: `queries_dir`, `results_dir`, `autosend_dest` (UNC), `autosend_settle_min` (2), `autosend_cleanup_min` (1440).
- Статистика: `stats_settle_min` (2), `stats_snapshots` (true), `stats_retention_days` (30), `almost_done_pct` (90), `eta_window_min` (30).
- Кейген: `keygen_script`, `keygen_input_xlsx`, `keygen_python`, `keygen_batches` (5), `keygen_target_mb` (6), `keygen_pages` (25), `keygen_footprints_per_seed` (24).
- `debug`.

## Развёртывание (планировщик Windows)
- **Монитор:** `schtasks /Create /TN "aparser-monitor-ui" /SC MINUTE /MO 5 ...` — **от пользователя** (НЕ SYSTEM: нужен профиль с Playwright-браузером и доступ к UNC-шаре). Полный путь к `python.exe`.
- **Релей:** `relay-run.bat` (цикл `--relay` с перезапуском) + `schtasks /SC ONSTART /RU SYSTEM` (нужен запуск редактора/cmd **от администратора**).
- **--stats / --keygen / --autopilot:** отдельные задачи, реже (15–30 мин).
- Зависимости на сервере: `pip install requests playwright tldextract openpyxl`, `playwright install chromium`, **VC++ Redistributable** (иначе Chromium падает «browser closed»), на Windows Server — `Install-WindowsFeature Server-Media-Foundation`. **tkinter** нужен для запуска GUI как `.py` (или использовать собранный exe, он Tk бандлит).

## Кейген (в `keygen/`)
Цепочка: `kwbuilder.build` → xlsx (2 листа) → `gsa_geo_pipeline.py --input-xlsx ... --out-dir` → батчи `B####_....txt` со строками `{Seed} {Operator} {Footprint}` (напр. `loans site:.cz inurl:"..."`). `--keygen` запускает пайплайн со случайным seed и раскладывает батчи в `queries/<имя>/<имя>.txt`. Нужен `openpyxl`.

## Известные грабли / решения
- Pro → 404 на API → использовать UI-версию.
- Задача от **SYSTEM** ломает монитор (нет браузера/шары) → запускать от пользователя.
- Кривой JSON конфига → `read_config_file` даёт понятную ошибку.
- Windows-консоль cp1251 не печатает эмодзи (только в тестах; в Telegram/лог всё ок).
- **Токен бота светился в переписке ранее — рекомендовано отозвать у @BotFather** (`/revoke`).
- Релей был открыт в интернет (сканеры) → закрыт: `relay_bind` на LAN-IP + `relay_allowed_ips` + фаервол `remoteip=10.10.10.0/24`; оборванные соединения игнорируются молча.
- Пути в **редакторе (GUI/exe)** — обычные (`C:\...`); в **JSON руками** — `\\` или прямые слэши.

## ROADMAP — что осталось
1. **Автопилот Phase 2 (следующий шаг):** авто-создание задания через UI под готовый батч. **Нужны дампы экранов Quick Task / Task Editor** (через `--interactive`) + решить «подходящие настройки» (какой парсер, какой конфиг; в карточках было `Config preset: 200t`, `Конфиг потоков: aparser`). Пресеты — позже.
2. **Интерфейс к статистике** (вкладка в exe или веб-дашборд).
3. **Топ-сводки в Telegram** (топ зон/операторов за период из `aparser_stats.db`).

## Как продолжить в новой среде
```
git clone https://github.com/MizziDiz/Aparser-checker.git C:\Monitor
cd C:\Monitor
py -m pip install -r requirements.txt
py -m pip install tldextract openpyxl
playwright install chromium
# config.example.json → data\aparser_monitor.config.json (пути под новую среду)
# при необходимости пересобрать редактор: build-config-exe.bat
```
`data/` не клонируется (у каждой среды свой конфиг/состояние/БД).
