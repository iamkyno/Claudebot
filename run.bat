@echo off
rem Creates a venv, installs requirements and starts the bot (mode from config.yaml).
cd /d "%~dp0"
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)
call venv\Scripts\activate.bat
pip install --quiet -r requirements.txt
python -m bot.main %*
