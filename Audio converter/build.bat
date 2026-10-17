@echo off
echo Building Audio Converter...
python -m PyInstaller --noconsole --onefile --name "Audio Converter" --add-data "templates;templates" app.py
echo.
echo Done. Your app is in the dist folder.
pause
