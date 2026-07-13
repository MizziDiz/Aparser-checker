@echo off
rem Отправка готовых результатов A-Parser на шару (без UI-мониторинга).
rem Регистрируется install-tasks.bat как задача каждые ~10 минут (от пользователя,
rem чтобы работал доступ к шаре по cmdkey). Лог запуска — data\autosend.out.log.
cd /d %~dp0
py aparser_monitor_ui.py --autosend >> data\autosend.out.log 2>&1
