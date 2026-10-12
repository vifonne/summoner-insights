#!/usr/bin/env python3
"""
Summoner Insights MCP Server
Provides Claude with access to League of Legends match history and timeline data for AI-powered coaching
"""

import asyncio
import os
import sqlite3
import json
from typing import Any, Sequence
from mcp.server.models import InitializationOptions
from mcp.server import NotificationOptions, Server
from mcp.types import Resource, Tool, TextContent, ImageContent, EmbeddedResource
import mcp.types as types
from pydantic import AnyUrl
import argparse
import ast

from summoner_insights import account_db_path


def is_player_death(details: str, participant_id: int | None) -> bool:
    if participant_id is None or not details:
        return False
    try:
        parsed = ast.literal_eval(details)
    except (ValueError, SyntaxError):
        return False
    return isinstance(parsed, dict) and parsed.get('victim') == participant_id


QUEUE_IDS = {
    'ranked_solo': (420,),
    'ranked_flex': (440,),
    'ranked': (420, 440),
    'normal': (400, 430, 490),
    'aram': (450,),
}

QUEUE_NAMES = {
    420: 'Ranked Solo/Duo',
    440: 'Ranked Flex',
    400: 'Normal Draft',
    430: 'Normal Blind',
    490: 'Quickplay',
    450: 'ARAM',
}

REMAKE_MAX_SECONDS = 300

QUEUE_PROPERTY = {
    "type": "string",
    "enum": ["all", *QUEUE_IDS],
    "description": "Only include matches from this queue (default: all)",
    "default": "all",
}


def queue_condition(queue: str | None) -> tuple[str, tuple]:
    if not queue or queue == 'all':
        return "1 = 1", ()
    if queue not in QUEUE_IDS:
        raise ValueError(f"Unknown queue: {queue}. Expected one of: all, {', '.join(QUEUE_IDS)}")
    ids = QUEUE_IDS[queue]
    return f"queue_id IN ({','.join('?' * len(ids))})", ids


def average(values) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


class SummonerInsightsMCP:
    def __init__(self, db_path: str = None):
        if db_path is None:
            username, tagline = os.getenv('RIOT_USERNAME'), os.getenv('RIOT_TAGLINE')
            if not username or not tagline:
                raise ValueError("Missing required environment variables: RIOT_USERNAME, RIOT_TAGLINE")
            db_path = account_db_path(username, tagline)
        self.db_path = db_path
        self.server = Server("summoner-insights")
    
    def get_db_connection(self):
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"Database file not found at: {self.db_path}")
        return sqlite3.connect(self.db_path)
    
    def setup_handlers(self):
        @self.server.list_tools()
        async def handle_list_tools() -> list[Tool]:
            return [
                Tool(
                    name="get_recent_matches",
                    description="Get the most recent match history with basic stats",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "Number of matches to retrieve (default: 10)",
                                "default": 10
                            },
                            "queue": QUEUE_PROPERTY
                        }
                    }
                ),
                Tool(
                    name="get_match_timeline",
                    description="Get detailed timeline data for a specific match",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "match_id": {
                                "type": "string",
                                "description": "The match ID to get timeline data for"
                            }
                        },
                        "required": ["match_id"]
                    }
                ),
                Tool(
                    name="get_performance_trends",
                    description="Analyze performance trends across recent matches",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "matches": {
                                "type": "integer",
                                "description": "Number of recent matches to analyze (default: 10)",
                                "default": 10
                            },
                            "queue": QUEUE_PROPERTY
                        }
                    }
                ),
                Tool(
                    name="get_champion_performance",
                    description="Get performance statistics for specific champions",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "champion": {
                                "type": "string",
                                "description": "Champion name to analyze (optional)"
                            },
                            "queue": QUEUE_PROPERTY
                        }
                    }
                ),
                Tool(
                    name="analyze_death_patterns",
                    description="Analyze death locations and timing patterns for coaching insights",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "matches": {
                                "type": "integer",
                                "description": "Number of recent matches to analyze (default: 10)",
                                "default": 10
                            },
                            "queue": QUEUE_PROPERTY
                        }
                    }
                ),
                Tool(
                    name="get_farming_analysis",
                    description="Analyze CS progression and farming efficiency over time",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "matches": {
                                "type": "integer",
                                "description": "Number of recent matches to analyze (default: 10)",
                                "default": 10
                            },
                            "queue": QUEUE_PROPERTY
                        }
                    }
                ),
                Tool(
                    name="get_vision_analysis",
                    description="Analyze warding and vision control using Riot's official per-match ward stats",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "matches": {
                                "type": "integer",
                                "description": "Number of recent matches to analyze (default: 10)",
                                "default": 10
                            },
                            "queue": QUEUE_PROPERTY
                        }
                    }
                )
            ]
        
        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
            if arguments is None:
                arguments = {}
            
            try:
                queue = arguments.get("queue")
                if name == "get_recent_matches":
                    return await self._get_recent_matches(arguments.get("limit", 10), queue)
                elif name == "get_match_timeline":
                    return await self._get_match_timeline(arguments["match_id"])
                elif name == "get_performance_trends":
                    return await self._get_performance_trends(arguments.get("matches", 10), queue)
                elif name == "get_champion_performance":
                    return await self._get_champion_performance(arguments.get("champion"), queue)
                elif name == "analyze_death_patterns":
                    return await self._analyze_death_patterns(arguments.get("matches", 10), queue)
                elif name == "get_farming_analysis":
                    return await self._get_farming_analysis(arguments.get("matches", 10), queue)
                elif name == "get_vision_analysis":
                    return await self._get_vision_analysis(arguments.get("matches", 10), queue)
                else:
                    raise ValueError(f"Unknown tool: {name}")
            
            except Exception as e:
                return [TextContent(type="text", text=f"Error: {str(e)}")]
    
    async def _get_recent_matches(self, limit: int, queue: str | None = None) -> list[TextContent]:
        condition, params = queue_condition(queue)
        conn = self.get_db_connection()
        cursor = conn.cursor()

        cursor.execute(f"""
            SELECT match_id, game_creation, game_duration, champion, kills, deaths, assists,
                   kda, cs, gold_earned, vision_score, win, position, game_mode, queue_id,
                   lane_cs_at_10, kill_participation, team_damage_share
            FROM matches
            WHERE {condition}
            ORDER BY game_creation DESC
            LIMIT ?
        """, (*params, limit))

        matches = cursor.fetchall()
        conn.close()

        if not matches:
            return [TextContent(type="text", text="No match data found in database.")]

        result = f"# Recent {len(matches)} Matches\n\n"

        for match in matches:
            (match_id, creation, duration, champion, kills, deaths, assists, kda, cs, gold, vision, win, position, mode,
             queue_id, lane_cs_at_10, kill_participation, team_damage_share) = match
            win_status = "🟢 WIN" if win else "🔴 LOSS"
            duration_min = duration // 60
            mode = QUEUE_NAMES.get(queue_id, mode)

            result += f"## {champion} - {win_status}\n"
            result += f"**Match ID:** {match_id}\n"
            result += f"**Date:** {creation} | **Duration:** {duration_min}m | **Mode:** {mode} | **Position:** {position}\n"
            result += f"**KDA:** {kills}/{deaths}/{assists} ({kda}) | **CS:** {cs} | **Gold:** {gold:,} | **Vision:** {vision}\n"
            if kill_participation is not None:
                result += (f"**Lane CS @10:** {lane_cs_at_10} | **Kill Participation:** {kill_participation:.0%} "
                           f"| **Team Damage Share:** {team_damage_share:.0%}\n")
            result += "\n"
        
        return [TextContent(type="text", text=result)]
    
    async def _get_match_timeline(self, match_id: str) -> list[TextContent]:
        conn = self.get_db_connection()
        cursor = conn.cursor()
        
        # Get match info
        cursor.execute("SELECT champion, game_duration, win, participant_id FROM matches WHERE match_id = ?", (match_id,))
        match_info = cursor.fetchone()

        if not match_info:
            conn.close()
            return [TextContent(type="text", text=f"No match found with ID: {match_id}")]

        champion, duration, win, participant_id = match_info
        
        # Get timeline snapshots
        cursor.execute("""
            SELECT minute, cs, gold, xp, level, position_x, position_y
            FROM timeline_snapshots 
            WHERE match_id = ? 
            ORDER BY minute
        """, (match_id,))
        snapshots = cursor.fetchall()
        
        # Get timeline events
        cursor.execute("""
            SELECT timestamp, event_type, position_x, position_y, details
            FROM timeline_events 
            WHERE match_id = ? 
            ORDER BY timestamp
        """, (match_id,))
        events = cursor.fetchall()
        
        conn.close()
        
        win_status = "WIN" if win else "LOSS"
        result = f"# Timeline Analysis: {champion} ({win_status})\n"
        result += f"**Match ID:** {match_id} | **Duration:** {duration//60}m\n\n"
        
        if snapshots:
            result += "## Performance Progression\n"
            result += "| Min | CS | Gold | XP | Level | Position |\n"
            result += "|-----|----|----|-------|-------|---------|\n"
            
            for snapshot in snapshots[::5]:  # Every 5 minutes
                minute, cs, gold, xp, level, pos_x, pos_y = snapshot
                result += f"| {minute} | {cs} | {gold:,} | {xp:,} | {level} | ({pos_x}, {pos_y}) |\n"
        
        if events:
            result += f"\n## Key Events ({len(events)} total)\n"
            champion_kills = [e for e in events if e[1] == 'CHAMPION_KILL']
            death_events = [e for e in champion_kills if is_player_death(e[4], participant_id)]
            kill_events = [e for e in champion_kills if not is_player_death(e[4], participant_id)]
            
            if death_events:
                result += f"\n### Deaths ({len(death_events)})\n"
                for event in death_events[:5]:  # Show first 5 deaths
                    timestamp, _, pos_x, pos_y, details = event
                    minute = timestamp // 60000
                    result += f"- **{minute}m**: Death at ({pos_x}, {pos_y}) - {details}\n"
            
            if kill_events:
                result += f"\n### Kills/Assists ({len(kill_events)})\n"
                for event in kill_events[:5]:  # Show first 5 kills
                    timestamp, _, pos_x, pos_y, details = event
                    minute = timestamp // 60000
                    result += f"- **{minute}m**: Kill at ({pos_x}, {pos_y}) - {details}\n"
        
        return [TextContent(type="text", text=result)]
    
    async def _get_performance_trends(self, matches: int, queue: str | None = None) -> list[TextContent]:
        condition, params = queue_condition(queue)
        conn = self.get_db_connection()
        cursor = conn.cursor()

        cursor.execute(f"""
            SELECT champion, kills, deaths, assists, kda, cs, gold_earned, vision_score, win, game_duration,
                   lane_cs_at_10, kill_participation, team_damage_share
            FROM matches
            WHERE {condition}
            ORDER BY game_creation DESC
            LIMIT ?
        """, (*params, matches))
        
        recent_matches = cursor.fetchall()
        conn.close()
        
        if not recent_matches:
            return [TextContent(type="text", text="No match data available for trend analysis.")]
        
        # Calculate trends
        total_matches = len(recent_matches)
        wins = sum(1 for match in recent_matches if match[8])
        win_rate = (wins / total_matches) * 100
        
        avg_kda = sum(match[4] for match in recent_matches) / total_matches
        avg_cs = sum(match[5] for match in recent_matches) / total_matches
        avg_vision = sum(match[7] for match in recent_matches) / total_matches
        avg_duration = sum(match[9] for match in recent_matches) / total_matches / 60
        
        # Recent vs older performance
        recent_half = recent_matches[:total_matches//2]
        older_half = recent_matches[total_matches//2:]
        
        recent_win_rate = (sum(1 for m in recent_half if m[8]) / len(recent_half)) * 100 if recent_half else 0
        older_win_rate = (sum(1 for m in older_half if m[8]) / len(older_half)) * 100 if older_half else 0
        
        result = f"# Performance Trends (Last {total_matches} matches)\n\n"
        result += f"## Overall Statistics\n"
        result += f"- **Win Rate:** {win_rate:.1f}% ({wins}/{total_matches})\n"
        result += f"- **Average KDA:** {avg_kda:.2f}\n"
        result += f"- **Average CS:** {avg_cs:.1f}\n"
        result += f"- **Average Vision Score:** {avg_vision:.1f}\n"
        result += f"- **Average Game Duration:** {avg_duration:.1f} minutes\n"

        avg_lane_cs_at_10 = average(match[10] for match in recent_matches)
        avg_kill_participation = average(match[11] for match in recent_matches)
        avg_damage_share = average(match[12] for match in recent_matches)
        if avg_lane_cs_at_10 is not None:
            result += f"- **Average Lane CS @10:** {avg_lane_cs_at_10:.1f}\n"
        if avg_kill_participation is not None:
            result += f"- **Average Kill Participation:** {avg_kill_participation:.0%}\n"
        if avg_damage_share is not None:
            result += f"- **Average Team Damage Share:** {avg_damage_share:.0%}\n"
        result += "\n"
        
        result += f"## Trend Analysis\n"
        if len(recent_half) > 0 and len(older_half) > 0:
            trend = "📈 Improving" if recent_win_rate > older_win_rate else "📉 Declining" if recent_win_rate < older_win_rate else "➡️ Stable"
            result += f"- **Recent Performance:** {trend}\n"
            result += f"  - Recent {len(recent_half)} matches: {recent_win_rate:.1f}% WR\n"
            result += f"  - Previous {len(older_half)} matches: {older_win_rate:.1f}% WR\n\n"
        
        # Champion diversity
        champions = [match[0] for match in recent_matches]
        unique_champions = len(set(champions))
        result += f"- **Champion Pool:** {unique_champions} unique champions\n"
        
        return [TextContent(type="text", text=result)]
    
    async def _get_champion_performance(self, champion: str = None, queue: str | None = None) -> list[TextContent]:
        condition, params = queue_condition(queue)
        conn = self.get_db_connection()
        cursor = conn.cursor()

        if champion:
            cursor.execute(f"""
                SELECT champion, COUNT(*) as games,
                       SUM(CASE WHEN win THEN 1 ELSE 0 END) as wins,
                       AVG(kda) as avg_kda, AVG(cs) as avg_cs, AVG(vision_score) as avg_vision
                FROM matches
                WHERE champion = ? AND {condition}
                GROUP BY champion
            """, (champion, *params))
        else:
            cursor.execute(f"""
                SELECT champion, COUNT(*) as games,
                       SUM(CASE WHEN win THEN 1 ELSE 0 END) as wins,
                       AVG(kda) as avg_kda, AVG(cs) as avg_cs, AVG(vision_score) as avg_vision
                FROM matches
                WHERE {condition}
                GROUP BY champion
                ORDER BY games DESC
            """, params)
        
        champions = cursor.fetchall()
        conn.close()
        
        if not champions:
            return [TextContent(type="text", text="No champion data found.")]
        
        result = f"# Champion Performance Analysis\n\n"
        result += "| Champion | Games | Win Rate | Avg KDA | Avg CS | Avg Vision |\n"
        result += "|----------|-------|----------|---------|---------|------------|\n"
        
        for champ_data in champions:
            champ, games, wins, avg_kda, avg_cs, avg_vision = champ_data
            win_rate = (wins / games) * 100
            result += f"| {champ} | {games} | {win_rate:.1f}% | {avg_kda:.2f} | {avg_cs:.1f} | {avg_vision:.1f} |\n"
        
        return [TextContent(type="text", text=result)]
    
    async def _analyze_death_patterns(self, matches: int, queue: str | None = None) -> list[TextContent]:
        condition, params = queue_condition(queue)
        conn = self.get_db_connection()
        cursor = conn.cursor()

        # Get recent match IDs
        cursor.execute(f"""
            SELECT match_id, participant_id FROM matches
            WHERE {condition}
            ORDER BY game_creation DESC
            LIMIT ?
        """, (*params, matches))
        participant_ids = dict(cursor.fetchall())

        if not participant_ids:
            conn.close()
            return [TextContent(type="text", text="No matches found for death pattern analysis.")]

        # Get death events
        match_ids = list(participant_ids)
        placeholders = ','.join('?' * len(match_ids))
        cursor.execute(f"""
            SELECT match_id, timestamp, position_x, position_y, details
            FROM timeline_events
            WHERE match_id IN ({placeholders})
            AND event_type = 'CHAMPION_KILL'
            ORDER BY timestamp
        """, match_ids)

        deaths = [
            row for row in cursor.fetchall()
            if is_player_death(row[4], participant_ids[row[0]])
        ]
        conn.close()

        missing = [m for m, pid in participant_ids.items() if pid is None]
        if missing:
            return [TextContent(type="text", text=(
                f"{len(missing)} of the last {len(participant_ids)} matches were stored without a participant ID, "
                "so deaths cannot be told apart from kills. Re-run summoner_insights.py to refresh them."
            ))]

        if not deaths:
            return [TextContent(type="text", text="No death data found in recent matches.")]
        
        result = f"# Death Pattern Analysis (Last {len(participant_ids)} matches)\n\n"
        result += f"**Total Deaths:** {len(deaths)}\n\n"
        
        # Timing analysis
        early_deaths = len([d for d in deaths if d[1] < 15 * 60 * 1000])  # < 15 min
        mid_deaths = len([d for d in deaths if 15 * 60 * 1000 <= d[1] < 25 * 60 * 1000])  # 15-25 min
        late_deaths = len([d for d in deaths if d[1] >= 25 * 60 * 1000])  # 25+ min
        
        result += "## Death Timing Distribution\n"
        result += f"- **Early Game (0-15m):** {early_deaths} deaths ({early_deaths/len(deaths)*100:.1f}%)\n"
        result += f"- **Mid Game (15-25m):** {mid_deaths} deaths ({mid_deaths/len(deaths)*100:.1f}%)\n"
        result += f"- **Late Game (25m+):** {late_deaths} deaths ({late_deaths/len(deaths)*100:.1f}%)\n\n"
        
        # Position analysis (simplified)
        river_deaths = len([d for d in deaths if 4000 <= d[2] <= 10000 and 4000 <= d[3] <= 10000])
        jungle_deaths = len([d for d in deaths if d[2] < 4000 or d[2] > 10000 or d[3] < 4000 or d[3] > 10000])
        
        result += "## Death Location Patterns\n"
        result += f"- **River/Mid Area:** {river_deaths} deaths\n"
        result += f"- **Jungle/Side Areas:** {jungle_deaths} deaths\n\n"
        
        result += "## Recent Deaths (Last 5)\n"
        for death in deaths[-5:]:
            match_id, timestamp, pos_x, pos_y, details = death
            minute = timestamp // 60000
            result += f"- **{minute}m** in match {match_id[-8:]}: Position ({pos_x}, {pos_y})\n"
        
        return [TextContent(type="text", text=result)]
    
    async def _get_farming_analysis(self, matches: int, queue: str | None = None) -> list[TextContent]:
        condition, params = queue_condition(queue)
        conn = self.get_db_connection()
        cursor = conn.cursor()

        # Get recent match IDs and their CS
        cursor.execute(f"""
            SELECT m.match_id, m.champion, m.cs, m.game_duration, m.win
            FROM matches m
            WHERE {condition}
            ORDER BY m.game_creation DESC
            LIMIT ?
        """, (*params, matches))
        matches_data = cursor.fetchall()

        if not matches_data:
            conn.close()
            return [TextContent(type="text", text="No farming data available.")]
        
        # Get timeline snapshots for CS progression
        match_ids = [m[0] for m in matches_data]
        placeholders = ','.join('?' * len(match_ids))
        cursor.execute(f"""
            SELECT match_id, minute, cs
            FROM timeline_snapshots 
            WHERE match_id IN ({placeholders})
            ORDER BY match_id, minute
        """, match_ids)
        
        timeline_data = cursor.fetchall()
        conn.close()
        
        result = f"# Farming Analysis (Last {len(matches_data)} matches)\n\n"
        
        # Overall CS stats
        total_cs = sum(m[2] for m in matches_data)
        avg_cs = total_cs / len(matches_data)
        avg_duration = sum(m[3] for m in matches_data) / len(matches_data) / 60
        cs_per_min = avg_cs / avg_duration
        
        result += f"## Overall Farming Performance\n"
        result += f"- **Average CS:** {avg_cs:.1f}\n"
        result += f"- **Average CS/min:** {cs_per_min:.1f}\n"
        result += f"- **Total CS:** {total_cs}\n\n"
        
        # CS efficiency by game outcome
        wins = [m for m in matches_data if m[4]]
        losses = [m for m in matches_data if not m[4]]
        
        if wins and losses:
            win_avg_cs = sum(m[2] for m in wins) / len(wins)
            loss_avg_cs = sum(m[2] for m in losses) / len(losses)
            
            result += f"## CS by Game Outcome\n"
            result += f"- **Wins:** {win_avg_cs:.1f} avg CS ({len(wins)} games)\n"
            result += f"- **Losses:** {loss_avg_cs:.1f} avg CS ({len(losses)} games)\n"
            result += f"- **Difference:** {win_avg_cs - loss_avg_cs:+.1f} CS in wins\n\n"
        
        # Champion-specific CS
        champion_cs = {}
        for match in matches_data:
            champ = match[1]
            cs = match[2]
            if champ not in champion_cs:
                champion_cs[champ] = []
            champion_cs[champ].append(cs)
        
        result += f"## CS by Champion\n"
        for champ, cs_list in champion_cs.items():
            avg_champ_cs = sum(cs_list) / len(cs_list)
            result += f"- **{champ}:** {avg_champ_cs:.1f} avg CS ({len(cs_list)} games)\n"
        
        # Timeline progression analysis
        if timeline_data:
            result += f"\n## CS Progression Patterns\n"
            # Group by minute intervals
            minute_cs = {}
            for match_id, minute, cs in timeline_data:
                if minute not in minute_cs:
                    minute_cs[minute] = []
                minute_cs[minute].append(cs)
            
            result += "| Minute | Avg CS | CS/min Rate |\n"
            result += "|--------|--------|-----------|\n"
            
            prev_cs = 0
            for minute in sorted(minute_cs.keys())[::5]:  # Every 5 minutes
                if minute_cs[minute]:
                    avg_cs_at_min = sum(minute_cs[minute]) / len(minute_cs[minute])
                    cs_rate = (avg_cs_at_min - prev_cs) / 5 if minute > 0 else avg_cs_at_min / max(1, minute)
                    result += f"| {minute} | {avg_cs_at_min:.1f} | {cs_rate:.1f} |\n"
                    prev_cs = avg_cs_at_min
        
        return [TextContent(type="text", text=result)]

    async def _get_vision_analysis(self, matches: int, queue: str | None = None) -> list[TextContent]:
        condition, params = queue_condition(queue)
        conn = self.get_db_connection()
        cursor = conn.cursor()

        cursor.execute(f"""
            SELECT champion, game_duration, win, vision_score,
                   wards_placed, control_wards_bought, control_wards_placed, wards_killed
            FROM matches
            WHERE {condition} AND wards_placed IS NOT NULL AND game_duration >= {REMAKE_MAX_SECONDS}
            ORDER BY game_creation DESC
            LIMIT ?
        """, (*params, matches))
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return [TextContent(type="text", text=(
                "No ward data found. Re-run summoner_insights.py to download Riot's per-match ward stats."
            ))]

        def per_minute(value, duration):
            return value / (duration / 60)

        games = len(rows)
        no_control_wards = sum(1 for row in rows if row[5] == 0)

        result = f"# Vision Analysis (Last {games} matches, remakes excluded)\n\n"
        result += "## Averages\n"
        result += f"- **Vision Score/min:** {average(per_minute(r[3], r[1]) for r in rows):.2f}\n"
        result += f"- **Wards Placed/min:** {average(per_minute(r[4], r[1]) for r in rows):.2f}\n"
        result += f"- **Control Wards Bought per Game:** {average(r[5] for r in rows):.1f}\n"
        result += f"- **Control Wards Placed per Game:** {average(r[6] for r in rows):.1f}\n"
        result += f"- **Enemy Wards Cleared per Game:** {average(r[7] for r in rows):.1f}\n"
        result += f"- **Games Without a Control Ward:** {no_control_wards}/{games}\n\n"

        wins = [r for r in rows if r[2]]
        losses = [r for r in rows if not r[2]]
        if wins and losses:
            result += "## Vision Score/min by Outcome\n"
            result += f"- **Wins:** {average(per_minute(r[3], r[1]) for r in wins):.2f} ({len(wins)} games)\n"
            result += f"- **Losses:** {average(per_minute(r[3], r[1]) for r in losses):.2f} ({len(losses)} games)\n\n"

        result += "## Per Match\n"
        result += "| Champion | Min | Result | Vision | Wards | Control Bought | Control Placed | Cleared |\n"
        result += "|----------|-----|--------|--------|-------|----------------|----------------|---------|\n"
        for champion, duration, win, vision, placed, bought, control_placed, killed in rows:
            result += (f"| {champion} | {duration // 60} | {'W' if win else 'L'} | {vision} | {placed} "
                       f"| {bought} | {control_placed} | {killed} |\n")

        return [TextContent(type="text", text=result)]


async def main():
    parser = argparse.ArgumentParser(description="Summoner Insights MCP Server")
    parser.add_argument("--db-path",
                       help="Path to SQLite database file (default: the database of the account in .env)")
    args = parser.parse_args()
    
    server = SummonerInsightsMCP(args.db_path)
    server.setup_handlers()
    
    from mcp.server.stdio import stdio_server
    
    async with stdio_server() as (read_stream, write_stream):
        await server.server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="summoner-insights",
                server_version="1.0.0",
                capabilities=server.server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())