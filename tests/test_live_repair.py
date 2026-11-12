from concurrent.futures import ThreadPoolExecutor
import sys
import time
import unittest
from unittest.mock import patch

import check_sources
from live_repair import CHANNEL_NAMES, channel_key, repair_playlist
from live_validation import Channel, StreamValidator, parse_m3u


def playlist(*channels):
    return '#EXTM3U\n' + ''.join(f'#EXTINF:-1,{name}\n{url}\n' for name, url in channels)


class RepairTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(check_sources, 'discover_live_channels', return_value=([], []))
        patcher.start()
        self.addCleanup(patcher.stop)

    def validator(self):
        validator = StreamValidator()
        mocked = patch.object(validator, 'check', side_effect=lambda url, headers:
                              ('good' in url, 'test'))
        check = mocked.start()
        self.addCleanup(mocked.stop)
        return validator, check

    def test_normalizes_names_without_confusing_distinct_channels(self):
        for name in ['CCTV-1 综合', 'CCTV1高清', 'CCTV-01 HD (1080p)', '央视1综合']:
            self.assertEqual(channel_key(name), 'CCTV1')
        self.assertEqual(channel_key('浙江卫视超清'), channel_key('浙江卫视'))
        self.assertNotEqual(channel_key('CCTV1'), channel_key('CCTV10'))
        self.assertNotEqual(channel_key('CCTV5'), channel_key('CCTV5+'))
        self.assertEqual(channel_key('CCTV-5 体育赛事'), 'CCTV5+')
        self.assertNotEqual(channel_key('CCTV4'), channel_key('CCTV4K'))
        self.assertNotEqual(channel_key('CCTV4中文国际'), channel_key('CCTV-4 America'))
        self.assertNotEqual(channel_key('CCTV1'), channel_key('CCTV+ 1'))

    def test_regional_aliases_preserve_channel_and_edition_identity(self):
        for name, aliases in CHANNEL_NAMES.items():
            for alias in aliases:
                with self.subTest(alias=alias):
                    self.assertEqual(channel_key(alias), channel_key(name))
        for first, second in [('RTHK TV 31', 'RTHK TV 32'), ('DaAi 1', 'DaAi 2'),
                              ('NTD TV Canada', 'NTD TV Canada West'),
                              ('Phoenix Chinese Channel', 'Phoenix InfoNews Channel'),
                              ('TDM Sports', 'TDM Entertainment'),
                              ('beIN SPORTS XTRA', 'beIN SPORTS 1'),
                              ('MovieSphere CA', 'MovieSphere UK'), ('FIFA+', 'FIFA+ Women')]:
            with self.subTest(first=first, second=second):
                self.assertNotEqual(channel_key(first), channel_key(second))

    def test_regional_candidate_gets_chinese_name_and_requested_group(self):
        validator, _ = self.validator()
        candidates = [playlist(('RTHK TV 31 (1080p) [Geo-blocked]', 'https://example.com/good'))]
        result = repair_playlist('#EXTM3U\n', candidates, ['港澳中文|港台电视31'], validator)
        channel = parse_m3u(result)[1][0]
        self.assertEqual(channel.name, '港台电视31')
        self.assertIn('group-title="港澳中文"', channel.tags[0])

    def test_explicit_group_updates_working_channel_without_changing_url_or_headers(self):
        validator, check = self.validator()
        original = ('#EXTM3U\n#EXTINF:-1 tvg-id="epg" group-title="Old",beIN SPORTS XTRA\n'
                    '#EXTVLCOPT:http-user-agent=player\nhttps://example.com/good\n')
        result = repair_playlist(original, [], ['免费体育（外语）|beIN SPORTS XTRA'], validator)
        channel = parse_m3u(result)[1][0]
        self.assertEqual(channel.url, 'https://example.com/good')
        self.assertEqual(dict(channel.headers), {'User-Agent': 'player'})
        self.assertIn('tvg-id="epg"', channel.tags[0])
        self.assertIn('group-title="免费体育（外语）"', channel.tags[0])
        self.assertEqual(channel.tags[0].count('group-title='), 1)
        check.assert_called_once()

    def test_alias_targets_deduplicate_and_empty_group_is_rejected(self):
        validator, check = self.validator()
        result = repair_playlist('#EXTM3U\n', [playlist(('RTHK TV 31', 'https://example.com/good'))],
                                 ['港澳中文|港台电视31', '港澳中文|RTHK TV 31'], validator)
        self.assertEqual(len(parse_m3u(result)[1]), 1)
        check.assert_called_once()
        for specification in ['|CCTV1', '港澳中文|']:
            with self.subTest(specification=specification), self.assertRaises(ValueError):
                repair_playlist('#EXTM3U\n', [], [specification], validator)

    def test_failed_channel_gets_matching_replacement_and_missing_target_is_added(self):
        validator, _ = self.validator()
        current = playlist(('CCTV1综合', 'https://example.com/dead'))
        candidates = [playlist(('CCTV-1 HD (1080p)', 'https://example.com/good1'),
                               ('CCTV-2', 'https://example.com/good2'))]
        result = repair_playlist(current, candidates, ['CCTV1综合', 'CCTV2财经'], validator)
        channels = parse_m3u(result)[1]
        self.assertEqual([c.name for c in channels], ['CCTV1综合', 'CCTV2财经'])
        self.assertEqual([c.url for c in channels], ['https://example.com/good1',
                                                  'https://example.com/good2'])

    def test_keeps_existing_working_address_and_deduplicates_channel(self):
        validator, check = self.validator()
        current = playlist(('浙江卫视', 'https://example.com/good-original'),
                           ('浙江卫视高清', 'https://example.com/good-duplicate'))
        result = repair_playlist(current, [playlist(('浙江卫视', 'https://example.com/good-new'))],
                                 [], validator)
        self.assertEqual([c.url for c in parse_m3u(result)[1]], ['https://example.com/good-original'])
        check.assert_called_once()

    def test_does_not_substitute_different_channel_or_add_unrequested_channel(self):
        validator, check = self.validator()
        result = repair_playlist('#EXTM3U\n', [playlist(('CCTV-10', 'https://example.com/good10'),
                                                       ('CCTV5+', 'https://example.com/good5plus'))],
                                 ['CCTV1', 'CCTV5'], validator)
        self.assertIsNone(result)
        check.assert_not_called()

    def test_preserves_epg_identity_but_replaces_request_headers(self):
        validator, check = self.validator()
        current = ('#EXTM3U\n#EXTINF:-1 tvg-id="epg" group-title="News",CCTV1\n'
                   '#EXTVLCOPT:http-user-agent=old\nhttps://example.com/dead\n')
        candidate = ('#EXTM3U\n#EXTINF:-1,CCTV-1高清\n'
                     '#EXTVLCOPT:http-user-agent=new\nhttps://example.com/good\n')
        result = repair_playlist(current, [candidate], [], validator)
        channel = parse_m3u(result)[1][0]
        self.assertIn('tvg-id="epg"', channel.tags[0])
        self.assertEqual(dict(channel.headers), {'User-Agent': 'new'})
        self.assertEqual(dict(check.call_args[0][1]), {'User-Agent': 'new'})

    def test_replacement_budget_and_deduplication(self):
        validator, check = self.validator()
        candidates = [playlist(('CCTV1', 'https://example.com/dead1'),
                               ('CCTV1', 'https://example.com/dead1'),
                               ('CCTV1', 'https://example.com/dead2'),
                               ('CCTV1', 'https://example.com/good3'))]
        self.assertIsNone(repair_playlist('#EXTM3U\n', candidates, ['CCTV1'],
                                         validator, max_alternatives=2))
        self.assertEqual(check.call_count, 2)

    def test_new_channel_keeps_candidate_logo_and_uses_target_epg_name(self):
        validator, _ = self.validator()
        candidate = ('#EXTM3U\n#EXTINF:-1 tvg-logo="https://example.com/logo.png",CCTV-2\n'
                     'https://example.com/good\n')
        result = repair_playlist('#EXTM3U\n', [candidate], ['CCTV2财经'], validator)
        self.assertIn('tvg-logo="https://example.com/logo.png"', result)
        self.assertIn('tvg-id="CCTV2财经"', result)

    def test_full_scan_merges_replacements_without_losing_other_working_channels(self):
        from io import StringIO
        import json
        current = playlist(('CCTV2财经', 'https://example.com/good-current'))
        candidate = playlist(('CCTV-1 HD', 'https://example.com/good-new'))
        files = {'sources_pool.txt': 'https://example.com/config\n',
                 'config.json': json.dumps({'sites': []}), 'active_lives.m3u': current}

        def fetch(url):
            if url.endswith('/config'):
                return json.dumps({'lives': [{'url': 'https://example.com/list'}]}), 1
            self.assertEqual(url, 'https://example.com/list')
            return candidate, 1

        with patch('builtins.open', side_effect=lambda path, **kw: StringIO(files[path])), \
             patch.object(sys, 'argv', ['check_sources.py']), \
             patch.object(check_sources, 'run_vod_update', return_value={'live_urls': ['https://example.com/list']}), \
             patch.object(check_sources, 'fetch_text', side_effect=fetch), \
             patch.object(check_sources, 'load_live_candidates', return_value=[]), \
             patch.object(check_sources, 'read_lines', return_value=['CCTV1综合', 'CCTV2财经']), \
             patch.object(StreamValidator, 'check', return_value=(True, 'test')), \
             patch.object(check_sources, 'write_playlist') as write, \
             patch.object(check_sources, 'write_config') as config_write:
            check_sources.main()
        result = write.call_args[0][0]
        self.assertIn('https://example.com/good-current', result)
        self.assertIn('https://example.com/good-new', result)
        config_write.assert_not_called()

    def test_bad_upstream_list_does_not_prevent_loading_next_source(self):
        good = playlist(('CCTV1', 'https://example.com/good'))
        with patch.object(check_sources, 'read_lines', return_value=['first', 'second']), \
             patch.object(check_sources, 'fetch_text', side_effect=[('<html>error</html>', 1), (good, 1)]):
            self.assertEqual(check_sources.load_live_candidates('sources.txt'), [good])

    def test_concurrent_requests_share_one_playback_check(self):
        validator = StreamValidator()
        channel = Channel(('#EXTINF:-1,CCTV1',), 'https://example.com/stream')

        def check(url, headers):
            time.sleep(0.02)
            return True, 'test'

        with patch.object(validator, 'check', side_effect=check) as mocked:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(validator.verify, [channel] * 8))
        mocked.assert_called_once()
        self.assertTrue(all(result[0] for result in results))

    def test_repair_cli_and_dry_run(self):
        for dry in (False, True):
            with self.subTest(dry_run=dry):
                args = ['check_sources.py', '--repair-live'] + (['--dry-run'] if dry else [])
                with patch.object(sys, 'argv', args), \
                     patch.object(check_sources, 'read_current_playlist', return_value='#EXTM3U\n'), \
                     patch.object(check_sources, 'load_live_candidates', return_value=[
                         playlist(('CCTV1', 'https://example.com/good'))]), \
                     patch.object(check_sources, 'read_lines', return_value=['CCTV1']), \
                     patch.object(StreamValidator, 'check', return_value=(True, 'test')), \
                     patch.object(check_sources, 'write_playlist') as write, \
                     patch.object(check_sources, 'write_config') as config_write:
                    check_sources.main()
                config_write.assert_not_called()
                if dry:
                    write.assert_not_called()
                else:
                    self.assertIn('https://example.com/good', write.call_args[0][0])


if __name__ == '__main__':
    unittest.main()
