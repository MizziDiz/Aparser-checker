@echo off
chcp 65001 >nul
setlocal
rem ============================================================================
rem  install-tasks.bat - регистрация задач планировщика A-Parser-монитора
rem  Запускать ИЗ папки проекта (напр. C:\Monitor), в cmd ОТ АДМИНИСТРАТОРА.
rem ----------------------------------------------------------------------------
rem  РОЛИ И ЧТО ГДЕ ВКЛЮЧЕНО (при ЦЕНТРАЛИЗОВАННОМ контроле)
rem
rem   КОНТРОЛЛЕР (выделенный сервер) - этим батником НЕ настраивается:
rem     мониторинг всех узлов ведёт он сам (cron + --config), хостит шару,
rem     Telegram шлёт напрямую. На Windows-узлах локальный монитор ВЫКЛючен.
rem
rem   РЕЛЕЙ - узел с доступом к Telegram:
rem     [ВКЛ]  aparser-relay     : --relay, ONSTART, долгоживущий - пересылка Telegram
rem     [ВКЛ]  aparser-autosend  : результаты -> шара
rem     [ВКЛ]  aparser-autopilot : авто-создание заданий при простое
rem     [ВЫКЛ] aparser-monitor-ui: мониторинг ведёт контроллер
rem
rem   САБ-СЕРВЕР - обычный узел парсинга:
rem     [ВКЛ]  aparser-autosend, aparser-autopilot
rem     [ВЫКЛ] aparser-monitor-ui: мониторинг ведёт контроллер
rem     Telegram у RU-узлов идёт ЧЕРЕЗ релей: в конфиге telegram_relay_url + relay_secret
rem
rem   АВТОНОМНЫЙ (без централизованного контроля):
rem     [ВКЛ]  aparser-monitor-ui + aparser-autosend + aparser-autopilot
rem
rem  ПЕРЕД ЗАПУСКОМ проверьте конфиг (data\aparser_monitor.config.json), удобно
rem  через aparser-config.exe. Для АВТОПИЛОТА обязательно:
rem     autopilot_create_tasks=true, autopilot_dry_run=false,
rem     autopilot_template_task="точное имя пресета", autopilot_config_preset,
rem     aparser_root="корень A-Parser ЭТОГО узла"  (у каждого узла свой)
rem  Имена пресетов узла:  py aparser_monitor_ui.py --list-presets
rem ============================================================================
cd /d %~dp0
set HERE=%~dp0
if not exist "%HERE%data" mkdir "%HERE%data"

echo.
echo   Выберите роль ЭТОГО сервера:
echo     [1] Саб-сервер [узел парсинга]     : autosend + autopilot
echo     [2] Релей                          : autosend + autopilot + relay
echo     [3] Автономный [без контроллера]   : монитор + autosend + autopilot
echo     [4] Только снять локальный монитор : удалить aparser-monitor-ui
echo.
choice /C 1234 /N /M "   Ваш выбор [1-4]: "
set ROLE=%errorlevel%
echo.

rem под централизацией локальный монитор не нужен (роли 1,2,4) - снимаем, если был
if "%ROLE%"=="3" goto :keep_monitor
echo Снимаю локальный монитор, если был. Его ведёт контроллер.
schtasks /Delete /TN "aparser-monitor-ui" /F >nul 2>&1
:keep_monitor

if "%ROLE%"=="4" goto :done

echo Регистрирую aparser-autosend, каждые 10 мин...
schtasks /Create /TN "aparser-autosend"  /SC MINUTE /MO 10 /TR "\"%HERE%autosend.bat\""  /RU "%USERDOMAIN%\%USERNAME%" /RP * /RL HIGHEST /F

echo Регистрирую aparser-autopilot, каждые 20 мин...
schtasks /Create /TN "aparser-autopilot" /SC MINUTE /MO 20 /TR "\"%HERE%autopilot.bat\"" /RU "%USERDOMAIN%\%USERNAME%" /RP * /RL HIGHEST /F

if not "%ROLE%"=="2" goto :not_relay
echo Регистрирую aparser-relay, ONSTART от SYSTEM, и запускаю...
schtasks /Create /TN "aparser-relay" /SC ONSTART /TR "\"%HERE%relay-run.bat\"" /RU SYSTEM /RL HIGHEST /F
schtasks /Run    /TN "aparser-relay"
:not_relay

if not "%ROLE%"=="3" goto :not_standalone
echo Автономный режим: регистрирую aparser-monitor-ui, каждые 5 мин...
schtasks /Create /TN "aparser-monitor-ui" /SC MINUTE /MO 5 /TR "\"%HERE%monitor.bat\"" /RU "%USERDOMAIN%\%USERNAME%" /RP * /RL HIGHEST /F
:not_standalone

:done
echo.
echo ГОТОВО. Проверка задач:
echo    schtasks /Query /TN "aparser-autosend"  /V /FO LIST ^| findstr /I "Result Run"
echo    schtasks /Query /TN "aparser-autopilot" /V /FO LIST ^| findstr /I "Result Run"
echo Первые логи запуска: data\*.out.log
endlocal
