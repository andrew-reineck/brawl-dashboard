import os
import requests
import sqlite3
from datetime import datetime

# Read secrets from GitHub Action environment variables
API_KEY = os.getenv("BRAWL_STARS_API_KEY")
PLAYER_TAG = os.getenv("BRAWL_STARS_PLAYER_TAG", "#29000000")

FORMATTED_TAG = PLAYER_TAG.replace("#", "%23")
BASE_URL = "https://api.brawlstars.com/v1"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json"
}

DB_PATH = "data/brawl_stars.db"

def init_db(cursor):
    """Initializes the relational database schema."""
    # 1. Overall Player Snapshots
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS player_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        player_tag TEXT,
        player_name TEXT,
        trophies INTEGER,
        highest_trophies INTEGER,
        exp_level INTEGER,
        victories_3v3 INTEGER,
        solo_victories INTEGER,
        duo_victories INTEGER
    )
    """)

    # 2. Per-Brawler Progression Snapshots (Powerups, Gears, Kit)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS brawler_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        player_tag TEXT,
        brawler_id INTEGER,
        brawler_name TEXT,
        power INTEGER,
        rank INTEGER,
        trophies INTEGER,
        highest_trophies INTEGER,
        gadget_count INTEGER,
        star_power_count INTEGER,
        gear_count INTEGER
    )
    """)

    # 3. Deduplicated Battle History with Ladder vs Ranked Queue Separation
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS battle_history (
        battle_time TEXT PRIMARY KEY,
        fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        queue_type TEXT,            -- 'Ladder', 'Ranked', or 'Other'
        event_mode TEXT,           -- 'brawlBall', 'knockout', 'gemGrab'
        event_map TEXT,            -- Map Name
        match_type TEXT,           -- 'ranked', 'soloRanked', 'teamRanked'
        result TEXT,               -- 'victory', 'defeat', 'draw'
        trophy_change INTEGER,     -- Populates for Ladder, 0 for Ranked
        brawler_used TEXT,
        brawler_power INTEGER,
        is_star_player BOOLEAN
    )
    """)

def parse_queue_type(battle_type):
    """Categorizes raw API battle type into Ladder vs Ranked."""
    if battle_type in ["soloRanked", "teamRanked"]:
        return "Ranked"
    elif battle_type == "ranked":
        return "Ladder"
    else:
        return "Other"

def run_etl():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    init_db(cursor)

    # ==========================================
    # 1. FETCH PLAYER PROFILE & BRAWLER SNAPSHOTS
    # ==========================================
    print(f"Fetching profile and brawlers for {PLAYER_TAG}...")
    p_res = requests.get(f"{BASE_URL}/players/{FORMATTED_TAG}", headers=HEADERS)
    
    if p_res.status_code == 200:
        p = p_res.json()
        
        # Save overall profile snapshot
        cursor.execute("""
            INSERT INTO player_snapshots 
            (player_tag, player_name, trophies, highest_trophies, exp_level, victories_3v3, solo_victories, duo_victories)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            p.get("tag"), p.get("name"), p.get("trophies"), p.get("highestTrophies"),
            p.get("expLevel"), p.get("3vs3Victories"), p.get("soloVictories"), p.get("duoVictories")
        ))

        # Save per-brawler snapshots
        brawlers = p.get("brawlers", [])
        for b in brawlers:
            cursor.execute("""
                INSERT INTO brawler_snapshots
                (player_tag, brawler_id, brawler_name, power, rank, trophies, highest_trophies, gadget_count, star_power_count, gear_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p.get("tag"),
                b.get("id"),
                b.get("name"),
                b.get("power"),
                b.get("rank"),
                b.get("trophies"),
                b.get("highestTrophies"),
                len(b.get("gadgets", [])),
                len(b.get("starPowers", [])),
                len(b.get("gears", []))
            ))
        print(f"Profile saved! Updated {len(brawlers)} brawlers ({p.get('trophies')} total trophies).")
    else:
        print(f"Profile Error {p_res.status_code}: {p_res.text}")

    # ==========================================
    # 2. FETCH & DEDUPLICATE BATTLE LOGS
    # ==========================================
    print("Fetching battle log...")
    b_res = requests.get(f"{BASE_URL}/players/{FORMATTED_TAG}/battlelog", headers=HEADERS)
    
    if b_res.status_code == 200:
        battles = b_res.json().get("items", [])
        inserted_count = 0

        for b in battles:
            b_time = b.get("battleTime")
            event = b.get("event", {})
            battle_info = b.get("battle", {})
            
            raw_type = battle_info.get("type", "")
            queue_type = parse_queue_type(raw_type)

            user_brawler = "Unknown"
            user_power = 0
            is_star = False

            # Check star player
            star_player = battle_info.get("starPlayer", {})
            if star_player and star_player.get("tag", "").replace("#", "") in PLAYER_TAG.replace("#", ""):
                is_star = True

            # Locate active player details inside team arrays
            if "teams" in battle_info:
                for team in battle_info["teams"]:
                    for player in team:
                        if player.get("tag", "").replace("#", "") in PLAYER_TAG.replace("#", ""):
                            user_brawler = player.get("brawler", {}).get("name", "Unknown")
                            user_power = player.get("brawler", {}).get("power", 0)

            cursor.execute("""
                INSERT OR IGNORE INTO battle_history 
                (battle_time, queue_type, event_mode, event_map, match_type, result, trophy_change, brawler_used, brawler_power, is_star_player)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                b_time,
                queue_type,
                event.get("mode"),
                event.get("map"),
                raw_type,
                battle_info.get("result"),
                battle_info.get("trophyChange", 0),
                user_brawler,
                user_power,
                is_star
            ))

            if cursor.rowcount > 0:
                inserted_count += 1

        print(f"Battle log processed: {inserted_count} new match(es) appended.")
    else:
        print(f"Battle Log Error {b_res.status_code}: {b_res.text}")

    conn.commit()
    conn.close()

if __name__ == "__main__":
    run_etl()