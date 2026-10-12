import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from summoner_insights import ADDED_MATCH_COLUMNS, SummonerInsights, account_db_path
from summoner_insights_mcp import SummonerInsightsMCP, is_player_death

PLAYER = 4
RANKED_SOLO = 420
ARAM = 450
SNAPSHOT = {'minute': 0, 'cs': 0, 'gold': 500, 'xp': 0, 'level': 1, 'position_x': 0, 'position_y': 0}


def kill_event(timestamp, killer, victim, assistants=()):
    return {
        'timestamp': timestamp,
        'event_type': 'CHAMPION_KILL',
        'position_x': 7000,
        'position_y': 7000,
        'details': str({'killer': killer, 'victim': victim, 'assistants': list(assistants)}),
    }


def match_stats(match_id='EUW1_1', participant_id=PLAYER, queue_id=RANKED_SOLO, **overrides):
    stats = {
        'match_id': match_id, 'game_creation': '2026-10-10 12:00:00', 'game_duration': 1800,
        'game_mode': 'CLASSIC', 'champion': 'Jhin', 'kills': 1, 'deaths': 2, 'assists': 1,
        'kda': 1.0, 'cs': 200, 'gold_earned': 10000, 'damage_dealt': 20000,
        'damage_taken': 15000, 'vision_score': 15, 'win': False, 'position': 'BOTTOM',
        'participant_id': participant_id, 'queue_id': queue_id, 'wards_placed': 9,
        'control_wards_bought': 0, 'control_wards_placed': 0, 'wards_killed': 1,
        'lane_cs_at_10': 70, 'kill_participation': 0.5, 'team_damage_share': 0.3, 'items': [0] * 6,
    }
    stats.update(overrides)
    return stats


def riot_match(match_id, challenges=True):
    participant = {
        'puuid': 'me', 'participantId': PLAYER, 'championName': 'Jhin', 'kills': 1, 'deaths': 2,
        'assists': 1, 'totalMinionsKilled': 180, 'neutralMinionsKilled': 20, 'goldEarned': 10000,
        'totalDamageDealtToChampions': 20000, 'totalDamageTaken': 15000, 'visionScore': 15,
        'win': True, 'teamPosition': 'BOTTOM', 'wardsPlaced': 9, 'visionWardsBoughtInGame': 2,
        'detectorWardsPlaced': 1, 'wardsKilled': 3,
        **{f'item{i}': 0 for i in range(6)},
    }
    if challenges:
        participant['challenges'] = {
            'laneMinionsFirst10Minutes': 72, 'killParticipation': 0.6, 'teamDamagePercentage': 0.31,
        }
    return {
        'metadata': {'matchId': match_id},
        'info': {
            'gameCreation': 1_760_000_000_000, 'gameDuration': 1800, 'gameMode': 'CLASSIC',
            'queueId': RANKED_SOLO, 'participants': [participant],
        },
    }


class SummonerInsightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        env = {'RIOT_API_KEY': 'x', 'RIOT_USERNAME': 'x', 'RIOT_TAGLINE': 'EUW1'}
        self.db_path = os.path.join(self.tmp.name, 'summoner_insights.db')
        with mock.patch.dict(os.environ, env):
            self.insights = SummonerInsights(db_path=self.db_path)
        self.mcp = SummonerInsightsMCP(db_path=self.db_path)

    def store_match(self, events, timeline=True, **overrides):
        stats = match_stats(**overrides)
        self.insights.write_to_database([stats])
        snapshots = [SNAPSHOT] if timeline else []
        self.insights.write_timeline_to_database(stats['match_id'], snapshots, events)

    def download(self, match_history, timeline=None):
        with mock.patch.multiple(
            self.insights,
            get_account_by_riot_id=mock.Mock(return_value={'puuid': 'me'}),
            get_summoner_data=mock.Mock(return_value={'summonerLevel': 1}),
            get_match_history=mock.Mock(return_value=match_history),
            get_match_details=mock.Mock(side_effect=riot_match),
            get_match_timeline=timeline or mock.Mock(return_value={}),
        ), mock.patch('summoner_insights.time.sleep'), mock.patch('builtins.print'):
            return self.insights.retrieve_stats(count=len(match_history))

    def report(self, method, *args):
        return asyncio.run(getattr(self.mcp, method)(*args))[0].text

    def test_counts_only_player_deaths(self):
        self.store_match([
            kill_event(60_000, killer=7, victim=PLAYER),
            kill_event(120_000, killer=PLAYER, victim=8),
            kill_event(180_000, killer=5, victim=9, assistants=[PLAYER]),
            kill_event(1_600_000, killer=8, victim=PLAYER, assistants=[7]),
        ])
        self.assertIn('**Total Deaths:** 2', self.report('_analyze_death_patterns', 10))

    def test_rerunning_download_does_not_duplicate_events(self):
        events = [kill_event(60_000, killer=7, victim=PLAYER)]
        self.store_match(events)
        self.store_match(events)
        with closing(sqlite3.connect(self.db_path)) as conn:
            count = conn.execute('SELECT COUNT(*) FROM timeline_events').fetchone()[0]
        self.assertEqual(count, 1)

    def test_reports_matches_missing_participant_id(self):
        self.store_match([kill_event(60_000, killer=7, victim=PLAYER)], participant_id=None)
        self.assertIn('without a participant ID', self.report('_analyze_death_patterns', 10))

    def test_adds_new_columns_to_existing_database(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute('DROP TABLE matches')
            conn.execute('CREATE TABLE matches (match_id TEXT PRIMARY KEY)')
        self.insights._init_database()
        with closing(sqlite3.connect(self.db_path)) as conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(matches)')}
        self.assertLessEqual(set(ADDED_MATCH_COLUMNS), columns)

    def test_each_account_uses_its_own_database(self):
        self.assertNotEqual(account_db_path('Compux72', 'EUW'), account_db_path('Nana', '46546'))
        env = {'RIOT_API_KEY': 'x', 'RIOT_USERNAME': 'Nana', 'RIOT_TAGLINE': '46546'}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(SummonerInsights, '_init_database'):
            downloader_path = SummonerInsights().db_path
            server_path = SummonerInsightsMCP().db_path
        self.assertEqual(downloader_path, account_db_path('Nana', '46546'))
        self.assertEqual(server_path, downloader_path)

    def test_is_player_death(self):
        self.assertTrue(is_player_death(str({'killer': 1, 'victim': PLAYER}), PLAYER))
        self.assertFalse(is_player_death(str({'killer': PLAYER, 'victim': 1}), PLAYER))
        self.assertFalse(is_player_death(str({'killer': 1, 'victim': PLAYER}), None))
        self.assertFalse(is_player_death('', PLAYER))

    def test_extracts_wards_placed_by_player(self):
        timeline = {'info': {
            'participants': [{'participantId': PLAYER, 'puuid': 'me'}],
            'frames': [{'timestamp': 60_000, 'participantFrames': {}, 'events': [
                {'type': 'WARD_PLACED', 'timestamp': 61_000, 'creatorId': PLAYER, 'wardType': 'CONTROL_WARD'},
                {'type': 'WARD_PLACED', 'timestamp': 62_000, 'creatorId': 9, 'wardType': 'SIGHT_WARD'},
            ]}],
        }}
        _, events = self.insights.extract_timeline_data(timeline, 'me')
        self.assertEqual(
            [(e['event_type'], e['details']) for e in events],
            [('WARD_PLACED', str({'ward_type': 'CONTROL_WARD'}))],
        )

    def test_extracts_official_match_stats(self):
        stats = self.insights.extract_player_stats(riot_match('EUW1_1'), 'me')
        self.assertEqual(
            {k: stats[k] for k in ADDED_MATCH_COLUMNS},
            {
                'participant_id': PLAYER, 'queue_id': RANKED_SOLO, 'wards_placed': 9,
                'control_wards_bought': 2, 'control_wards_placed': 1, 'wards_killed': 3,
                'lane_cs_at_10': 72, 'kill_participation': 0.6, 'team_damage_share': 0.31,
            },
        )

    def test_extracts_match_without_challenges(self):
        stats = self.insights.extract_player_stats(riot_match('EUW1_1', challenges=False), 'me')
        self.insights.write_to_database([stats])
        self.assertIsNone(stats['kill_participation'])
        self.assertIn('Jhin', self.report('_get_recent_matches', 10))

    def test_skips_matches_already_stored(self):
        self.store_match([])
        self.store_match([], match_id='EUW1_OLD', queue_id=None)
        stats = self.download(['EUW1_NEW', 'EUW1_1', 'EUW1_OLD'])
        self.assertEqual([s['match_id'] for s in stats], ['EUW1_NEW', 'EUW1_OLD'])

    def test_redownloads_matches_missing_timeline(self):
        self.store_match([], match_id='EUW1_NO_TIMELINE', timeline=False)
        stats = self.download(['EUW1_NO_TIMELINE'])
        self.assertEqual([s['match_id'] for s in stats], ['EUW1_NO_TIMELINE'])

    def test_failed_timeline_download_does_not_save_match(self):
        def timeline(match_id):
            if match_id == 'EUW1_FAILS':
                raise Exception('429 - Rate limit exceeded')
            return {}

        stats = self.download(['EUW1_FAILS', 'EUW1_OK'], timeline=mock.Mock(side_effect=timeline))
        self.assertEqual([s['match_id'] for s in stats], ['EUW1_OK'])

    def test_match_history_pages_past_riot_limit(self):
        def page(url, headers, params):
            response = mock.Mock(status_code=200)
            response.json.return_value = [f"EUW1_{params['start'] + i}" for i in range(params['count'])]
            return response

        with mock.patch('summoner_insights.requests.get', side_effect=page) as get:
            match_ids = self.insights.get_match_history('me', count=150)
        self.assertEqual(len(match_ids), 150)
        self.assertEqual(len(set(match_ids)), 150)
        self.assertEqual([c.kwargs['params']['count'] for c in get.call_args_list], [100, 50])

    def test_match_history_stops_when_history_runs_out(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = ['EUW1_1', 'EUW1_2']
        with mock.patch('summoner_insights.requests.get', return_value=response) as get:
            match_ids = self.insights.get_match_history('me', count=150)
        self.assertEqual(match_ids, ['EUW1_1', 'EUW1_2'])
        self.assertEqual(get.call_count, 1)

    def test_filters_by_queue(self):
        self.store_match([], match_id='EUW1_RANKED', champion='Jhin')
        self.store_match([], match_id='EUW1_ARAM', champion='Teemo', queue_id=ARAM)
        ranked = self.report('_get_recent_matches', 10, 'ranked')
        self.assertIn('Jhin', ranked)
        self.assertNotIn('Teemo', ranked)
        self.assertIn('Ranked Solo/Duo', ranked)
        self.assertIn('Teemo', self.report('_get_recent_matches', 10, 'all'))
        self.assertIn('Teemo', self.report('_get_champion_performance', None, 'aram'))
        self.assertNotIn('Jhin', self.report('_get_champion_performance', None, 'aram'))

    def test_rejects_unknown_queue(self):
        self.store_match([])
        with self.assertRaises(ValueError):
            self.report('_get_recent_matches', 10, 'urf')

    def test_vision_analysis_uses_official_stats_and_skips_remakes(self):
        self.store_match([], match_id='EUW1_1', wards_placed=9, control_wards_bought=0)
        self.store_match([], match_id='EUW1_2', wards_placed=12, control_wards_bought=2, win=True)
        self.store_match([], match_id='EUW1_REMAKE', game_duration=90, wards_placed=0)
        text = self.report('_get_vision_analysis', 10)
        self.assertIn('Last 2 matches', text)
        self.assertIn('**Wards Placed/min:** 0.35', text)
        self.assertIn('**Games Without a Control Ward:** 1/2', text)


if __name__ == '__main__':
    unittest.main()
