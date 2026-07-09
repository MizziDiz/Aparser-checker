@echo off
rem Сборка редактора настроек в один .exe (Windows). Запускать из корня проекта.
rem Готовый aparser-config.exe кладётся в корень; промежуточные файлы — в build\.
cd /d %~dp0
py -m pip install --upgrade pyinstaller
py -m PyInstaller --onefile --windowed --name aparser-config ^
  --distpath . --workpath build --specpath build ^
  --hidden-import aparser_monitor --hidden-import requests ^
  --hidden-import lib.config_schema --hidden-import lib.relay ^
  aparser_config_gui.py
echo.
echo Готово. Файл: aparser-config.exe (в этой же папке)
