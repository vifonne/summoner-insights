#!/usr/bin/env python3
"""
Summoner Insights - League of Legends Performance Analysis
AI-powered coaching through comprehensive match data collection and analysis
"""

import argparse
import os
import sqlite3
import requests
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

ADDED_MATCH_COLUMNS = {
    'participant_id': 'INTEGER',
    'queue_id': 'INTEGER',
    'wards_placed': 'INTEGER',
    'control_wards_bought': 'INTEGER',
    'control_wards_placed': 'INTEGER',
    'wards_killed': 'INTEGER',
    'lane_cs_at_10': 'INTEGER',
    'kill_participation': 'REAL',
    'team_damage_share': 'REAL',
}

MATCH_IDS_PAGE_SIZE = 100


def account_db_path(username, tagline):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, f"summoner_insights_{username}_{tagline}.db")


class SummonerInsights:
    def __init__(self, db_path=None):
        self.api_key = os.getenv('RIOT_API_KEY')
        self.username = os.getenv('RIOT_USERNAME')
        self.tagline = os.getenv('RIOT_TAGLINE')
        self.platform = (os.getenv('RIOT_PLATFORM') or self.tagline or '').lower()
        
        if not all([self.api_key, self.username, self.tagline]):
            raise ValueError("Missing required environment variables: RIOT_API_KEY, RIOT_USERNAME, RIOT_TAGLINE")
        
        self.base_url = f"https://{self.platform}.api.riotgames.com"
        self.regional_url = self._get_regional_url()
        self.headers = {"X-Riot-Token": self.api_key}
        self.db_path = db_path or account_db_path(self.username, self.tagline)
        self._init_database()
    
    def _get_regional_url(self):
        region_mapping = {
            'na1': 'americas',
            'br1': 'americas',
            'la1': 'americas',
            'la2': 'americas',
            'euw1': 'europe',
            'eun1': 'europe',
            'tr1': 'europe',
            'ru': 'europe',
            'kr': 'asia',
            'jp1': 'asia'
        }
        regional = region_mapping.get(self.platform, 'americas')
        return f"https://{regional}.api.riotgames.com"
    
    def _init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS matches (
                match_id TEXT PRIMARY KEY,
                game_creation TEXT,
                game_duration INTEGER,
                game_mode TEXT,
                champion TEXT,
                kills INTEGER,
                deaths INTEGER,
                assists INTEGER,
                kda REAL,
                cs INTEGER,
                gold_earned INTEGER,
                damage_dealt INTEGER,
                damage_taken INTEGER,
                vision_score INTEGER,
                win BOOLEAN,
                position TEXT,
                item_0 INTEGER,
                item_1 INTEGER,
                item_2 INTEGER,
                item_3 INTEGER,
                item_4 INTEGER,
                item_5 INTEGER
            )
        ''')

        columns = {row[1] for row in cursor.execute("PRAGMA table_info(matches)")}
        for column, column_type in ADDED_MATCH_COLUMNS.items():
            if column not in columns:
                cursor.execute(f"ALTER TABLE matches ADD COLUMN {column} {column_type}")
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS timeline_snapshots (
                match_id TEXT,
                minute INTEGER,
                cs INTEGER,
                gold INTEGER,
                xp INTEGER,
                level INTEGER,
                vision_score INTEGER,
                position_x INTEGER,
                position_y INTEGER,
                PRIMARY KEY (match_id, minute),
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS timeline_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id TEXT,
                timestamp INTEGER,
                event_type TEXT,
                position_x INTEGER,
                position_y INTEGER,
                details TEXT,
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def get_account_by_riot_id(self):
        import urllib.parse
        encoded_username = urllib.parse.quote(self.username)
        encoded_tagline = urllib.parse.quote(self.tagline)
        url = f"{self.regional_url}/riot/account/v1/accounts/by-riot-id/{encoded_username}/{encoded_tagline}"
        response = requests.get(url, headers=self.headers)
        
        if response.status_code != 200:
            raise Exception(f"Failed to get account data: {response.status_code} - {response.text}")
        
        return response.json()
    
    def get_summoner_data(self, puuid):
        url = f"{self.base_url}/lol/summoner/v4/summoners/by-puuid/{puuid}"
        response = requests.get(url, headers=self.headers)
        
        if response.status_code != 200:
            raise Exception(f"Failed to get summoner data: {response.status_code} - {response.text}")
        
        return response.json()
    
    def get_match_history(self, puuid, count=10):
        url = f"{self.regional_url}/lol/match/v5/matches/by-puuid/{puuid}/ids"
        match_ids = []

        while len(match_ids) < count:
            params = {"start": len(match_ids), "count": min(MATCH_IDS_PAGE_SIZE, count - len(match_ids))}
            response = requests.get(url, headers=self.headers, params=params)

            if response.status_code != 200:
                raise Exception(f"Failed to get match history: {response.status_code} - {response.text}")

            page = response.json()
            match_ids.extend(page)
            if len(page) < params["count"]:
                break

        return match_ids
    
    def get_match_details(self, match_id):
        url = f"{self.regional_url}/lol/match/v5/matches/{match_id}"
        response = requests.get(url, headers=self.headers)
        
        if response.status_code != 200:
            raise Exception(f"Failed to get match details: {response.status_code} - {response.text}")
        
        return response.json()
    
    def get_match_timeline(self, match_id):
        url = f"{self.regional_url}/lol/match/v5/matches/{match_id}/timeline"
        response = requests.get(url, headers=self.headers)
        
        if response.status_code != 200:
            raise Exception(f"Failed to get match timeline: {response.status_code} - {response.text}")
        
        return response.json()
    
    def extract_player_stats(self, match_data, puuid):
        participants = match_data['info']['participants']
        player_stats = None
        
        for participant in participants:
            if participant['puuid'] == puuid:
                player_stats = participant
                break
        
        if not player_stats:
            return None
        
        game_info = match_data['info']
        challenges = player_stats.get('challenges', {})

        return {
            'match_id': match_data['metadata']['matchId'],
            'game_creation': datetime.fromtimestamp(game_info['gameCreation'] / 1000).strftime('%Y-%m-%d %H:%M:%S'),
            'game_duration': game_info['gameDuration'],
            'game_mode': game_info['gameMode'],
            'champion': player_stats['championName'],
            'kills': player_stats['kills'],
            'deaths': player_stats['deaths'],
            'assists': player_stats['assists'],
            'kda': round((player_stats['kills'] + player_stats['assists']) / max(player_stats['deaths'], 1), 2),
            'cs': player_stats['totalMinionsKilled'] + player_stats['neutralMinionsKilled'],
            'gold_earned': player_stats['goldEarned'],
            'damage_dealt': player_stats['totalDamageDealtToChampions'],
            'damage_taken': player_stats['totalDamageTaken'],
            'vision_score': player_stats['visionScore'],
            'win': player_stats['win'],
            'position': player_stats['teamPosition'],
            'participant_id': player_stats['participantId'],
            'queue_id': game_info['queueId'],
            'wards_placed': player_stats['wardsPlaced'],
            'control_wards_bought': player_stats['visionWardsBoughtInGame'],
            'control_wards_placed': player_stats['detectorWardsPlaced'],
            'wards_killed': player_stats['wardsKilled'],
            'lane_cs_at_10': challenges.get('laneMinionsFirst10Minutes'),
            'kill_participation': challenges.get('killParticipation'),
            'team_damage_share': challenges.get('teamDamagePercentage'),
            'items': [
                player_stats['item0'], player_stats['item1'], player_stats['item2'],
                player_stats['item3'], player_stats['item4'], player_stats['item5']
            ]
        }
    
    def stored_match_ids(self):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("""
            SELECT match_id FROM matches m
            WHERE queue_id IS NOT NULL
            AND EXISTS (SELECT 1 FROM timeline_snapshots s WHERE s.match_id = m.match_id)
        """).fetchall()
        conn.close()
        return {row[0] for row in rows}

    def retrieve_stats(self, count=30):
        print(f"Retrieving stats for {self.username}#{self.tagline}...")
        
        account_data = self.get_account_by_riot_id()
        puuid = account_data['puuid']
        
        summoner_data = self.get_summoner_data(puuid)
        
        summoner_name = summoner_data.get('name', account_data.get('gameName', 'Unknown'))
        print(f"Found summoner: {summoner_name} (Level {summoner_data['summonerLevel']})")
        
        recent_ids = self.get_match_history(puuid, count)
        stored_ids = self.stored_match_ids()
        match_ids = [m for m in recent_ids if m not in stored_ids]
        print(f"Found {len(recent_ids)} recent matches, {len(match_ids)} not yet stored")
        
        all_stats = []
        
        for i, match_id in enumerate(match_ids, 1):
            print(f"Processing match {i}/{len(match_ids)}: {match_id}")
            
            try:
                match_data = self.get_match_details(match_id)
                stats = self.extract_player_stats(match_data, puuid)
                
                if stats:
                    print(f"  Fetching timeline data...")
                    timeline_data = self.get_match_timeline(match_id)
                    snapshots, events = self.extract_timeline_data(timeline_data, puuid)
                    self.write_timeline_to_database(match_id, snapshots, events)
                    print(f"  Timeline: {len(snapshots)} snapshots, {len(events)} events")

                    all_stats.append(stats)
                
                time.sleep(0.1)
                
            except Exception as e:
                print(f"Error processing match {match_id}: {e}")
                continue
        
        return all_stats
    
    def extract_timeline_data(self, timeline_data, puuid):
        if not timeline_data or 'info' not in timeline_data:
            return [], []
        
        frames = timeline_data['info']['frames']
        participant_id = None
        
        for participant in timeline_data['info']['participants']:
            if participant['puuid'] == puuid:
                participant_id = participant['participantId']
                break
        
        if not participant_id:
            return [], []
        
        snapshots = []
        events = []
        
        for frame in frames:
            minute = frame['timestamp'] // 60000
            
            if str(participant_id) in frame['participantFrames']:
                participant_frame = frame['participantFrames'][str(participant_id)]
                
                snapshot = {
                    'minute': minute,
                    'cs': participant_frame['minionsKilled'] + participant_frame['jungleMinionsKilled'],
                    'gold': participant_frame['totalGold'],
                    'xp': participant_frame['xp'],
                    'level': participant_frame['level'],
                    'position_x': participant_frame['position']['x'] if 'position' in participant_frame else 0,
                    'position_y': participant_frame['position']['y'] if 'position' in participant_frame else 0
                }
                snapshots.append(snapshot)
            
            if 'events' in frame:
                for event in frame['events']:
                    if self._is_player_event(event, participant_id):
                        event_data = {
                            'timestamp': event['timestamp'],
                            'event_type': event['type'],
                            'position_x': event.get('position', {}).get('x', 0),
                            'position_y': event.get('position', {}).get('y', 0),
                            'details': self._extract_event_details(event)
                        }
                        events.append(event_data)
        
        return snapshots, events
    
    def _is_player_event(self, event, participant_id):
        return (
            event.get('participantId') == participant_id or
            event.get('creatorId') == participant_id or
            event.get('killerId') == participant_id or
            event.get('victimId') == participant_id or
            (event.get('type') in ['CHAMPION_KILL', 'ELITE_MONSTER_KILL', 'BUILDING_KILL'] and 
             participant_id in event.get('assistingParticipantIds', []))
        )
    
    def _extract_event_details(self, event):
        details = {}
        event_type = event['type']
        
        if event_type == 'CHAMPION_KILL':
            details['killer'] = event.get('killerId')
            details['victim'] = event.get('victimId')
            details['assistants'] = event.get('assistingParticipantIds', [])
        elif event_type == 'ITEM_PURCHASED':
            details['item_id'] = event.get('itemId')
        elif event_type == 'ELITE_MONSTER_KILL':
            details['monster_type'] = event.get('monsterType')
            details['monster_subtype'] = event.get('monsterSubType')
        elif event_type == 'WARD_PLACED':
            details['ward_type'] = event.get('wardType')
        elif event_type == 'WARD_KILL':
            details['ward_type'] = event.get('wardType')
        
        return str(details) if details else ''
    
    def write_to_database(self, stats_data):
        if not stats_data:
            print("No data to write")
            return
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        new_matches = 0
        
        for stats in stats_data:
            row = {key: value for key, value in stats.items() if key != 'items'}
            row.update({f'item_{i}': item for i, item in enumerate(stats['items'])})

            cursor.execute(f'''
                INSERT OR REPLACE INTO matches ({', '.join(row)})
                VALUES ({', '.join(':' + column for column in row)})
            ''', row)
            
            if cursor.rowcount > 0:
                new_matches += 1
        
        conn.commit()
        conn.close()
        
        print(f"Database updated. {new_matches} matches processed")
    
    def write_timeline_to_database(self, match_id, snapshots, events):
        if not snapshots and not events:
            return
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM timeline_events WHERE match_id = ?", (match_id,))
        
        for snapshot in snapshots:
            cursor.execute('''
                INSERT OR REPLACE INTO timeline_snapshots (
                    match_id, minute, cs, gold, xp, level, vision_score, position_x, position_y
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                match_id, snapshot['minute'], snapshot['cs'], snapshot['gold'],
                snapshot['xp'], snapshot['level'], 0, snapshot['position_x'], snapshot['position_y']
            ))
        
        for event in events:
            cursor.execute('''
                INSERT OR REPLACE INTO timeline_events (
                    match_id, timestamp, event_type, position_x, position_y, details
                ) VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                match_id, event['timestamp'], event['event_type'],
                event['position_x'], event['position_y'], event['details']
            ))
        
        conn.commit()
        conn.close()

def main():
    parser = argparse.ArgumentParser(description="Download recent League of Legends matches into the local database")
    parser.add_argument("--count", type=int, default=30,
                        help="Number of recent matches to check; already stored matches are skipped (default: 30)")
    args = parser.parse_args()

    try:
        insights = SummonerInsights()
        stats = insights.retrieve_stats(args.count)
        insights.write_to_database(stats)

        print(f"\nSummary:")
        print(f"New matches processed: {len(stats)}")

        if stats:
            wins = sum(1 for stat in stats if stat['win'])
            print(f"Wins: {wins}/{len(stats)} ({wins/len(stats)*100:.1f}%)")
            avg_kda = sum(stat['kda'] for stat in stats) / len(stats)
            print(f"Average KDA: {avg_kda:.2f}")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()