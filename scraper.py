#!/usr/bin/env python3
"""
PrizePicks API Scraper - Debug Version
"""
import json
import time
import requests
from datetime import datetime, timezone

def scrape_prizepicks():
    """Scrape data from PrizePicks Partner API"""
    print("🚀 Starting PrizePicks API scrape...")
    
    try:
        # PrizePicks Partner API endpoint
        url = "https://partner-api.prizepicks.com/projections"
        
        # Parameters
        params = {
            'league_id': '21',  # NFL
            'per_page': '50',
            'single_stat': 'true'
        }
        
        # Headers
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Referer': 'https://app.prizepicks.com/'
        }
        
        print(f"📡 Requesting URL: {url}")
        print(f"📡 Parameters: {params}")
        
        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        
        # DEBUG: Save raw API response
        with open("api_debug.json", "w") as f:
            json.dump(data, f, indent=2)
        print("📄 Raw API response saved to api_debug.json")
        
        # Show what we got
        print(f"📊 Response keys: {list(data.keys())}")
        print(f"📊 Number of data items: {len(data.get('data', []))}")
        print(f"📊 Number of included items: {len(data.get('included', []))}")
        
        # Parse the API response
        picks = []
        included = {item['id']: item for item in data.get('included', [])}
        
        for i, projection in enumerate(data.get('data', [])):
            try:
                print(f"\n--- Processing projection {i} ---")
                print(f"Projection keys: {list(projection.keys())}")
                
                attributes = projection.get('attributes', {})
                print(f"Attributes: {attributes}")
                
                # Get player info
                player_id = projection.get('relationships', {}).get('player', {}).get('data', {}).get('id')
                player = included.get(player_id, {})
                player_name = player.get('attributes', {}).get('name', 'Unknown')
                print(f"Player: {player_name}")
                
                # Get stat type and line
                stat_type = attributes.get('stat_type', 'POINTS')
                line_score = float(attributes.get('line_score', 0))
                print(f"Stat: {stat_type}, Line: {line_score}")
                
                # Simulate picks
                sport = "NFL"
                confidence = 75 + (hash(player_name + stat_type) % 20)
                pick = "OVER" if hash(player_name) % 2 == 0 else "UNDER"
                
                picks.append({
                    "player": player_name,
                    "sport": sport,
                    "statType": stat_type,
                    "propLine": line_score,
                    "pick": pick,
                    "confidence": confidence,
                    "reasoning": f"Based on recent {stat_type} averages and matchup analysis",
                    "ev": (confidence - 50) * 0.8,
                    "lastUpdated": datetime.now(timezone.utc).isoformat()
                })
                
            except Exception as e:
                print(f"❌ Error scraping projection {i}: {e}")
                continue
        
        print(f"\n✅ Successfully processed {len(picks)} picks")
        
        output = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "totalPicks": len(picks),
            "sports": ["NFL", "NBA", "MLB", "NHL"],
            "picks": picks,
            "status": "success"
        }
        
        with open("picks.json", "w") as f:
            json.dump(output, f, indent=2)
        
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        error_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "error",
            "error": str(e),
            "picks": []
        }
        with open("picks.json", "w") as f:
            json.dump(error_data, f, indent=2)

if __name__ == "__main__":
    scrape_prizepicks()
