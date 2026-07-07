# aparser_monitor — уведомления A-Parser → Telegram

Уведомления в Telegram о состоянии A-Parser. Есть **две версии** — под разные лицензии:

| Скрипт | Лицензия | Как читает состояние | Что умеет |
|--------|----------|----------------------|-----------|
| `aparser_monitor.py` | **Enterprise** | HTTP API | завершение задания, доля ошибок > 50%, недоступность |
| `aparser_monitor_ui.py` | **Pro и Enterprise** | web-интерфейс (Playwright) | завершение парсинга, доля ошибок > 50%, недоступность интерфейса |

> **API — функция только Enterprise-версии.** Если у вас Pro, `aparser_monitor.py` вернёт 404 на `/API` — используйте `aparser_monitor_ui.py` (см. раздел «Вариант для Pro» ниже).

Все уведомления с кулдауном 8 часов, чтобы не спамить.

---

## Вариант для Pro (без API): `aparser_monitor_ui.py`

Читает web-интерфейс A-Parser через headless-браузер (Playwright). Проверено на A-Parser Pro v1.2.3293. За проход:

1. Логинится (`POST /auth`), заходит в **Tasks Queue** и обходит все страницы, разбирая карточки заданий (значения полей читаются структурно, из самого элемента `.x-form-display-field`).
2. **✅ Парсинг завершён** — когда число незавершённых заданий (`work`/`waitSlot`/…) падает с >0 до 0.
3. **⚠️ Много ошибок** — A-Parser сам показывает в карточке `Failed queries: 147801 99.8%`; если доля ≥ порога и обработано ≥ `min_requests` запросов → тревога по этому заданию.
4. Интерфейс не отвечает → **🔴**, снова доступен → **🟢**.

> Живые поля карточки (State) сначала рендерятся плейсхолдером `Display Field` и заполняются на следующем тике опроса A-Parser — скрипт дожидается их готовности перед чтением.

```
pip install playwright
playwright install chromium
```

В `aparser_monitor.config.json` добавьте:
```json
"aparser_ui_url": "http://127.0.0.1:9092/",
"aparser_ui_password": "пароль от web-интерфейса"
```

Режимы запуска:
```
python aparser_monitor_ui.py            # рабочий проход (для планировщика)
python aparser_monitor_ui.py --check    # диагностика: что видит скрипт, без отправки в Telegram
python aparser_monitor_ui.py --dump     # снимок Tasks Queue: HTML + PNG + ui_cards.json (сырые карточки)
python aparser_monitor_ui.py --interactive  # видимый браузер: собрать дампы разделов в ui_dumps/
```

**Перед постановкой на расписание запустите `--check`** — он покажет разобранные карточки и какие вызовут тревогу, ничего не отправляя. Константы под свой билд (`LOGIN_*`, `NEXT_PAGE`, `CARDS_JS`, `PLACEHOLDER`) вынесены вверх файла; если у вас другая сборка и `--check` парсит неверно — сверьте по `--dump` (файл `ui_cards.json`).

### Планировщик (запуск раз в 5 минут)

Скрипт разовый — кулдаун, «уже отправленные» и признак предыдущего состояния хранятся в `aparser_monitor.state.json`.

**Windows (Планировщик задач):**
```
schtasks /Create /SC MINUTE /MO 5 /TN "aparser-monitor-ui" ^
  /TR "python C:\Monitor\aparser_monitor_ui.py"
```
Путь `C:\Monitor\` замените на свой каталог со скриптом. Полезные флаги: `/RU SYSTEM` — запуск от системы без входа в сессию; проверить/запустить вручную — `schtasks /Run /TN "aparser-monitor-ui"`; удалить — `schtasks /Delete /TN "aparser-monitor-ui" /F`.

**Linux (cron):**
```
*/5 * * * * cd /path/aparser_monitor && /usr/bin/python3 aparser_monitor_ui.py >> monitor.log 2>&1
```

> Учтите: каждый проход поднимает headless-Chromium и обходит все страницы Tasks Queue (~10–20 с при девяти страницах). Интервал 5 минут это с запасом покрывает. Если проходов много и они наслаиваются — увеличьте интервал.

---

## Вариант для Enterprise (API): `aparser_monitor.py`

Опрашивает JSON API A-Parser и шлёт в Telegram: ✅ задание завершено; ⚠️ доля ошибок > 50%; 🔴 API недоступен / 🟢 снова доступен.

## Настройка

1. Создайте Telegram-бота у [@BotFather](https://t.me/BotFather) → получите `bot_token`. Узнать `chat_id`: напишите боту, затем откройте `https://api.telegram.org/bot<TOKEN>/getUpdates` и возьмите `chat.id`.
2. В A-Parser включите API (Настройки → API), запомните порт (по умолчанию `9091`). Пароль опционален — если API без пароля, оставьте `aparser_password` пустым (`""`).
3. Скопируйте `aparser_monitor.config.example.json` → `aparser_monitor.config.json` и заполните `aparser_password`, `telegram_bot_token`, `telegram_chat_id`, `aparser_url`. Либо задайте те же значения переменными окружения (`APARSER_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `APARSER_URL`) — они имеют приоритет над файлом.

## Запуск

```
pip install -r requirements.txt
python aparser_monitor.py
```

Скрипт разовый — состояние (кулдаун, уже отправленные, флаг доступности) хранится в `aparser_monitor.state.json` рядом со скриптом. Ставьте его на расписание раз в 2–5 минут.

Windows (Планировщик задач), запуск каждые 5 минут:

```
schtasks /Create /SC MINUTE /MO 5 /TN "aparser-monitor" ^
  /TR "python path\aparser_monitor.py"
```

Linux (cron):

```
*/5 * * * * /usr/bin/python3 /path/aparser_monitor/aparser_monitor.py
```

Параметры (`error_threshold`, `cooldown_hours`, `min_requests`) правятся в конфиге. `min_requests` — минимум запросов, ниже которого процент ошибок не считается (чтобы 1 из 1 не давал ложную тревогу).

> Имена счётчиков good/bad в ответе `getTaskState` зависят от версии A-Parser. Скрипт ищет их по нескольким вероятным ключам (`extract_counters` в `aparser_monitor.py`); если тревоги по ошибкам не срабатывают — сверьте реальный ответ API и поправьте списки ключей.

## Что нужно для запуска на серверах

Скрипт лёгкий, ставится где угодно, откуда виден API A-Parser и есть выход в интернет к Telegram.

- **Python 3.9+** и `pip install requests` (единственная зависимость).
- **Файлы на сервер:** содержимое этой папки — `aparser_monitor.py` + заполненный `aparser_monitor.config.json` (или переменные окружения). Файл `aparser_monitor.state.json` скрипт создаст сам — каталог должен быть доступен на запись.
- **Сетевой доступ:**
  - до API A-Parser — `aparser_url` (по умолчанию порт `9091`). Если монитор на том же сервере, что и A-Parser, хватит `http://127.0.0.1:9091/API`. Если на другом — откройте порт только для IP монитора и держите API за паролем/файрволом (по HTTP пароль идёт в теле запроса, желательно закрытая сеть или HTTPS-прокси).
  - до `api.telegram.org:443` (исходящий HTTPS).
- **Планировщик** для периодического запуска (раз в 2–5 мин): Windows — Планировщик задач (`schtasks`) или встроенный планировщик A-Parser; Linux — cron.
- **На каждый сервер с A-Parser — свой запуск** (свой `config.json` с его `aparser_url` и свой `state.json`). Один монитор может следить и за несколькими серверами, но тогда его нужно доработать под список адресов — сейчас один запуск = один A-Parser.

Секреты (пароль API, токен бота) не коммитьте: `aparser_monitor.config.json` и `aparser_monitor.state.json` уже в `.gitignore`.
