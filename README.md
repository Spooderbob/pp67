# StubFlip — Free MLB The Show 26 Stub Bot

Open-source, zero-cost alternative to StubBot.net. Automates card flipping
in the MLB The Show 26 Companion App through ADB so you earn stubs hands-free.

---

## How it works

1. Connects to your Android emulator over ADB
2. Opens the MLB The Show Companion App and navigates to the Marketplace
3. Searches for Diamond Equipment, Live Series, or Sponsorship cards
4. Reads **Buy Now** and **Best Sell** prices using Tesseract OCR
5. If net profit (after 10 % tax) ≥ your threshold → places a buy order
6. Repeats on a configurable cycle

---

## Files

| File | What it does |
|---|---|
| `server.py` | Flask web server — run this to start everything |
| `bot.py` | ADB bot — marketplace navigation + OCR + order logic |
| `calibrate.py` | Takes a screenshot so you can tune coordinates |
| `index.html` | Dashboard UI served at `http://localhost:5000` |
| `config.json` | Settings (created automatically on first run) |
| `setup.bat` | One-click Windows installer |
| `setup.sh` | One-click Linux / macOS installer |

---

## Step-by-step setup

### Step 1 — Install Python 3.11+

Download from https://www.python.org/downloads/  
Windows: check **"Add Python to PATH"** during install.

### Step 2 — Run the setup script

**Windows:**
```
setup.bat
```
**Linux / macOS:**
```bash
chmod +x setup.sh && ./setup.sh
```
This installs `flask`, `pillow`, and `pytesseract`.

### Step 3 — Install Tesseract OCR

**Windows:** Download the installer from  
https://github.com/UB-Mannheim/tesseract/wiki  
Install to `C:\Program Files\Tesseract-OCR` and add that folder to PATH.

**Linux:** (the setup script does this automatically)
```bash
sudo apt install tesseract-ocr
```
**macOS:** (the setup script does this automatically)
```bash
brew install tesseract
```

### Step 4 — Set up LDPlayer 9 (free emulator)

1. Download LDPlayer 9 from https://www.ldplayer.net/ — it's free
2. Create a new instance with these exact settings:
   - Resolution: **1920 × 1080**
   - DPI: **280**
   - Device profile: **Samsung Galaxy S20 FE**
   - Graphics renderer: **Vulkan**
3. In LDPlayer settings → Other settings → **Enable ADB**
4. Install the **MLB The Show Companion App** from the Play Store
5. Log in with your PSN / Xbox account

### Step 5 — Connect ADB

```bash
adb connect 127.0.0.1:5555
adb devices
```

You should see something like:
```
List of devices attached
127.0.0.1:5555   device
```

If nothing appears, open LDPlayer → Settings → Other → ADB, note the port, and use that instead of 5555.

### Step 6 — Calibrate screen coordinates (important!)

Navigate to the **Marketplace** inside the Companion App, then run:

```bash
python calibrate.py      # Windows: python calibrate.py
```

This saves `screen.png`. Open it in Paint or any image editor, hover over
each UI element, and note the X, Y pixel positions. Then open `config.json`
and fill in the `"coords"` section:

```json
{
  "coords": {
    "nav_diamond_dynasty": [960, 1020],
    "nav_marketplace":     [760, 1020],
    "search_bar":          [540, 160],
    "first_card_tap":      [540, 380],
    "buy_now_region":      [1050, 580, 1880, 650],
    "best_sell_region":    [1050, 660, 1880, 730],
    "buy_now_button":      [1460, 840],
    "confirm_button":      [960, 780],
    "price_input":         [960, 550]
  }
}
```

The default values already work for a 1920×1080 / 280 DPI LDPlayer instance.
Only override the ones that are off.

### Step 7 — Start the dashboard

```bash
python server.py
```

Then open **http://localhost:5000** in your browser.

---

## Using the dashboard

| Setting | What it does |
|---|---|
| **Min Profit Margin** | Minimum net stubs per flip after 10 % tax. Start at 500. |
| **Active Hours** | Bot sleeps outside this window (e.g. 8 AM – 11 PM). |
| **Card Types** | Toggle Diamond Equipment, Live Series, Sponsorships. |
| **Max Budget** | Bot won't bid higher than this stub amount. |
| **Delay** | Seconds to wait between flip cycles (minimum 5). |

1. Adjust the settings
2. Click **Save Configuration**
3. Click **Start Bot**
4. Watch the Activity Log and Trade History update live

---

## Troubleshooting

**"No device found"**  
Run `adb devices`. If empty, try `adb connect 127.0.0.1:5555` (or your emulator's port).

**Prices always read as 0**  
The OCR crop boxes don't line up. Run `calibrate.py`, open `screen.png`, and measure the `buy_now_region` and `best_sell_region` boxes more carefully.

**Bot taps the wrong spots**  
Re-run `calibrate.py` with the Marketplace open and re-measure the tap coordinates.

**"adb not found"**  
Install Android SDK Platform Tools:  
https://developer.android.com/tools/releases/platform-tools  
Unzip and add the folder to your PATH.

**"tesseract not found"**  
Add Tesseract to PATH (see Step 3) and restart your terminal.

---

## Disclaimer

This tool automates a video-game marketplace. Use it responsibly and at your
own risk. Automation may violate the game's terms of service.

---

## License

MIT — free to use, modify, and share.
