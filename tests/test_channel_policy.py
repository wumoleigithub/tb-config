import unittest
from unittest.mock import patch

from channel_policy import blocked_channel, blocked_metadata, blocked_text, blocked_url
from live_discovery import build_discovery
from live_repair import repair_playlist
from live_validation import Channel, StreamValidator, parse_m3u


class ChannelPolicyTests(unittest.TestCase):
    def test_names_ids_and_domains(self):
        for name in ['新唐人全球', '新塘人', 'ＮＴＤ TV', 'NTDTVAsiaPacific.tw@HD',
                     'New Tang Dynasty Television', '法輪大法', '法轮功', 'Falun Gong',
                     'The Epoch Times', 'EpochTV', 'Gan Jing World', 'Shen Yun']:
            with self.subTest(name=name):
                self.assertTrue(blocked_text(name))
        for url in ['https://live.ntdtv.com/live.m3u8',
                    'https://ntd02.akamaized.net/HD-NY/index.m3u8',
                    'https://www.theepochtimes.com/live']:
            self.assertTrue(blocked_url(url))
        channel = Channel(('#EXTINF:-1 tvg-id="NTDTVCanada.ca",Generic TV',),
                          'https://example.com/live')
        self.assertTrue(blocked_channel(channel))

    def test_unrelated_channels_and_shared_cdns_remain_allowed(self):
        for name in ['CCTV1综合', '港台电视31', '凤凰资讯', '人间卫视', '大爱一台',
                     'Epoch Cinema', '文艺神韵', 'CountDown TV']:
            self.assertFalse(blocked_text(name), name)
        for url in ['https://rthktv31-live.akamaized.net/live.m3u8',
                    'https://notntdtv.com/live.m3u8', 'https://example.com/live?token=NTD']:
            self.assertFalse(blocked_url(url), url)

    def test_filter_blocks_before_playback_even_if_url_is_cached_as_valid(self):
        validator = StreamValidator()
        validator.cache['https://example.com/shared', ()] = True, 'cached'
        playlist = ('#EXTM3U\n#EXTINF:-1,新唐人全球\nhttps://example.com/shared\n'
                    '#EXTINF:-1,CCTV1\nhttps://example.com/shared\n')
        with patch.object(validator, 'check') as check:
            result = validator.filter_m3u(playlist)
        check.assert_not_called()
        self.assertEqual([c.name for c in parse_m3u(result)[1]], ['CCTV1'])

    def test_repair_drops_existing_blocked_channels_and_rejects_replacements(self):
        validator = StreamValidator()
        current = '#EXTM3U\n#EXTINF:-1,NTD TV\nhttps://example.com/ntd\n'
        candidates = ('#EXTM3U\n#EXTINF:-1,CCTV1\nhttps://ntd02.akamaized.net/stream\n'
                      '#EXTINF:-1,NTD TV\nhttps://example.com/ntd\n')
        with patch.object(validator, 'check', return_value=(True, 'test')) as check:
            result = repair_playlist(current, [candidates], ['NTD TV', 'CCTV1'], validator)
        self.assertIsNone(result)
        check.assert_not_called()

    def test_discovery_checks_owners_aliases_and_alternative_domains(self):
        self.assertTrue(blocked_metadata({'name': 'Generic', 'owners': ['Epoch Media Group']}))
        self.assertTrue(blocked_metadata({'name': 'Generic', 'alt_names': ['新唐人']}))
        catalog = ('#EXTM3U\n#EXTINF:-1 tvg-id="Hidden.tw",Generic\nhttps://example.com/live\n'
                   '#EXTINF:-1 tvg-id="Normal.tw",Normal\nhttps://example.com/normal\n')
        metadata = [dict(id='Hidden.tw', name='Generic', country='TW', owners=['Epoch Media Group']),
                    dict(id='Normal.tw', name='Normal', country='TW')]
        feeds = [dict(channel=item['id'], id='Main', is_main=True, languages=['zho']) for item in metadata]
        streams = [dict(channel='Normal.tw', feed='Main', url='https://live.ntdtv.com/live')]
        targets, candidates = build_discovery(catalog, metadata, feeds, streams)
        self.assertEqual(targets, ['台湾中文|Normal'])
        self.assertEqual([c.url for c in parse_m3u(candidates[0])[1]], ['https://example.com/normal'])


if __name__ == '__main__':
    unittest.main()
