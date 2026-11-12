import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import check_sources
from live_validation import StreamValidator, parse_m3u


class PlaylistTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(check_sources, 'discover_live_channels', return_value=([], []))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_metadata_headers_and_malformed_entries(self):
        header, channels = parse_m3u(
            '\ufeff#EXTM3U x-tvg-url="epg.xml"\n'
            '#EXTINF:-1,missing URL\n'
            '#EXTINF:-1 group-title="News",News\n'
            '#EXTVLCOPT:http-user-agent=Example Player\n'
            '#EXTVLCOPT:http-referrer=https://example.com/\n'
            '\nhttps://example.com/live\n#EXTINF:-1,incomplete\n')
        self.assertIn('x-tvg-url=', header)
        self.assertEqual(len(channels), 1)
        self.assertEqual(dict(channels[0].headers), {
            'User-Agent': 'Example Player', 'Referer': 'https://example.com/'})

    def test_filters_failures_preserves_order_and_reuses_results(self):
        validator = StreamValidator()
        content = ('#EXTM3U\n#EXTINF:-1,one\nhttps://example.com/good\n'
                   '#EXTINF:-1,bad\nhttps://example.com/bad\n'
                   '#EXTINF:-1,another name\nhttps://example.com/good\n')
        with patch.object(validator, 'check', side_effect=lambda url, headers:
                          (url.endswith('/good'), 'test')) as check:
            result = validator.filter_m3u(content)
            validator.filter_m3u(content)
        self.assertEqual(check.call_count, 2)
        self.assertNotIn(',bad', result)
        self.assertLess(result.index(',one'), result.index(',another name'))

    def test_unsupported_protocol_and_timeout(self):
        validator = StreamValidator()
        with patch('live_validation.subprocess.run') as run:
            self.assertFalse(validator.check('mitv://example.com/channel')[0])
            run.assert_not_called()
            run.side_effect = subprocess.TimeoutExpired('ffmpeg', 30)
            self.assertFalse(validator.check('https://example.com/live')[0])

    def test_missing_ffmpeg_aborts(self):
        with patch('live_validation.shutil.which', return_value=None):
            with self.assertRaises(RuntimeError):
                StreamValidator()

    def test_live_only_cleans_existing_playlist_and_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            playlist = Path(directory) / 'active_lives.m3u'
            original = '#EXTM3U\n#EXTINF:-1,dead\nhttps://example.com/dead\n'
            playlist.write_text(original)
            real_open = open

            def redirected_open(path, *args, **kwargs):
                self.assertEqual(path, 'active_lives.m3u')
                return real_open(playlist, *args, **kwargs)

            with patch('builtins.open', side_effect=redirected_open), \
                 patch.object(StreamValidator, 'check', return_value=(False, '404')):
                with patch.object(sys, 'argv', ['check_sources.py', '--live-only', '--dry-run']):
                    check_sources.main()
                self.assertEqual(playlist.read_text(), original)
                with patch.object(sys, 'argv', ['check_sources.py', '--live-only']):
                    check_sources.main()
                self.assertEqual(playlist.read_text(), '#EXTM3U\n')

    def test_full_scan_cleans_existing_list_without_candidates(self):
        config = {'spider': 'https://example.com/spider.jar', 'sites': []}
        original = '#EXTM3U\n#EXTINF:-1,dead\nhttps://example.com/dead\n'
        from io import StringIO
        files = {'sources_pool.txt': '', 'config.json': json.dumps(config),
                 'active_lives.m3u': original}
        with patch('builtins.open', side_effect=lambda path, **kw: StringIO(files[path])), \
             patch.object(sys, 'argv', ['check_sources.py']), \
             patch.object(StreamValidator, 'check', return_value=(False, '404')), \
             patch.object(check_sources, 'run_vod_update', return_value={'live_urls': []}), \
             patch.object(check_sources, 'load_live_candidates', return_value=[]), \
             patch.object(check_sources, 'read_lines', return_value=[]), \
             patch.object(check_sources, 'write_playlist') as write:
            check_sources.main()
        write.assert_called_once_with(None)

    def test_full_dry_run_validates_existing_channels_without_writing(self):
        from io import StringIO
        files = {'sources_pool.txt': '', 'config.json': json.dumps({'sites': []}),
                 'active_lives.m3u': '#EXTM3U\n#EXTINF:-1,dead\nhttps://example.com/dead\n'}
        with patch('builtins.open', side_effect=lambda path, **kw: StringIO(files[path])), \
             patch.object(sys, 'argv', ['check_sources.py', '--dry-run']), \
             patch.object(StreamValidator, 'check', return_value=(False, '404')) as check, \
             patch.object(check_sources, 'run_vod_update', return_value={'live_urls': []}), \
             patch.object(check_sources, 'load_live_candidates', return_value=[]), \
             patch.object(check_sources, 'read_lines', return_value=[]), \
             patch.object(check_sources, 'write_playlist') as write:
            check_sources.main()
        check.assert_called_once()
        write.assert_not_called()


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/protected.ts':
            if (self.headers.get('User-Agent') != 'Example Player' or
                    self.headers.get('Referer') != 'https://example.com/'):
                self.send_error(403)
                return
            self.path = '/good.ts'
        super().do_GET()


class PlaybackTests(unittest.TestCase):
    """Use real FFmpeg and local HTTP fixtures, without external streams."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        subprocess.run([
            'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=64x64:rate=25',
            '-t', '2', '-c:v', 'mpeg2video', '-f', 'mpegts', str(root / 'good.ts'),
        ], check=True, capture_output=True, timeout=15)
        (root / 'error.html').write_text('<html>Not a stream</html>')
        (root / 'good.m3u8').write_text(
            '#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\ngood.ts\n#EXT-X-ENDLIST\n')
        (root / 'broken.m3u8').write_text(
            '#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nmissing.ts\n#EXT-X-ENDLIST\n')
        cls.server = ThreadingHTTPServer(
            ('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(root)))
        cls.addClassCleanup(cls.server.server_close)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.server.shutdown)
        cls.base = f'http://127.0.0.1:{cls.server.server_port}/'

    def test_actual_decoding_rejects_http_errors_html_and_missing_segments(self):
        validator = StreamValidator(timeout=8, sample_seconds=1)
        for path, valid in [('good.ts', True), ('good.m3u8', True), ('missing.ts', False),
                            ('error.html', False), ('broken.m3u8', False)]:
            with self.subTest(path=path):
                self.assertEqual(validator.check(self.base + path)[0], valid)

    def test_clip_too_short_is_not_a_pass(self):
        validator = StreamValidator(timeout=8, sample_seconds=5)
        self.assertFalse(validator.check(self.base + 'good.ts')[0])

    def test_channel_request_headers_are_used(self):
        validator = StreamValidator(timeout=8, sample_seconds=1)
        self.assertFalse(validator.check(self.base + 'protected.ts')[0])
        self.assertTrue(validator.check(self.base + 'protected.ts', (
            ('User-Agent', 'Example Player'), ('Referer', 'https://example.com/')))[0])


if __name__ == '__main__':
    unittest.main()
