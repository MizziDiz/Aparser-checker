@echo off
rem ============================================================================
rem  build-config-exe.bat — сборка GUI-редактора настроек в один .exe (Windows)
rem ----------------------------------------------------------------------------
rem  ЧТО ДЕЛАЕТ
rem    Собирает aparser_config_gui.py в один файл aparser-config.exe.
rem    Exe кладётся в КОРЕНЬ проекта; промежуточные файлы сборки — в build\.
rem
rem  КАК EXE РАБОТАЕТ С КОНФИГОМ (важно)
rem    Запущенный aparser-config.exe читает и сохраняет
rem        <папка рядом с exe>\data\aparser_monitor.config.json
rem    (frozen-путь = каталог самого exe). То есть:
rem      - положите aparser-config.exe в C:\Monitor (рядом с папкой data\);
rem      - при старте форма ОТКРОЕТСЯ УЖЕ ЗАПОЛНЕННОЙ текущими значениями;
rem      - правите поля и жмёте «Сохранить» — пишется тот же config.json.
rem    Ничего вручную указывать не нужно — существующий конфиг подхватывается сам.
rem
rem  ТРЕБОВАНИЯ
rem      - Python 3.10+ с лончером py     (проверка:  py --version)
rem      - интернет для pip              (ставит pyinstaller)
rem      - tkinter в составе Python      (обычно есть; иначе переставить Python с Tcl/Tk)
rem
rem  ЗАПУСК: из корня проекта (двойной клик или из cmd).
rem ============================================================================
cd /d %~dp0

echo [1/2] Установка/обновление PyInstaller...
py -m pip install --upgrade pyinstaller || goto :err

echo [2/2] Сборка aparser-config.exe...
py -m PyInstaller --onefile --windowed --name aparser-config ^
  --distpath . --workpath build --specpath build ^
  --hidden-import aparser_monitor --hidden-import requests ^
  --hidden-import lib.config_schema --hidden-import lib.relay ^
  aparser_config_gui.py || goto :err

echo.
echo ГОТОВО: aparser-config.exe (в этой папке). build\ можно удалить.
echo Положите exe рядом с data\ (напр. в C:\Monitor) — откроет текущий конфиг.
goto :eof

:err
echo.
echo ОШИБКА сборки. Проверьте: py --version, интернет для pip, наличие tkinter.
exit /b 1
