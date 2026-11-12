import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import urllib.error
import zipfile

from discover_sources import discover_candidates
from vod_sources import Client, Response, inspect_config, inspect_source, normalize_config, parse_config, response_state
from vod_update import choose_bundle, run_vod_update, update_pool


def jar_bytes(*classes):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as jar:
        jar.writestr('classes.dex', b' '.join(b'Lcom/github/catvod/spider/' + name.encode() + b';' for name in classes))
    return output.getvalue()


class FakeClient:
    def __init__(self, responses):
        self.responses = responses

    def get(self, url, **kwargs):
        return self.responses.get(url, Response(404, url=url, error='HTTP 404'))


class VodTests(unittest.TestCase):
    def setUp(self):
        self.jar = jar_bytes('Movie', 'PushAgent')
        self.client = FakeClient({'https://example.com/jar/plugin.jar': Response(200, self.jar)})
        self.config = {'spider': './jar/plugin.jar;md5;' + hashlib.md5(self.jar).hexdigest(),
                       'sites': [{'key': 'movie', 'type': 3, 'api': 'csp_Movie', 'ext': './rules.json'}]}
        self.url = 'https://example.com/config.json'

    def test_jsonc_preserves_urls_and_comment_strings(self):
        parsed = parse_config('\ufeff{/* hello */"url":"https://example.com/a//b", "value":"/* x */", //comment\n "array":[1, /* trailing */],}')
        self.assertEqual(parsed, {'url': 'https://example.com/a//b', 'value': '/* x */', 'array': [1]})
        with self.assertRaises(ValueError):
            parse_config('[1]')

    def test_relative_dependencies_and_checksum(self):
        config = normalize_config(self.config, self.url)
        self.assertTrue(config['spider'].startswith('https://example.com/jar/'))
        self.assertEqual(config['sites'][0]['ext'], 'https://example.com/rules.json')
        result = inspect_config(self.config, self.url, self.client)
        self.assertEqual(result['ready_count'], 1)
        self.config['spider'] = './jar/plugin.jar;md5;0000'
        self.assertEqual(inspect_config(self.config, self.url, self.client)['state'], 'invalid')

    def test_missing_class_malformed_site_and_exclusion(self):
        self.config['sites'] += [None, {'key': ['bad'], 'api': 5},
            {'key': 'missing', 'api': 'csp_NotInJar', 'type': 3},
            {'key': 'malformed', 'api': 'csp_Movie', 'jar': {'bad': 'shape'}}]
        result = inspect_config(self.config, self.url, self.client, ['movie'])
        self.assertEqual(result['ready_count'], 0)
        self.assertEqual([row['state'] for row in result['sites']], ['invalid', 'unknown'])

    def test_http_error_is_preserved_and_temporary_failures_not_dead(self):
        client = Client()
        error = urllib.error.HTTPError(self.url, 404, 'missing', {}, None)
        with patch('vod_sources.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = error
            response = client.get(self.url)
        self.assertEqual(response.status, 404)
        self.assertEqual(response_state(response), 'dead')
        for status in (0, 403, 405, 429, 500, 503):
            self.assertEqual(response_state(Response(status)), 'unknown')

    def test_download_size_and_total_time_are_bounded(self):
        response = MagicMock(status=200, url=self.url)
        response.__enter__.return_value = response
        response.read1.side_effect = [b'12345']
        with patch('vod_sources.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            result = Client().get(self.url, limit=4)
        self.assertEqual(result.error, 'response too large')
        with patch('vod_sources.urllib.request.build_opener') as opener, \
                patch('vod_sources.time.monotonic', side_effect=[0, 16]):
            opener.return_value.open.return_value = response
            result = Client(timeout=15).get(self.url)
        self.assertIn('time budget', result.error)

    def test_html_200_is_not_a_config_or_jar(self):
        client = FakeClient({self.url: Response(200, b'<html>error</html>'),
                            'https://example.com/jar/plugin.jar': Response(200, b'<html>error</html>')})
        self.assertEqual(inspect_source(self.url, client)['state'], 'invalid')
        self.assertEqual(inspect_config(self.config, self.url, client)['state'], 'invalid')

    def test_catalog_does_not_claim_playback_verified(self):
        config = {'sites': [{'key': 'api', 'type': 1, 'api': 'https://example.com/api?token=test'}]}
        client = FakeClient({'https://example.com/api?token=test&ac=detail': Response(200, b'{"list":[{"vod_id":1}]}')})
        result = inspect_config(config, self.url, client)
        self.assertEqual(result['ready_count'], 1)
        self.assertIn('playback not verified', result['sites'][0]['reason'])

    def test_vod_policy_blocks_aliases_and_keeps_other_sites(self):
        self.config['sites'].append({'key': 'ntd', 'name': '新唐人', 'api': 'csp_Movie'})
        result = inspect_config(self.config, self.url, self.client)
        self.assertEqual([s['key'] for s in result['ready_sites']], ['movie'])

    def test_inspection_time_budget_marks_remaining_sites_unknown(self):
        with patch('vod_sources.time.monotonic', side_effect=[0, 61]):
            result = inspect_config(self.config, self.url, self.client)
        self.assertEqual(result['state'], 'unknown')
        self.assertIn('budget', result['sites'][0]['reason'])

    def test_partial_unknown_refresh_does_not_shrink_verified_config(self):
        candidate = inspect_config(self.config, self.url, self.client)
        candidate['url'] = self.url
        candidate['sites'].append({'state': 'unknown'})
        selected, reason = choose_bundle({'_source_url': self.url}, {'ready_count': 2}, [candidate])
        self.assertIsNone(selected)
        self.assertIn('partially', reason)

    def test_same_host_different_jar_does_not_merge(self):
        current = {'spider': 'https://example.com/old.jar', 'sites': [{'key': 'old'}]}
        candidate = inspect_config(self.config, self.url, self.client)
        candidate['url'] = self.url
        selected, _ = choose_bundle(current, {'ready_count': 1}, [candidate])
        self.assertIsNone(selected)

    def test_refreshes_parameters_and_parsers_as_bundle_preserving_lives(self):
        current = {'_source_url': self.url, 'spider': 'https://example.com/old.jar',
                   'sites': [{'key': 'movie', 'ext': 'old'}], 'lives': [{'url': 'local'}], 'rules': ['old']}
        self.config['parses'] = [{'url': './parse', 'name': 'new'}]
        self.config['rules'] = ['new']
        candidate = inspect_config(self.config, self.url, self.client)
        candidate['url'] = self.url
        updated, _ = choose_bundle(current, {'ready_count': 1}, [candidate])
        self.assertEqual(updated['sites'][0]['ext'], 'https://example.com/rules.json')
        self.assertEqual(updated['parses'][0]['url'], 'https://example.com/parse')
        self.assertEqual(updated['rules'], ['new'])
        self.assertEqual(updated['lives'], current['lives'])

    def test_pool_requires_repeated_404_and_revives_recovered_source(self):
        rows = ['# manual', self.url, '#dead https://example.com/recovered', '#disabled https://example.com/skip']
        results = [{'url': self.url, 'http_status': 404, 'state': 'dead'},
                   {'url': 'https://example.com/recovered', 'http_status': 200, 'state': 'ready'}]
        text, history, _ = update_pool(rows, results, {})
        self.assertNotIn('#dead ' + self.url, text)
        text, _, _ = update_pool(text.splitlines(), results, history)
        self.assertIn('#dead ' + self.url, text)
        self.assertNotIn('#dead https://example.com/recovered', text)
        self.assertIn('#disabled https://example.com/skip', text)

    def test_dry_run_and_atomic_backup_on_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = {'spider': 'https://offline.test/old.jar', 'sites': [{'key': 'old', 'api': 'csp_Old'}], 'lives': []}
            (root / 'config.json').write_text(json.dumps(current))
            (root / 'sources_pool.txt').write_text(self.url + '\n')
            self.client.responses[self.url] = Response(200, json.dumps(self.config).encode(), self.url)
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            report = run_vod_update(root=root, client=self.client, discover=False, dry_run=True)
            self.assertTrue(report['config_changed'])
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
            run_vod_update(root=root, client=self.client, discover=False)
            self.assertEqual(json.loads((root / 'config.previous.json').read_text()), current)
            self.assertEqual(json.loads((root / 'config.json').read_text())['_source_url'], self.url)

    def test_discovery_failure_preserves_pool(self):
        candidates, warnings = discover_candidates(FakeClient({}), limit=5)
        self.assertEqual(candidates, [])
        self.assertTrue(warnings)

    def test_discovery_finds_nested_config_and_obeys_limit(self):
        client = FakeClient({
            'https://api.github.com/repos/owner/repo': Response(200, b'{"default_branch":"branch"}'),
            'https://api.github.com/repos/owner/repo/git/trees/branch?recursive=1': Response(200, b'{"tree":[{"type":"blob","path":"nested/config.json"}]}')})
        with patch('discover_sources.KNOWN_REPOS', [('owner/repo', [])]):
            candidates, _ = discover_candidates(client, limit=1)
        self.assertEqual(candidates, ['https://raw.githubusercontent.com/owner/repo/branch/nested/config.json'])


if __name__ == '__main__':
    unittest.main()
