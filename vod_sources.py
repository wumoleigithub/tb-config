"""TVBox source inspection. Download dependencies for inspection, never execute them."""

from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import io
import json
import os
import re
import ssl
from threading import Lock
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from channel_policy import blocked_text, blocked_url


def encode_url(url):
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ''
    host = '[' + host + ']' if ':' in host else host.encode('idna').decode('ascii')
    if parts.port:
        host += ':' + str(parts.port)
    return urllib.parse.urlunsplit((parts.scheme, host,
        urllib.parse.quote(parts.path, safe='/%:@'),
        urllib.parse.quote(parts.query, safe='%=&?/:,+;$@'), ''))


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header('Authorization')
        return redirected


@dataclass
class Response:
    status: int = 0
    body: bytes = b''
    url: str = ''
    elapsed: float = 0
    error: str = ''


class Client:
    def __init__(self, timeout=15):
        self.timeout = timeout
        self.cache = {}
        self.lock = Lock()

    def get(self, url, limit=4_000_000):
        key = url, limit
        with self.lock:
            owner = key not in self.cache
            if owner:
                self.cache[key] = Future()
            future = self.cache[key]
        if owner:
            try:
                future.set_result(self._get(url, limit))
            except Exception as error:
                future.set_result(Response(url=url, error=str(error)))
        return future.result()

    def _get(self, url, limit):
        if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
            return Response(url=url, error='unsupported URL')
        headers = {'User-Agent': 'TVBox-source-checker/2', 'Accept': '*/*'}
        token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
        if token and urllib.parse.urlsplit(url).hostname == 'api.github.com':
            headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request(encode_url(url), headers=headers)
        opener = urllib.request.build_opener(SafeRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        start = time.monotonic()
        try:
            with opener.open(request, timeout=self.timeout) as response:
                chunks, size = [], 0
                while size <= limit:
                    if time.monotonic() - start > self.timeout:
                        return Response(response.status, url=response.url, error='download time budget exhausted')
                    chunk = response.read1(min(65_536, limit + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                body = b''.join(chunks)
                if len(body) > limit:
                    return Response(response.status, url=response.url, error='response too large')
                return Response(response.status, body, response.url, time.monotonic() - start)
        except urllib.error.HTTPError as error:
            return Response(error.code, url=url, elapsed=time.monotonic() - start, error=f'HTTP {error.code}')
        except (OSError, ValueError) as error:
            return Response(url=url, elapsed=time.monotonic() - start, error=str(error))


def parse_config(text):
    """Accept JSON/JSONC without changing comment-like strings inside URLs."""
    text = text.lstrip('\ufeff')
    output, index, quoted, escaped = [], 0, False, False
    while index < len(text):
        char = text[index]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            output.append(char)
        elif text.startswith('//', index):
            end = text.find('\n', index)
            index = len(text) if end < 0 else end
            output.append('\n')
            continue
        elif text.startswith('/*', index):
            end = text.find('*/', index + 2)
            if end < 0:
                raise ValueError('unterminated comment')
            index = end + 2
            output.append(' ')
            continue
        elif char == ',':
            following = text[index + 1:].lstrip()
            if not following.startswith(('}', ']')):
                output.append(char)
        else:
            output.append(char)
        index += 1
    # A second pass handles trailing commas followed by comments.
    cleaned = ''.join(output)
    if cleaned != text and ('//' in text or '/*' in text):
        return parse_config(cleaned)
    result = json.loads(cleaned)
    if not isinstance(result, dict):
        raise ValueError('configuration must be an object')
    return result


def resolve_reference(value, source_url):
    if not isinstance(value, str):
        return value
    path, separator, suffix = value.partition(';')
    if path.startswith(('./', '../', '/')):
        path = urllib.parse.urljoin(source_url, path)
    return path + (separator + suffix if separator else '')


def normalize_config(data, source_url):
    data = deepcopy(data)
    data['spider'] = resolve_reference(data.get('spider', ''), source_url)
    def relative_values(value):
        if isinstance(value, str):
            return resolve_reference(value, source_url)
        if isinstance(value, dict):
            return {k: relative_values(v) for k, v in value.items()}
        if isinstance(value, list):
            return [relative_values(v) for v in value]
        return value
    for field in ('sites', 'parses', 'lives'):
        if isinstance(data.get(field), list):
            for item in data[field]:
                if isinstance(item, dict):
                    for key in ('api', 'jar', 'ext', 'url'):
                        if key in item:
                            item[key] = relative_values(item[key])
    for field in ('wallpaper', 'logo'):
        if field in data:
            data[field] = resolve_reference(data[field], source_url)
    return data


def response_state(response):
    if response.status in (404, 410):
        return 'dead'
    if response.status == 200 and response.body and not response.error:
        return 'ok'
    return 'unknown'


def inspect_jar(raw, client):
    if not isinstance(raw, str) or not raw.startswith(('http://', 'https://')):
        return {'state': 'unknown', 'reason': 'JAR URL unavailable', 'classes': []}
    parts = raw.split(';')
    response = client.get(parts[0], limit=32_000_000)
    state = response_state(response)
    result = {'state': state, 'url': parts[0], 'reason': response.error, 'classes': []}
    if state != 'ok':
        return result
    digest = hashlib.md5(response.body).hexdigest()
    result['md5'] = digest
    if len(parts) >= 3 and parts[1].lower() == 'md5' and parts[2].strip().lower() != digest:
        return dict(result, state='invalid', reason='JAR MD5 mismatch')
    try:
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            classes = set()
            expanded = 0
            for info in archive.infolist():
                match = re.search(r'(?:^|/)com/github/catvod/spider/([^/]+)\.class$', info.filename)
                if match:
                    classes.add(match.group(1))
                if info.filename.endswith('.dex') and info.file_size <= 32_000_000:
                    expanded += info.file_size
                    if expanded > 64_000_000:
                        return dict(result, state='unknown', reason='DEX inspection size budget exhausted')
                    classes.update(c.decode('ascii') for c in re.findall(
                        rb'Lcom/github/catvod/spider/([A-Za-z0-9_$]+);', archive.read(info)))
            result['classes'] = sorted(classes)
            if not classes:
                result.update(state='unknown', reason='archive readable; spider classes cannot be identified')
    except (zipfile.BadZipFile, RuntimeError, OSError):
        result.update(state='invalid', reason='not a readable JAR/ZIP')
    return result


def inspect_api(url, client):
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query['ac'] = 'detail'
    target = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
    response = client.get(target)
    state = response_state(response)
    if state != 'ok':
        return {'state': state, 'reason': response.error}
    try:
        data = json.loads(response.body.decode('utf-8-sig'))
        rows = data.get('list') if isinstance(data, dict) else None
        if isinstance(rows, list) and any(isinstance(row, dict) and row.get('vod_id') for row in rows):
            return {'state': 'ok', 'reason': 'catalog API responds; playback not verified'}
    except (ValueError, UnicodeError):
        pass
    try:
        root = ET.fromstring(response.body)
        if root.find('.//video/id') is not None:
            return {'state': 'ok', 'reason': 'XML catalog responds; playback not verified'}
    except ET.ParseError:
        pass
    return {'state': 'invalid', 'reason': 'API did not return a movie catalog'}


def inspect_config(data, source_url, client, excluded=()):
    data = normalize_config(data, source_url)
    sites = data.get('sites')
    if not isinstance(sites, list):
        return {'state': 'invalid', 'reason': 'sites must be a list', 'sites': [], 'config': data, 'ready_count': 0}
    checked, valid_sites, jars, seen = [], [], {}, set()
    started = time.monotonic()
    api_checks = 0
    for site in sites:
        if not isinstance(site, dict) or not isinstance(site.get('key'), str) or not isinstance(site.get('api'), str):
            continue
        key, api = site['key'], site['api']
        if not key or not api or key in excluded or key in seen:
            continue
        if blocked_text(' '.join(str(site.get(k, '')) for k in ('key', 'name', 'api'))) or blocked_url(api):
            continue
        seen.add(key)
        kind = site.get('type', 3)
        if time.monotonic() - started > 60:
            check = {'state': 'unknown', 'reason': 'source inspection time budget exhausted'}
        elif kind == 3 and api.startswith('csp_'):
            jar = site.get('jar') or data.get('spider', '')
            if not isinstance(jar, str):
                jar = ''
            if jar not in jars:
                jars[jar] = (inspect_jar(jar, client) if len(jars) < 8 else
                             {'state': 'unknown', 'reason': 'JAR inspection budget exhausted', 'classes': []})
            dependency = jars[jar]
            check = {'state': dependency['state'], 'reason': dependency.get('reason', '')}
            if check['state'] == 'ok':
                if api[4:] not in dependency['classes']:
                    check = {'state': 'invalid', 'reason': 'spider class missing: ' + api[4:]}
                else:
                    check['reason'] = 'JAR and class verified; requires TVBox playback check'
        elif kind in (0, 1) and api.startswith(('http://', 'https://')):
            check = (inspect_api(api, client) if api_checks < 12 else
                     {'state': 'unknown', 'reason': 'catalog inspection budget exhausted'})
            api_checks += 1
        else:
            check = {'state': 'unknown', 'reason': 'client-specific JS/Python/plugin interface; not executed'}
        checked.append(dict(check, key=key, name=site.get('name', key), type=kind))
        if check['state'] == 'ok':
            valid_sites.append(site)
    ready = len(valid_sites)
    state = 'ready' if ready else 'unknown' if any(s['state'] == 'unknown' for s in checked) else 'invalid'
    return {'state': state, 'sites': checked, 'config': data, 'ready_sites': valid_sites,
            'ready_count': ready, 'site_count': len(checked),
            'jars': [{k: v for k, v in jar.items() if k != 'classes'} for jar in jars.values()]}


def inspect_source(url, client, excluded=()):
    response = client.get(url)
    if response_state(response) != 'ok':
        return {'url': url, 'state': response_state(response), 'http_status': response.status,
                'reason': response.error, 'ready_count': 0, 'site_count': 0}
    try:
        data = parse_config(response.body.decode('utf-8-sig'))
        result = inspect_config(data, response.url or url, client, excluded)
    except (ValueError, TypeError, UnicodeError):
        result = {'state': 'invalid', 'reason': 'invalid TVBox config', 'ready_count': 0, 'site_count': 0}
    return dict(result, url=url, http_status=response.status, elapsed=response.elapsed)
