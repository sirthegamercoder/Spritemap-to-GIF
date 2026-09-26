@echo off
echo Install requirements

python -m venv .venv
call .venv\Scripts\activate.bat
pip install -r src\requirements.txt
echo.
echo Requirements has installed!
exit /b