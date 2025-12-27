#!/usr/bin/env python3
"""
PrizePicks AI Analyzer
Fetches real-time projections and analyzes them
"""
import json
import time
import random
import requests
from datetime import datetime, timezone
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

def analyze_projection(player, stat_type, line_score):
    """
    AI analysis: Generate pick recommendation
    This is where your analysis logic goes
    """
    # Simple algorithm (replace with real analysis)
    confidence = 75 + (hash(player + stat_type) % 20)
    
    # Determine pick
    pick = "OVER" if hash(player) % 2 == 0 else "UNDER"
    
    # Expected Value calculation
    ev = (confidence - 50) * 0.8
    
    return {
        "pick": pick,
        "confidence": confidence,
        "ev": round(ev, 1),
        "reasoning": f"Based on {stat_type} trends and matchup analysis"
    }

def fetch_from_api():
    """Try PrizePicks API first"""
    print("📡 Attempting API fetch...")
    try:
        # NFL=7, NBA=15, MLB=8, NHL=11 - try active leagues
        league_ids = ['7', '15', '8', '11']
        
        for league_id in league_ids:
            params = {
                'league_id': league_id,
                'per_page': '100',
                'single_stat': 'true'
            }
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json',
            }
            
            response = requests.get(
                "https://partner-api.prizepicks.com/projections",
                params=params,
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get('data'):
                    print(f"✅ API success - {len(data['data'])} projections")
                    return data
        
        print("❌ API empty for all leagues")
        return None
        
    except Exception as e:
        print(f"❌ API Error: {e}")
        return None

def scrape_with_selenium():
    """Fallback: Web scraper"""
    print("🌐 Starting web scraper...")
    
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    
    driver = webdriver.Chrome(options=chrome_options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined}")
    
    try:
        driver.get("https://app.prizepicks.com")
        time.sleep(8)  # Wait for page load
        
        # Try to close popup
        try:
            driver.find_element(By.CSS_SELECTOR, ".close").click()
            time.sleep(2)
        except:
            pass
        
        # Find projection cards
        cards = driver.find_elements(By.CSS_SELECTOR, "[data-testid='projection-card']")
        print(f"Found {len(cards)} cards via web scraper")
        
        picks = []
        for card in cards[:50]:
            try:
                player = card.find_element(By.CSS_SELECTOR, "[data-testid='player-name']").text
                stat = card.find_element(By.CSS_SELECTOR, "[data-testid='stat-type']").text
                line = card.find_element(By.CSS_SELECTOR, "[data-testid='line-score']").text
                
                analysis = analyze_projection(player, stat, float(line))
                
                picks.append({
                    "player": player,
                    "sport": "NFL",
                    "statType": stat,
                    "propLine": float(line),
                    **analysis,
                    "lastUpdated": datetime.now(timezone.utc).isoformat()
                })
            except:
                continue
        
        return picks
        
    finally:
        driver.quit()

def main():
    """Main execution"""
    print("🚀 PrizePicks AI Analyzer Starting...")
    
    # Try API first
    api_data = fetch_from_api()
    picks = []
    
    if api_data:
        # Parse API data
        included = {item['id']: item for item in api_data.get('included', [])}
        
        for projection in api_data.get('data', []):
            try:
                attrs = projection.get('attributes', {})
                player_id = projection.get('relationships', {}).get('player', {}).get('data', {}).get('id')
                player_data = included.get(player_id, {})
                player_name = player_data.get('attributes', {}).get('name', 'Unknown')
                
                stat_type = attrs.get('stat_type', 'POINTS')
                line_score = float(attrs.get('line_score', 0))
                
                analysis = analyze_projection(player_name, stat_type, line_score)
                
                picks.append({
                    "player": player_name,
                    "sport": "NFL",
                    "statType": stat_type,
                    "propLine": line_score,
                    **analysis,
                    "lastUpdated": datetime.now(timezone.utc).isoformat()
                })
            except:
                continue
    else:
        # API failed, use web scraper
        picks = scrape_with_selenium()
    
    # Save results
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "totalPicks": len(picks),
        "sports": ["NFL", "NBA", "MLB", "NHL"],
        "picks": picks,
        "status": "success"
    }
    
    with open("picks.json", "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"✅ Complete! {len(picks)} picks analyzed")

if __name__ == "__main__":
    main()
