#!/usr/bin/env python3
"""
StubFlip Bot - Free MLB The Show 26 stub farming via ADB automation.

Setup requirements:
  1. Android emulator (LDPlayer / BlueStacks / NoxPlayer) running at 1920x1080 280 DPI
  2. MLB The Show Companion App installed and logged in
  3. ADB enabled on the emulator (usually port 5555)
  4. `adb devices` should list your emulator

The bot navigates the Companion App marketplace, reads buy-now and best-sell
prices with OCR, and executes flips when the margin exceeds your threshold.
"""

import io
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image, ImageEnhance, ImageFilter
    import pytesseract
    TESSERACT_OK = True
except ImportError:
    TESSERACT_OK = False


# ---------------------------------------------------------------------------
# Screen coordinate map (1920 × 1080, 280 DPI, Samsung Galaxy S20 FE preset)
# Override these in config.json under "coords" if your layout differs.
# ---------------------------------------------------------------------------
DEFAULT_COORDS = {
    # Bottom nav bar
    "nav_diamond_dynasty": (960, 1020),
    "nav_marketplace":     (760, 1020),

    # Marketplace toolbar
    "search_bar":          (540, 160),
    "filter_button":       (1800, 160),
    "sort_button":         (1650, 160),

    # Listing card regions (first result row)
    "first_card_tap":      (540, 380),

    # Detail screen price regions (x1,y1,x2,y2 crop boxes)
    "buy_now_region":      (1050, 580, 1880, 650),
    "best_sell_region":    (1050, 660, 1880, 730),

    # Detail screen action buttons
    "buy_now_button":      (1460, 840),
    "sell_button":         (1460, 920),

    # Confirm / OK overlays
    "confirm_button":      (960, 780),
    "ok_button":           (960, 780),

    # Price input field (when listing for sale)
    "price_input":         (960, 550),

    # Back / close
    "back_button":         (60, 60),
    "close_overlay":       (960, 200),
}

# MLB The Show 26 Companion App package (update if the app store changes it)
APP_PACKAGE = "com.scea.mlbts.companion"
APP_ACTIVITY = ".ui.MainActivity"

# Diamond Dynasty takes a 10 % sales tax on stubs
SALES_TAX = 0.10


class ADBError(Exception):
    pass


class StubBot:
    def __init__(self, config: dict, stats: dict):
        self.config = config
        self.stats = stats
        self.running = False
        self.device: str | None = None

        # Allow coords overrides in config
        self.coords = {**DEFAULT_COORDS, **config.get("coords", {})}

        # Trade history kept in memory
        self.trade_log: list[dict] = []

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log(self, msg: str, level: str = "info"):
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "msg": msg,
            "level": level,
        }
        self.stats["log"].insert(0, entry)
        self.stats["log"] = self.stats["log"][:200]
        print(f"[{entry['time']}] {msg}")

    # ------------------------------------------------------------------
    # ADB primitives
    # ------------------------------------------------------------------

    def _run(self, *args, timeout: int = 15) -> str:
        cmd = ["adb"]
        if self.device:
            cmd += ["-s", self.device]
        cmd += list(args)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.stdout.strip()
        except subprocess.TimeoutExpired:
            raise ADBError(f"ADB command timed out: {' '.join(args)}")
        except FileNotFoundError:
            raise ADBError(
                "adb not found. Install Android SDK Platform Tools and add to PATH."
            )

    def connect(self) -> bool:
        """Find and attach to a running emulator or USB device."""
        out = self._run("devices")
        lines = [l for l in out.splitlines()[1:] if l.strip()]
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                self.device = parts[0]
                self.log(f"Connected to {self.device}")
                return True

        # Try explicit LDPlayer / BlueStacks default ports
        for port in (5555, 5554, 21503, 62001):
            try:
                self._run("connect", f"127.0.0.1:{port}", timeout=5)
                out = self._run("devices")
                for line in out.splitlines()[1:]:
                    if f"127.0.0.1:{port}" in line and "device" in line:
                        self.device = f"127.0.0.1:{port}"
                        self.log(f"Connected to emulator on port {port}")
                        return True
            except Exception:
                pass

        self.log(
            "No device found. Start your Android emulator and enable ADB.", "error"
        )
        return False

    def screenshot(self) -> "Image.Image | None":
        try:
            raw = subprocess.run(
                ["adb", "-s", self.device, "exec-out", "screencap", "-p"],
                capture_output=True,
                timeout=10,
            )
            return Image.open(io.BytesIO(raw.stdout))
        except Exception as e:
            self.log(f"Screenshot failed: {e}", "error")
            return None

    def tap(self, x: int, y: int, delay: float = 0.6):
        self._run("shell", "input", "tap", str(x), str(y))
        time.sleep(delay)

    def tap_coord(self, name: str, delay: float = 0.6):
        c = self.coords[name]
        self.tap(c[0], c[1], delay)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400):
        self._run("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))
        time.sleep(0.5)

    def type_text(self, text: str):
        safe = text.replace(" ", "%s").replace("'", "")
        self._run("shell", "input", "text", safe)
        time.sleep(0.4)

    def clear_field(self):
        self._run("shell", "input", "keyevent", "KEYCODE_CTRL_A")
        time.sleep(0.1)
        self._run("shell", "input", "keyevent", "KEYCODE_DEL")
        time.sleep(0.3)

    def press_back(self):
        self._run("shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(0.5)

    def launch_app(self):
        self._run(
            "shell", "am", "start", "-n", f"{APP_PACKAGE}/{APP_ACTIVITY}"
        )
        time.sleep(4)

    # ------------------------------------------------------------------
    # OCR price reader
    # ------------------------------------------------------------------

    def _ocr_region(self, img: "Image.Image", region: tuple) -> str:
        crop = img.crop(region)
        # Upscale + high-contrast greyscale improves OCR on small text
        crop = crop.resize(
            (crop.width * 3, crop.height * 3), Image.LANCZOS
        )
        crop = crop.convert("L")
        crop = ImageEnhance.Contrast(crop).enhance(3.0)
        crop = crop.filter(ImageFilter.SHARPEN)
        return pytesseract.image_to_string(
            crop,
            config="--psm 7 -c tessedit_char_whitelist=0123456789,",
        )

    def read_price(self, img: "Image.Image", region_key: str) -> int:
        if not TESSERACT_OK:
            return 0
        raw = self._ocr_region(img, self.coords[region_key])
        digits = re.sub(r"[^0-9]", "", raw)
        return int(digits) if digits else 0

    # ------------------------------------------------------------------
    # Marketplace navigation
    # ------------------------------------------------------------------

    def navigate_to_marketplace(self):
        self.log("Navigating to Marketplace…")
        self.launch_app()
        # Diamond Dynasty tab
        self.tap_coord("nav_diamond_dynasty")
        time.sleep(1.5)
        # Marketplace sub-tab
        self.tap_coord("nav_marketplace")
        time.sleep(2)

    def search_for(self, term: str):
        self.tap_coord("search_bar")
        time.sleep(0.4)
        self.clear_field()
        self.type_text(term)
        self._run("shell", "input", "keyevent", "KEYCODE_ENTER")
        time.sleep(2.5)

    # ------------------------------------------------------------------
    # Trading logic
    # ------------------------------------------------------------------

    CARD_TYPE_SEARCH = {
        "diamondEquipment": "Diamond Equipment",
        "liveSeries":       "Live Series",
        "sponsorships":     "Sponsorship",
    }

    def get_prices(self) -> tuple[int, int]:
        """Tap the first listing and read buy-now / best-sell prices."""
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

    def place_buy_order(self, price: int):
        """Tap first listing → Buy → set price → confirm."""
        self.tap_coord("first_card_tap")
        time.sleep(1.5)
        self.tap_coord("buy_now_button")
        time.sleep(1)
        # Enter custom bid price
        self.tap_coord("price_input")
        time.sleep(0.3)
        self.clear_field()
        self.type_text(str(price))
        time.sleep(0.3)
        self.tap_coord("confirm_button")
        time.sleep(1.5)
        self.tap_coord("ok_button")
        time.sleep(1)
        self.press_back()
        time.sleep(1)

    def attempt_flip(self, type_key: str) -> bool:
        search_term = self.CARD_TYPE_SEARCH[type_key]
        self.search_for(search_term)

        buy_now, best_sell = self.get_prices()

        if buy_now == 0 or best_sell == 0:
            self.log(f"[{search_term}] Could not read prices — skipping", "warn")
            return False

        gross_margin = buy_now - best_sell
        net_margin   = gross_margin - int(buy_now * SALES_TAX)

        self.log(
            f"[{search_term}] BuyNow={buy_now:,}  BestSell={best_sell:,}  "
            f"Net margin={net_margin:,}"
        )

        threshold = self.config.get("profitMargin", 500)
        if net_margin < threshold:
            self.log(f"[{search_term}] Margin too thin ({net_margin:,} < {threshold:,})")
            return False

        budget = self.config.get("maxBudget", 100_000)
        if best_sell + 1 > budget:
            self.log(f"[{search_term}] Cost {best_sell+1:,} exceeds budget {budget:,}")
            return False

        bid = best_sell + 1
        self.log(f"[{search_term}] Placing buy order at {bid:,} stubs…")
        self.place_buy_order(bid)

        self.stats["tradesCompleted"] += 1
        self.stats["stubsEarned"]     += net_margin

        self.trade_log.insert(0, {
            "time":       datetime.now().strftime("%H:%M:%S"),
            "type":       search_term,
            "buyNow":     buy_now,
            "bestSell":   best_sell,
            "bid":        bid,
            "netProfit":  net_margin,
        })
        self.stats["tradeHistory"] = self.trade_log[:50]

        self.log(f"[{search_term}] Order placed! Est. profit {net_margin:,} stubs")
        return True

    # ------------------------------------------------------------------
    # Active-hours guard
    # ------------------------------------------------------------------

    def in_active_window(self) -> bool:
        h = datetime.now().hour
        return self.config.get("activeHoursStart", 0) <= h < self.config.get("activeHoursEnd", 24)

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

        card_types = self.config.get("cardTypes", {})
        active = [k for k, v in card_types.items() if v]
        if not active:
            self.log("No card types enabled — enable at least one in config.", "warn")
            self.stats["running"] = False
            return

        delay = max(5, self.config.get("delayBetweenTrades", 30))

        while self.running:
            if not self.in_active_window():
                self.log("Outside active hours — sleeping 5 min…")
                for _ in range(300):
                    if not self.running:
                        break
                    time.sleep(1)
                continue

            for card_type in active:
                if not self.running:
                    break
                try:
                    self.attempt_flip(card_type)
                except ADBError as e:
                    self.log(f"ADB error: {e}", "error")
                    time.sleep(10)
                except Exception as e:
                    self.log(f"Unexpected error: {e}", "error")
                time.sleep(3)

            self.log(f"Cycle done. Next in {delay}s…")
            for _ in range(delay):
                if not self.running:
                    break
                time.sleep(1)

        self.log("Bot stopped.")

    def stop(self):
        self.running = False
