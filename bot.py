#!/usr/bin/env python3
"""
StubFlip Bot — Free MLB The Show 26 stub-farming via ADB.

Requirements
------------
* Android emulator (LDPlayer 9 recommended) at 1920x1080 / 280 DPI
* MLB The Show Companion App installed and logged in on the emulator
* adb in PATH  (Android SDK Platform Tools)
* tesseract in PATH  (for OCR price reading)
* pip install flask pillow pytesseract

Run calibrate.py once to capture a screenshot and tune the coordinate
regions in config.json before the first real run.
"""

import io
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# PIL is required; pytesseract is required for OCR
try:
    from PIL import Image, ImageEnhance, ImageFilter
    PIL_OK = True
except ImportError:
    PIL_OK = False

try:
    import pytesseract
    TESSERACT_OK = True
except ImportError:
    TESSERACT_OK = False

# ---------------------------------------------------------------------------
# Default screen-coordinate map
# Calibrated for 1920×1080 / 280 DPI / Samsung Galaxy S20 FE preset in
# LDPlayer 9.  Override any key in config.json under "coords".
# ---------------------------------------------------------------------------
DEFAULT_COORDS: Dict[str, Tuple] = {
    # Bottom nav bar of the Companion App
    "nav_diamond_dynasty": (960, 1020),
    "nav_marketplace":     (760, 1020),

    # Marketplace toolbar
    "search_bar":          (540, 160),

    # First search-result card
    "first_card_tap":      (540, 380),

    # Detail screen — OCR crop boxes  (x1, y1, x2, y2)
    "buy_now_region":      (1050, 580, 1880, 650),
    "best_sell_region":    (1050, 660, 1880, 730),

    # Detail screen action buttons
    "buy_now_button":      (1460, 840),

    # Confirm / OK overlay button
    "confirm_button":      (960, 780),

    # Price input field when placing an order
    "price_input":         (960, 550),
}

# MLB The Show 26 Companion App package/activity
# Update this if Sony changes the package name.
APP_PACKAGE  = "com.scea.mlbts.companion"
APP_ACTIVITY = ".ui.MainActivity"

# DD marketplace sales tax
SALES_TAX = 0.10

# Emulator ADB ports to try (LDPlayer, BlueStacks, NoxPlayer, MEmu)
EMULATOR_PORTS = (5555, 5554, 21503, 62001, 7555)


class ADBError(Exception):
    pass


class StubBot:
    def __init__(self, config: dict, stats: dict):
        self.config  = config
        self.stats   = stats
        self.running = False
        self.device: Optional[str] = None

        # Merge caller-supplied coordinate overrides
        raw = {**DEFAULT_COORDS}
        for k, v in config.get("coords", {}).items():
            raw[k] = tuple(v) if isinstance(v, list) else v
        self.coords = raw

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log(self, msg: str, level: str = "info"):
        entry = {
            "time":  datetime.now().strftime("%H:%M:%S"),
            "msg":   msg,
            "level": level,
        }
        self.stats["log"].insert(0, entry)
        self.stats["log"] = self.stats["log"][:200]
        print(f"[{entry['time']}] {msg}")

    # ------------------------------------------------------------------
    # ADB primitives
    # ------------------------------------------------------------------

    def _adb(self, *args: str, timeout: int = 15) -> str:
        cmd = ["adb"]
        if self.device:
            cmd += ["-s", self.device]
        cmd += list(args)
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
            return r.stdout.strip()
        except subprocess.TimeoutExpired:
            raise ADBError(f"ADB timed out: {' '.join(args)}")
        except FileNotFoundError:
            raise ADBError(
                "adb not found — install Android SDK Platform Tools and add to PATH"
            )

    def connect(self) -> bool:
        """Attach to a running emulator or USB-connected device."""
        out = self._adb("devices")
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                self.device = parts[0]
                self.log(f"Connected to {self.device}")
                return True

        # Auto-connect to common emulator TCP ports
        for port in EMULATOR_PORTS:
            try:
                self._adb("connect", f"127.0.0.1:{port}", timeout=5)
                out2 = self._adb("devices")
                for line in out2.splitlines()[1:]:
                    if f"127.0.0.1:{port}" in line and "device" in line:
                        self.device = f"127.0.0.1:{port}"
                        self.log(f"Connected to emulator on port {port}")
                        return True
            except Exception:
                pass

        self.log(
            "No device found. Start your emulator and enable ADB, "
            "then try: adb connect 127.0.0.1:5555",
            "error",
        )
        return False

    # ------------------------------------------------------------------
    # Screen interaction
    # ------------------------------------------------------------------

    def screenshot(self) -> Optional["Image.Image"]:
        if not PIL_OK:
            self.log("Pillow not installed — run: pip install pillow", "error")
            return None
        try:
            raw = subprocess.run(
                ["adb", "-s", self.device, "exec-out", "screencap", "-p"],
                capture_output=True,
                timeout=15,
            )
            return Image.open(io.BytesIO(raw.stdout))
        except Exception as exc:
            self.log(f"Screenshot failed: {exc}", "error")
            return None

    def tap(self, x: int, y: int, delay: float = 0.6):
        self._adb("shell", "input", "tap", str(x), str(y))
        time.sleep(delay)

    def tap_coord(self, name: str, delay: float = 0.6):
        c = self.coords[name]
        self.tap(int(c[0]), int(c[1]), delay)

    def type_text(self, text: str):
        safe = text.replace(" ", "%s").replace("'", "")
        self._adb("shell", "input", "text", safe)
        time.sleep(0.4)

    def clear_field(self):
        self._adb("shell", "input", "keyevent", "KEYCODE_CTRL_A")
        time.sleep(0.1)
        self._adb("shell", "input", "keyevent", "KEYCODE_DEL")
        time.sleep(0.3)

    def press_back(self):
        self._adb("shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(0.5)

    def launch_app(self):
        self._adb("shell", "am", "start", "-n",
                  f"{APP_PACKAGE}/{APP_ACTIVITY}")
        time.sleep(4)

    # ------------------------------------------------------------------
    # OCR price reader
    # ------------------------------------------------------------------

    def _read_price_from_region(
        self, img: "Image.Image", region: Tuple
    ) -> int:
        if not TESSERACT_OK:
            return 0
        crop = img.crop(region)
        # 3× upscale + high contrast improves OCR on small stub numbers
        crop = crop.resize(
            (crop.width * 3, crop.height * 3), Image.LANCZOS
        )
        crop = crop.convert("L")
        crop = ImageEnhance.Contrast(crop).enhance(3.0)
        crop = crop.filter(ImageFilter.SHARPEN)
        raw = pytesseract.image_to_string(
            crop,
            config="--psm 7 -c tessedit_char_whitelist=0123456789,",
        )
        digits = re.sub(r"[^0-9]", "", raw)
        return int(digits) if digits else 0

    def read_price(self, img: "Image.Image", coord_key: str) -> int:
        return self._read_price_from_region(img, self.coords[coord_key])

    # ------------------------------------------------------------------
    # Marketplace navigation
    # ------------------------------------------------------------------

    def navigate_to_marketplace(self):
        self.log("Launching Companion App…")
        self.launch_app()
        self.log("Tapping Diamond Dynasty tab…")
        self.tap_coord("nav_diamond_dynasty")
        time.sleep(1.5)
        self.log("Tapping Marketplace tab…")
        self.tap_coord("nav_marketplace")
        time.sleep(2.5)
        self.log("Marketplace ready.")

    def search_for(self, term: str):
        self.tap_coord("search_bar")
        time.sleep(0.4)
        self.clear_field()
        self.type_text(term)
        self._adb("shell", "input", "keyevent", "KEYCODE_ENTER")
        time.sleep(2.5)

    # ------------------------------------------------------------------
    # Price inspection
    # ------------------------------------------------------------------

    def get_prices(self) -> Tuple[int, int]:
        """Open first listing and read buy-now + best-sell prices."""
        self.tap_coord("first_card_tap")
        time.sleep(1.5)
        img = self.screenshot()
        if img is None:
            return 0, 0
        buy_now   = self.read_price(img, "buy_now_region")
        best_sell = self.read_price(img, "best_sell_region")
        self.press_back()
        time.sleep(1)
        return buy_now, best_sell

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_buy_order(self, price: int):
        """Open first listing → Buy Now → set price → confirm."""
        self.tap_coord("first_card_tap")
        time.sleep(1.5)
        self.tap_coord("buy_now_button")
        time.sleep(1)
        self.tap_coord("price_input")
        time.sleep(0.3)
        self.clear_field()
        self.type_text(str(price))
        time.sleep(0.3)
        self.tap_coord("confirm_button")
        time.sleep(1.5)
        self.tap_coord("confirm_button")   # second OK if needed
        time.sleep(1)
        self.press_back()
        time.sleep(1)

    # ------------------------------------------------------------------
    # Flip logic
    # ------------------------------------------------------------------

    CARD_SEARCH_TERMS: Dict[str, str] = {
        "diamondEquipment": "Diamond Equipment",
        "liveSeries":       "Live Series",
        "sponsorships":     "Sponsorship",
    }

    def attempt_flip(self, type_key: str) -> bool:
        label = self.CARD_SEARCH_TERMS[type_key]
        self.search_for(label)

        buy_now, best_sell = self.get_prices()

        if buy_now == 0 or best_sell == 0:
            self.log(f"[{label}] Could not read prices — skipping", "warn")
            return False

        tax        = int(buy_now * SALES_TAX)
        net_margin = buy_now - best_sell - tax

        self.log(
            f"[{label}] BuyNow={buy_now:,}  BestSell={best_sell:,}  "
            f"Net={net_margin:,}  Tax={tax:,}"
        )

        threshold = int(self.config.get("profitMargin", 500))
        if net_margin < threshold:
            self.log(
                f"[{label}] Margin {net_margin:,} < threshold {threshold:,} — skip"
            )
            return False

        budget = int(self.config.get("maxBudget", 100_000))
        bid    = best_sell + 1
        if bid > budget:
            self.log(f"[{label}] Bid {bid:,} exceeds budget {budget:,} — skip")
            return False

        self.log(f"[{label}] Placing buy order at {bid:,} stubs…")
        self.place_buy_order(bid)

        self.stats["tradesCompleted"] += 1
        self.stats["stubsEarned"]     += net_margin

        entry = {
            "time":      datetime.now().strftime("%H:%M:%S"),
            "type":      label,
            "buyNow":    buy_now,
            "bestSell":  best_sell,
            "bid":       bid,
            "netProfit": net_margin,
        }
        trade_history: List[dict] = self.stats.get("tradeHistory", [])
        trade_history.insert(0, entry)
        self.stats["tradeHistory"] = trade_history[:50]

        self.log(f"[{label}] Order placed! Est. profit {net_margin:,} stubs")
        return True

    # ------------------------------------------------------------------
    # Active-hours guard
    # ------------------------------------------------------------------

    def in_active_window(self) -> bool:
        hour  = datetime.now().hour
        start = int(self.config.get("activeHoursStart", 0))
        end   = int(self.config.get("activeHoursEnd", 24))
        return start <= hour < end

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        self.running = True

        if not self.connect():
            self.stats["running"] = False
            self.stats["status"]  = "error"
            return

        self.navigate_to_marketplace()

        card_types  = self.config.get("cardTypes", {})
        active_keys = [k for k, v in card_types.items() if v]

        if not active_keys:
            self.log(
                "No card types enabled — enable at least one in Configuration.",
                "warn",
            )
            self.stats["running"] = False
            return

        delay = max(5, int(self.config.get("delayBetweenTrades", 30)))

        while self.running:
            if not self.in_active_window():
                self.log("Outside active hours — sleeping 5 min…")
                for _ in range(300):
                    if not self.running:
                        break
                    time.sleep(1)
                continue

            for key in active_keys:
                if not self.running:
                    break
                try:
                    self.attempt_flip(key)
                except ADBError as exc:
                    self.log(f"ADB error: {exc} — retrying in 15s", "error")
                    time.sleep(15)
                except Exception as exc:
                    self.log(f"Unexpected error: {exc}", "error")
                time.sleep(3)

            if self.running:
                self.log(f"Cycle complete. Next in {delay}s…")
                for _ in range(delay):
                    if not self.running:
                        break
                    time.sleep(1)

        self.log("Bot stopped.")

    def stop(self):
        self.running = False
