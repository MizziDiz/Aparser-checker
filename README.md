# aparser_monitor — уведомления A-Parser → Telegram

Отдельный самодостаточный скрипт: опрашивает API A-Parser и шлёт в Telegram:

- ✅ задание завершено;
- ⚠️ авария — доля ошибок > 50%;
- 🔴 A-Parser недоступен (API не отвечает: обрыв, таймаут, ошибка) и 🟢 снова доступен.

Все уведомления с кулдауном 8 часов, чтобы не спамить.

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
  /TR "python C:\Users\namit\Documents\cc-links\aparser_monitor\aparser_monitor.py"
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
