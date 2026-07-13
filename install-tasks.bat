@echo off
setlocal
rem ============================================================================
rem  install-tasks.bat — регистрация задач планировщика A-Parser-монитора
rem  Запускать ИЗ папки проекта (напр. C:\Monitor), в cmd ОТ АДМИНИСТРАТОРА.
rem ----------------------------------------------------------------------------
rem  РОЛИ И ЧТО ГДЕ ВКЛЮЧЕНО (при ЦЕНТРАЛИЗОВАННОМ контроле с контроллера .50)
rem
rem   КОНТРОЛЛЕР (выделенный сервер) — этим батником НЕ настраивается:
rem     мониторинг всех узлов ведёт он сам (cron + --config), хостит шару,
rem     Telegram шлёт напрямую. На Windows-узлах локальный монитор ВЫКЛючен.
rem
rem   РЕЛЕЙ — узел с доступом к Telegram:
rem     [ВКЛ]  aparser-relay     (--relay, ONSTART, долгоживущий) — пересылка Telegram
rem     [ВКЛ]  aparser-autosend  (--autosend)   — результаты -> шара
rem     [ВКЛ]  aparser-autopilot (--autopilot)  — авто-создание заданий при простое
rem     [ВЫКЛ] aparser-monitor-ui — мониторинг ведёт контроллер (иначе двойные «завершено»)
rem
rem   САБ-СЕРВЕР — обычный узел парсинга:
rem     [ВКЛ]  aparser-autosend, aparser-autopilot
rem     [ВЫКЛ] aparser-monitor-ui — мониторинг ведёт контроллер
rem     Telegram у RU-узлов идёт ЧЕРЕЗ релей: в конфиге telegram_relay_url + relay_secret
rem
rem   АВТОНОМНЫЙ (без централизованного контроля):
rem     [ВКЛ]  aparser-monitor-ui (--) + aparser-autosend + aparser-autopilot
rem            (+ aparser-relay, если этот же узел — релей)
rem
rem  ПЕРЕД ЗАПУСКОМ проверьте конфиг (data\aparser_monitor.config.json), удобно
rem  через aparser-config.exe. Для АВТОПИЛОТА обязательно:
rem     autopilot_create_tasks=true, autopilot_dry_run=false,
rem     autopilot_template_task="<точное имя пресета, напр. Aparser yahoo>",
rem     autopilot_config_preset="200t",
rem     aparser_root="<корень A-Parser ЭТОГО узла>"  (у каждого узла свой!)
rem  Имена пресетов узла можно посмотреть:  py aparser_monitor_ui.py --list-presets
rem ============================================================================
cd /d %~dp0
set HERE=%~dp0
if not exist "%HERE%data" mkdir "%HERE%data"

echo.
echo   Выберите роль ЭТОГО сервера:
echo     [1] Саб-сервер (узел парсинга)     : autosend + autopilot
echo     [2] Релей                          : autosend + autopilot + relay(автозапуск)
echo     [3] Автономный (без контроллера)    : монитор + autosend + autopilot
echo     [4] Только снять локальный монитор  : удалить aparser-monitor-ui
echo.
choice /C 1234 /N /M "   Ваш выбор [1-4]: "
set ROLE=%errorlevel%
echo.

rem --- при централизованном контроле локальный монитор не нужен: снимаем, если был ---
if not "%ROLE%"=="3" (
  echo Снимаю локальный монитор (если был) — его ведёт контроллер...
  schtasks /Delete /TN "aparser-monitor-ui" /F >nul 2>&1
)
if "%ROLE%"=="4" goto :done

rem --- autosend (роли 1-3): каждые 10 минут, ОТ ПОЛЬЗОВАТЕЛЯ (доступ к шаре по cmdkey) ---
echo Регистрирую aparser-autosend (каждые 10 мин)...
schtasks /Create /TN "aparser-autosend"  /SC MINUTE /MO 10 /TR "\"%HERE%autosend.bat\""  /RU "%USERDOMAIN%\%USERNAME%" /RP * /RL HIGHEST /F

rem --- autopilot (роли 1-3): каждые 20 минут, от пользователя ---
echo Регистрирую aparser-autopilot (каждые 20 мин)...
schtasks /Create /TN "aparser-autopilot" /SC MINUTE /MO 20 /TR "\"%HERE%autopilot.bat\"" /RU "%USERDOMAIN%\%USERNAME%" /RP * /RL HIGHEST /F

if "%ROLE%"=="2" (
  echo Регистрирую aparser-relay (ONSTART, от SYSTEM) и запускаю...
  schtasks /Create /TN "aparser-relay" /SC ONSTART /TR "\"%HERE%relay-run.bat\"" /RU SYSTEM /RL HIGHEST /F
  schtasks /Run    /TN "aparser-relay"
)

if "%ROLE%"=="3" (
  echo Автономный режим: регистрирую aparser-monitor-ui (каждые 5 мин)...
  schtasks /Create /TN "aparser-monitor-ui" /SC MINUTE /MO 5 /TR "\"%HERE%monitor.bat\"" /RU "%USERDOMAIN%\%USERNAME%" /RP * /RL HIGHEST /F
)

:done
echo.
echo ГОТОВО. Проверка задач:
echo    schtasks /Query /TN "aparser-autosend"  /V /FO LIST ^| findstr /I "Result Run"
echo    schtasks /Query /TN "aparser-autopilot" /V /FO LIST ^| findstr /I "Result Run"
echo Первые логи запуска — в data\*.out.log
endlocal
