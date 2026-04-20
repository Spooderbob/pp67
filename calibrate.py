#!/usr/bin/env python3
"""
StubFlip Calibrator
-------------------
Takes a screenshot from your emulator and saves it as screen.png.
Open that file in any image editor, hover over each UI element to read
its pixel coordinates, then paste them into config.json under "coords".

Usage:
    python calibrate.py
"""

import json
import subprocess
import sys
from pathlib import Path


def adb(*args, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["adb"] + list(args), capture_output=True, timeout=timeout)


def find_device() -> str | None:
    out = adb("devices").stdout.decode()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return None


def main():
    print("=" * 50)
    print("  StubFlip Calibrator")
    print("=" * 50)
    print()

    device = find_device()
    if device is None:
        # Try common emulator ports
        for port in (5555, 5554, 21503, 62001, 7555):
            try:
                adb("connect", f"127.0.0.1:{port}", timeout=4)
                device = find_device()
                if device:
                    break
            except Exception:
                pass

    if device is None:
        print("ERROR: No device found.")
        print()
        print("  1. Start your Android emulator (LDPlayer, BlueStacks, etc.)")
        print("  2. Run:  adb connect 127.0.0.1:5555")
        print("  3. Run:  adb devices  (should show 'device')")
        print("  4. Re-run this script.")
        sys.exit(1)

    print(f"Device: {device}")
    print()
    print("Taking screenshot…")

    result = subprocess.run(
        ["adb", "-s", device, "exec-out", "screencap", "-p"],
        capture_output=True,
        timeout=15,
    )

    if not result.stdout:
        print("ERROR: Screenshot empty. Is the screen on?")
        sys.exit(1)

    Path("screen.png").write_bytes(result.stdout)
    print("Saved:  screen.png")
    print()
    print("Next steps:")
    print()
    print("  1. Open screen.png in Paint, GIMP, Photoshop, or any image editor")
    print("  2. Navigate the MLB The Show Companion App to the Marketplace")
    print("     BEFORE running this script so the screenshot shows the right screen")
    print("  3. Hover over each element below and note its X, Y pixel position:")
    print()
    print("     COORDINATE        WHAT TO AIM AT")
    print("     ─────────────────────────────────────────────────────────────")
    print("     nav_diamond_dynasty  Diamond Dynasty button in bottom nav")
    print("     nav_marketplace      Marketplace sub-tab button")
    print("     search_bar           Search field at top of marketplace")
    print("     first_card_tap       Center of the first card in results")
    print("     buy_now_button       'Buy Now' button on the card detail screen")
    print("     confirm_button       Confirm / OK button on the popup")
    print("     price_input          Price text box when placing an order")
    print()
    print("  4. For region crops (price boxes), you need x1,y1,x2,y2:")
    print()
    print("     buy_now_region       Box around the Buy Now price number")
    print("     best_sell_region     Box around the Best Sell Order price")
    print()
    print("  5. Open config.json and fill in the 'coords' section, e.g.:")
    print()
    print('     "coords": {')
    print('       "nav_diamond_dynasty": [960, 1020],')
    print('       "buy_now_region":      [1050, 580, 1880, 650]')
    print('     }')
    print()
    print("  TIP: Coordinates that are already correct can be left out of")
    print("       the overrides — the defaults will be used.")


if __name__ == "__main__":
    main()
