#!/bin/bash
# recreate-creds.sh — пересоздание кредов/конфигов харвеста С НУЛЯ, если они исчезли.
# Создаёт файлы-скелеты с плейсхолдерами; заполни <...> реальными значениями.
# Секреты (пароли/токен) в git НЕ хранятся — только локально на контроллере.
set -e
cd "$(dirname "$0")"          # корень репо

echo "== 1. SMB-креды нод (/root/.smbcreds<N>) =="
# Впиши реальные креды SMB-доступа к узлу (username/password/domain).
NODES="${NODES:-1 2 3}"          # ← номера своих узлов, через пробел (пример: 1 2 3)
for n in $NODES; do
  f="/root/.smbcreds$n"
  if [ -e "$f" ]; then echo "  $f уже есть — пропуск"; continue; fi
  printf 'username=<SMB_USER>\npassword=<SMB_PASS>\ndomain=WORKGROUP\n' > "$f"
  chmod 600 "$f"
  echo "  создан $f (впиши username/password)"
done

echo "== 2. Инфра-конфиг раннера (data/nodes/harvest_node.json) =="
# LAN-IP узла + путь к SMB-кредам + пути A-Parser на узле. Раннер читает его.
f="data/nodes/harvest_node.json"
if [ -e "$f" ]; then echo "  $f уже есть — пропуск"; else
  cat > "$f" <<'JSON'
{
  "smb": "//<LAN-IP-УЗЛА>/C$",
  "creds": "/root/.smbcreds<N>",
  "queries_unc": "soft\\aparser\\queries",
  "results_unc": "soft\\aparser\\results",
  "ui_url": "http://<LAN-IP-УЗЛА>:9092/"
}
JSON
  echo "  создан $f — впиши <LAN-IP-УЗЛА> и <N> (напр. 14)"
fi

echo "== 3. Node-конфиги монитора (data/nodes/node-<N>.config.json) =="
# Telegram-токен/чат + UI-url/порт/таймауты. Скелет с плейсхолдерами.
for n in $NODES; do
  f="data/nodes/node-$n.config.json"
  if [ -e "$f" ]; then echo "  $f уже есть — пропуск"; continue; fi
  cat > "$f" <<JSON
{
  "aparser_ui_url": "http://<LAN-IP-.$n>:9092/",
  "aparser_ui_password": "",
  "server_name": "node-$n",
  "telegram_bot_token": "<TELEGRAM_BOT_TOKEN>",
  "telegram_chat_id": "<TELEGRAM_CHAT_ID>",
  "error_threshold": 0.5, "cooldown_hours": 8, "min_requests": 20,
  "heartbeat_hours": 6, "ui_nav_timeout_ms": 90000, "ui_cards_timeout_ms": 60000,
  "ui_page_change_ms": 10000, "debug": true
}
JSON
  echo "  создан $f — впиши IP/токен/чат"
done

echo ""
echo "ГОТОВО. Что заполнить вручную:"
echo "  - /root/.smbcreds<N>: username/password SMB-доступа"
echo "  - data/nodes/harvest_node.json: LAN-IP узла + путь к кредам"
echo "  - data/nodes/node-<N>.config.json: LAN-IP, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
echo "  Токен/чат Telegram — из BotFather / у владельца."
