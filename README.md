# StubFlip — Free MLB The Show 26 Stub Bot

A free, open-source alternative to StubBot.net. Automates card flipping in the
MLB The Show 26 Companion App via ADB so you earn stubs without touching the
controller.

---

## How it works

1. Connects to your Android emulator over ADB
2. Opens the MLB The Show Companion App and navigates to the Marketplace
3. Searches for Diamond Equipment, Live Series, and Sponsorship cards
4. Reads the **Buy Now** and **Best Sell** prices using OCR (Tesseract)
5. When the net margin (after 10 % tax) exceeds your threshold, places a buy order
6. Repeats on a configurable cycle until you stop it

---

## Requirements

| Requirement | Notes |
|---|---|
| Windows 10/11 or Linux | macOS works too |
| Python 3.11+ | `python --version` |
| Android emulator | LDPlayer 9, BlueStacks 5, or NoxPlayer |
| Emulator settings | 1920×1080, 280 DPI, Samsung Galaxy S20 FE preset, Vulkan |
| ADB | Part of Android SDK Platform Tools — add to PATH |
| Tesseract OCR | Install from https://github.com/UB-Mannheim/tesseract/wiki |
| MLB The Show Companion App | Installed and logged in on the emulator |

---

## Setup

### 1 — Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2 — Install Tesseract OCR

**Windows:** Download the installer from UB-Mannheim, install to `C:\Program Files\Tesseract-OCR`, add that folder to PATH.

**Linux:**
```bash
sudo apt install tesseract-ocr
```

**macOS:**
```bash
brew install tesseract
```

### 3 — Set up your emulator

- Install LDPlayer 9 (free, fast, good ADB support)
- Create a new instance: 1920×1080, 280 DPI, Samsung Galaxy S20 FE, Vulkan graphics
- Enable ADB: Settings → Other settings → Enable ADB
- Install the MLB The Show Companion App, log in, and open it to the Marketplace

### 4 — Connect ADB

```bash
# LDPlayer default port
adb connect 127.0.0.1:5555

# Verify the device shows up
adb devices
```

### 5 — Start the dashboard

```bash
python server.py
```

Open **http://localhost:5000** in your browser.

---

## Using the dashboard

1. Adjust **Min Profit Margin** — minimum stubs profit per flip after tax (default 500)
2. Set **Active Hours** — the bot sleeps outside this window
3. Toggle **Card Types** — choose which card categories to flip
4. Set **Max Budget** — bot won't bid above this amount
5. Set **Delay** — seconds between flip cycles (minimum 5)
6. Click **Save Configuration**, then **Start Bot**

The **Activity Log** and **Trade History** update every 3 seconds.

---

## Coordinate calibration

Screen tap coordinates are tuned for 1920×1080 / 280 DPI. If the bot taps the
wrong spots, override them in `config.json` under the `"coords"` key:

```json
{
  "coords": {
    "nav_diamond_dynasty": [960, 1020],
    "first_card_tap":      [540, 380],
    "buy_now_region":      [1050, 580, 1880, 650]
  }
}
```

Run your emulator, take a screenshot with `adb exec-out screencap -p > screen.png`,
open it in an image editor, and note the pixel coordinates of each UI element.

---

## FAQ

**Will this get my account banned?**
All automation carries risk. The bot includes randomised delays to reduce
detection, but use it at your own discretion.

**The bot says "No device found".**
Make sure `adb devices` lists your emulator. Run `adb connect 127.0.0.1:5555`
(LDPlayer) or check your emulator's ADB port in its settings.

**Prices are always 0.**
Tesseract isn't reading the screen correctly. Take a screenshot, check the
`*_region` coordinates, and adjust them in `config.json`.

**Can I run this on a real phone?**
Yes — enable USB debugging, connect via USB, and `adb devices` should detect it.
Coordinate calibration will differ from emulator defaults.

---

## License

MIT — free to use, modify, and distribute.
