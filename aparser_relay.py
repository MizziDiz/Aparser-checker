#!/usr/bin/env python3
"""
aparser_relay.py — сервер-релей для отправки сообщений в Telegram.

Запускается на сервере локальной сети, у которого ЕСТЬ доступ к Telegram
(`py aparser_monitor_ui.py --relay` или `py aparser_monitor.py --relay`).
Принимает по HTTP сообщения от других серверов (у которых Telegram заблокирован)
и пересылает их в Telegram напрямую, используя свой токен/chat_id.

Протокол: POST http://<релей>:<relay_port>/send
    тело JSON: {"secret": "<relay_secret>", "text": "<текст сообщения>"}
    ответ:     {"ok": true} либо {"ok": false, "error": "..."}

На клиентах в конфиге задаётся telegram_relay_url (напр. http://192.168.1.5:8899)
и тот же relay_secret. Токен бота на клиентах не нужен — он только на релее.

Это долгоживущий процесс: запускайте его как автозагрузку/службу (см. README),
а не разовой задачей планировщика.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aparser_monitor import send_telegram_direct


def run_relay(cfg: dict, logger: logging.Logger) -> int:
    port = int(cfg.get("relay_port", 8899) or 8899)
    bind = cfg.get("relay_bind", "0.0.0.0") or "0.0.0.0"
    secret = cfg.get("relay_secret", "")

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path.rstrip("/") != "/send":
                self._reply(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._reply(400, {"ok": False, "error": "bad json"})
                return
            if secret and data.get("secret", "") != secret:
                logger.warning(f"relay: отклонён запрос с неверным секретом от {self.client_address[0]}")
                self._reply(403, {"ok": False, "error": "bad secret"})
                return
            text = str(data.get("text", "")).strip()
            if not text:
                self._reply(400, {"ok": False, "error": "empty text"})
                return
            try:
                send_telegram_direct(cfg, text)
                logger.info(f"relay: переслано в Telegram от {self.client_address[0]}")
                self._reply(200, {"ok": True})
            except Exception as e:
                logger.error(f"relay: не удалось переслать: {e}")
                self._reply(502, {"ok": False, "error": str(e)})

        def log_message(self, *args):
            pass  # тишина в stderr — своё логирование выше

    srv = ThreadingHTTPServer((bind, port), Handler)
    logger.info(f"relay слушает {bind}:{port} (POST /send); отправляет от chat_id={cfg.get('telegram_chat_id')}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        logger.info("relay остановлен")
    finally:
        srv.server_close()
    return 0
