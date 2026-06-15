@echo off
chcp 65001 >nul
cd /d "%~dp0"

py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python 3.12 не найден
    pause
    exit /b 1
)

py -3.12 -m pip install --upgrade pip

py -3.12 -m pip install --no-deps opentele
py -3.12 -m pip install pyqt5
py -3.12 -m pip install TgCrypto-pyrofork
py -3.12 -m pip install telethon python-dotenv "python-socks[asyncio]" customtkinter openai groq "httpx[socks]" pytest pytest-asyncio Pillow fpdf2

py -3.12 -c "from opentele.td import TDesktop; from opentele.td.storage import Storage; print('[OK] opentele полностью работает')"
if errorlevel 1 (
    echo [!] opentele всё ещё не импортируется
    pause
    exit /b 1
)

echo.
echo Готово. Запускай run_gui.bat
pause
