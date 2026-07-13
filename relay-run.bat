@echo off
rem Долгоживущий сервер-релей Telegram с автоперезапуском (для RU-узлов, где Telegram
rem заблокирован — они шлют сообщения через этот релей). Ставится ТОЛЬКО на узле-релее,
rem у которого ЕСТЬ доступ к Telegram. Регистрируется install-tasks.bat как задача
rem ONSTART (от SYSTEM). Лог — data\relay.out.log.
rem В конфиге релея: relay_secret, relay_bind (LAN-IP), relay_allowed_ips (подсеть LAN).
cd /d %~dp0
:loop
py aparser_monitor_ui.py --relay >> data\relay.out.log 2>&1
timeout /t 10 /nobreak >nul
goto loop
