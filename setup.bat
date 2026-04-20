@echo off
echo ============================================
echo   StubFlip Setup
echo ============================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found.
    echo Download Python 3.11+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo Installing Python dependencies...
python -m pip install --upgrade pip
python -m pip install flask pillow pytesseract

echo.
echo ============================================
echo   Setup complete!
echo ============================================
echo.
echo Next steps:
echo   1. Install Tesseract OCR:
echo      https://github.com/UB-Mannheim/tesseract/wiki
echo      Default install path: C:\Program Files\Tesseract-OCR
echo      Add that folder to your PATH environment variable.
echo.
echo   2. Install LDPlayer 9 (free):
echo      https://www.ldplayer.net/
echo      Settings: 1920x1080 resolution, 280 DPI,
echo      Samsung Galaxy S20 FE, Vulkan graphics, ADB enabled.
echo.
echo   3. Connect ADB:
echo      adb connect 127.0.0.1:5555
echo      adb devices  (should show 'device')
echo.
echo   4. Install MLB The Show Companion App on the emulator,
echo      log in, and navigate to the Marketplace.
echo.
echo   5. Run calibrate.py to take a screenshot for coordinate setup:
echo      python calibrate.py
echo.
echo   6. Start the bot dashboard:
echo      python server.py
echo      Then open http://localhost:5000
echo.
pause
