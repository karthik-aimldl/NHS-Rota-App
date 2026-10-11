@echo off
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3.10+ not found. Install from python.org and tick Add Python to PATH.& pause & exit /b 1)
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || (echo Dependency installation failed.& pause & exit /b 1)
python app.py
pause
