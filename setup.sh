#!/bin/bash
# StubFlip setup for Linux / macOS

set -e

echo "============================================"
echo "  StubFlip Setup"
echo "============================================"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found."
    echo "Install Python 3.11+ first:"
    echo "  Linux:  sudo apt install python3 python3-pip"
    echo "  macOS:  brew install python"
    exit 1
fi

echo "Installing Python dependencies…"
python3 -m pip install --upgrade pip
python3 -m pip install flask pillow pytesseract

echo ""
echo "Installing Tesseract OCR…"
if command -v apt-get &>/dev/null; then
    sudo apt-get install -y tesseract-ocr
elif command -v brew &>/dev/null; then
    brew install tesseract
else
    echo "  Could not auto-install Tesseract."
    echo "  Install it manually: https://tesseract-ocr.github.io/tessdoc/Installation.html"
fi

echo ""
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Install an Android emulator (LDPlayer 9 or BlueStacks 5)"
echo "     Set: 1920x1080, 280 DPI, Samsung Galaxy S20 FE, Vulkan, ADB on"
echo ""
echo "  2. Connect ADB:"
echo "     adb connect 127.0.0.1:5555"
echo "     adb devices   # should show 'device'"
echo ""
echo "  3. Install MLB The Show Companion App on the emulator,"
echo "     log in, and open the Marketplace."
echo ""
echo "  4. Calibrate screen coordinates:"
echo "     python3 calibrate.py"
echo ""
echo "  5. Start the dashboard:"
echo "     python3 server.py"
echo "     Then open http://localhost:5000"
