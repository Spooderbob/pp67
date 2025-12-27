#!/usr/bin/env python3
"""
PrizePicks Debug Scraper - Saves EVERYTHING
"""

import json
import time
from datetime import datetime, timezone
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

def main():
    print("Starting DEBUG scraper...")
    
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1920,1080")
    
    driver = webdriver.Chrome(options=options)
    picks = []
    
    try:
        driver.get("https://app.prizepicks.com")
        time.sleep(10)
        
        # Save page source
        page_source = driver.page_source
        with open("debug_full_page.html", "w", encoding="utf-8") as f:
            f.write(page_source)
        print("📄 Saved: debug_full_page.html")
        
        # Save screenshot
        driver.save_screenshot("debug_screenshot.png")
        print("📸 Saved: debug_screenshot.png")
        
        # Try different selectors
        selectors_to_try = [
            "[data-testid='projection-card']",
            ".projection-card",
            "[class*='card']",
            "div"  # Last resort: find ALL divs
        ]
        
        for selector in selectors_to_try:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                print(f"Selector '{selector}' found {len(elements)} elements")
                
                if elements and selector == "[data-testid='projection-card']":
                    cards = elements
                    break
            except Exception as e:
                print(f"Selector '{selector}' failed: {e}")
        
        # Inspect first few elements
        if elements:
            for i, el in enumerate(elements[:5]):
                try:
                    text = el.text.strip()
                    if text:
                        print(f"Element {i} text: {text[:100]}...")
                except:
                    pass
        
        # Try to extract from cards if found
        if 'cards' in locals():
            for i, card in enumerate(cards[:5]):
                try:
                    print(f"\n--- Card {i} ---")
                    print(f"HTML: {card.get_attribute('outerHTML')[:200]}...")
                    print(f"Text: {card.text[:100]}...")
                except:
                    pass
        
        # Simple extraction attempt
        for card in cards[:20]:
            try:
                player = card.find_element(By.CSS_SELECTOR, "[data-testid='player-name']").text
                print(f"✅ Extracted: {player}")
            except:
                continue
        
    except Exception as e:
        print(f"❌ Fatal error: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
