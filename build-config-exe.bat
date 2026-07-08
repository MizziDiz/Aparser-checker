@echo off
rem Сборка редактора настроек в один .exe (Windows). Запускать из корня проекта.
rem Артефакты (spec, промежуточные) складываются в build\, готовый exe — в dist\.
cd /d %~dp0
py -m pip install --upgrade pyinstaller
py -m PyInstaller --onefile --windowed --name aparser-config ^
  --distpath dist --workpath build --specpath build ^
  --hidden-import aparser_monitor --hidden-import requests ^
  --hidden-import lib.config_schema ^
  aparser_config_gui.py
echo.
echo Готово. Файл: dist\aparser-config.exe
echo Положите aparser-config.exe рядом с aparser_monitor_ui.py (напр. в C:\Monitor)
