@echo off
rem Сборка редактора настроек в один .exe (Windows). Запускать из папки скрипта.
cd /d %~dp0
py -m pip install --upgrade pyinstaller
py -m PyInstaller --onefile --windowed --name aparser-config ^
  --hidden-import aparser_monitor --hidden-import requests ^
  aparser_config_gui.py
echo.
echo Готово. Файл: dist\aparser-config.exe
echo Положите aparser-config.exe рядом с aparser_monitor_ui.py (напр. в C:\Monitor)
