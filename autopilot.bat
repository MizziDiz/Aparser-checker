@echo off
rem Автопилот: при простое узла создаёт задание под готовый батч (или запускает кейген).
rem Работает локально против своего A-Parser (127.0.0.1). Требует в конфиге:
rem   autopilot_create_tasks=true, autopilot_dry_run=false,
rem   autopilot_template_task="<точное имя пресета>", autopilot_config_preset="200t",
rem   aparser_root="<корень A-Parser этого узла>".
rem Регистрируется install-tasks.bat каждые ~20 минут. Лог — data\autopilot.out.log.
cd /d %~dp0
py aparser_monitor_ui.py --autopilot >> data\autopilot.out.log 2>&1
