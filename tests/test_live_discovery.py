from argparse import Namespace
import json
import unittest
from unittest.mock import patch

import check_sources
from live_discovery import CATALOG_URL, build_discovery, discover_live_channels
from live_validation import StreamValidator, parse_m3u


def catalog(*entries):
    return '#EXTM3U\n' + ''.join(
        f'#EXTINF:-1 tvg-id="{identity}",{identity}\n{url}\n' for identity, url in entries)


def metadata(identity, name, country='TW', categories=None, **extra):
    return dict(id=identity, name=name, country=country, categories=categories or [],
                alt_names=[], is_nsfw=False, closed=None, **extra)


def feed(identity, languages=None, name='Main', main=True):
    return dict(channel=identity, id=name, languages=languages or ['zho'], is_main=main)


class DiscoveryTests(unittest.TestCase):
    def test_new_channel_is_discovered_without_manual_target(self):
        targets, candidates = build_discovery(
            catalog(('NewTV.tw', 'https://example.com/live.m3u8')),
            [metadata('NewTV.tw', 'A Newly Added Channel')], [feed('NewTV.tw')], [])
        self.assertEqual(targets, ['台湾中文|A Newly Added Channel'])
        channel = parse_m3u(candidates[0])[1][0]
        self.assertEqual(channel.name, 'A Newly Added Channel')
        self.assertEqual(channel.url, 'https://example.com/live.m3u8')

    def test_only_free_catalog_members_are_discovered(self):
        targets, _ = build_discovery(
            catalog(('Free.tw', 'https://example.com/free')),
            [metadata('Free.tw', 'Free Channel'), metadata('Other.tw', 'Other Channel')],
            [feed('Free.tw'), feed('Other.tw')],
            [{'channel': 'Other.tw', 'url': 'https://example.com/other'}])
        self.assertEqual(targets, ['台湾中文|Free Channel'])

    def test_mainland_news_foreign_language_closed_and_adult_are_excluded(self):
        items = [metadata('CN.cn', 'Mainland News', 'CN'),
                 metadata('EN.tw', 'English News'), metadata('Closed.tw', 'Closed'),
                 metadata('Adult.tw', 'Adult')]
        items[2]['closed'] = '2020-01-01'
        items[3]['is_nsfw'] = True
        targets, candidates = build_discovery(
            catalog(*[(item['id'], 'https://example.com/' + item['id']) for item in items]),
            items, [feed('CN.cn'), feed('EN.tw', ['eng', 'zho']), feed('Closed.tw'), feed('Adult.tw')], [])
        self.assertEqual((targets, candidates), ([], []))

    def test_english_sports_and_movies_are_interleaved_under_limit(self):
        items = [metadata('News.tw', 'New TV'), metadata('Sport.us', 'Sport', 'US', ['sports']),
                 metadata('Movie.us', 'Movie', 'US', ['movies']),
                 metadata('Sport2.us', 'Sport2', 'US', ['sports'])]
        targets, _ = build_discovery(
            catalog(*[(item['id'], 'https://example.com/' + item['id']) for item in items]),
            items, [feed(item['id'], ['zho'] if item['country'] == 'TW' else ['eng']) for item in items],
            [], limit=3)
        self.assertEqual(targets, ['台湾中文|New TV', '免费体育（外语）|Sport', '免费电影（外语）|Movie'])

    def test_exact_feed_identity_and_headers_are_preserved(self):
        targets, candidates = build_discovery(
            catalog(('News.tw@West', 'https://www.youtube.com/watch?v=channel')),
            [metadata('News.tw', 'Regional TV')],
            [feed('News.tw', name='West', main=False), feed('News.tw', name='East')],
            [{'channel': 'News.tw', 'feed': 'West', 'url': 'https://example.com/west',
              'user_agent': 'Player', 'referrer': 'https://example.com/'},
             {'channel': 'News.tw', 'feed': 'East', 'url': 'https://example.com/east'},
             {'channel': 'Wrong.tw', 'url': 'https://example.com/wrong'}])
        self.assertEqual(len(targets), 1)
        channels = parse_m3u(candidates[0])[1]
        self.assertEqual([c.url for c in channels], ['https://example.com/west'])
        self.assertEqual(dict(channels[0].headers), {'User-Agent': 'Player', 'Referer': 'https://example.com/'})

    def test_same_name_different_catalog_id_is_not_merged(self):
        targets, candidates = build_discovery(
            catalog(('First.tw', 'https://example.com/first'), ('Second.hk', 'https://example.com/second')),
            [metadata('First.tw', 'Same TV'), metadata('Second.hk', 'Same TV', 'HK')],
            [feed('First.tw'), feed('Second.hk')], [])
        self.assertEqual(len(targets), 1)
        self.assertNotIn('https://example.com/second', candidates[0])

    def test_zero_limit_does_not_fetch_and_unavailable_api_is_nonfatal(self):
        def unavailable(url, timeout=30):
            return '<html>error</html>', 0

        with patch('builtins.print'):
            self.assertEqual(discover_live_channels(unavailable), ([], []))
        with patch('check_sources.fetch_text') as fetch:
            self.assertEqual(discover_live_channels(fetch, limit=0), ([], []))
            fetch.assert_not_called()

    def test_stream_api_failure_still_uses_free_catalog_url(self):
        def fetch(url, timeout=30):
            if url == CATALOG_URL:
                return catalog(('New.tw', 'https://example.com/new')), 1
            if url.endswith('channels.json'):
                return json.dumps([metadata('New.tw', 'New Channel')]), 1
            if url.endswith('feeds.json'):
                return json.dumps([feed('New.tw')]), 1
            return None, 0

        targets, candidates = discover_live_channels(fetch)
        self.assertEqual(targets, ['台湾中文|New Channel'])
        self.assertIn('https://example.com/new', candidates[0])

    def test_existing_favourite_group_is_not_overridden_by_discovery(self):
        args = Namespace(preferred_channels='preferred', live_sources='sources',
                         no_discovery=False, discovery_limit=40, max_alternatives=8)
        current = '#EXTM3U\n#EXTINF:-1 group-title="央视频道",CCTV6电影\nhttps://example.com/live\n'
        validator = StreamValidator()
        with patch.object(check_sources, 'read_lines', return_value=['CCTV6电影']), \
             patch.object(check_sources, 'load_live_candidates', return_value=[]), \
             patch.object(check_sources, 'discover_live_channels', return_value=(['电影频道|CCTV-6'], [])), \
             patch.object(validator, 'check', return_value=(True, 'test')):
            result = check_sources.prepare_live_repair(args, current, validator)
        self.assertIn('group-title="央视频道"', result)
        self.assertNotIn('group-title="电影频道"', result)

    def test_discovery_can_be_disabled(self):
        args = Namespace(preferred_channels='preferred', live_sources='sources',
                         no_discovery=True, discovery_limit=40, max_alternatives=8)
        validator = StreamValidator()
        with patch.object(check_sources, 'read_lines', return_value=[]), \
             patch.object(check_sources, 'load_live_candidates', return_value=[]), \
             patch.object(check_sources, 'discover_live_channels') as discover:
            self.assertIsNone(check_sources.prepare_live_repair(args, '#EXTM3U\n', validator))
        discover.assert_not_called()


if __name__ == '__main__':
    unittest.main()
