"""User-requested channel exclusions, shared by discovery and playback checks."""

import re
import unicodedata
from urllib.parse import urlsplit


_NAMES = re.compile(
    r"新[唐塘]人|法[轮輪](?:功|大法)|大[纪紀]元|[干乾][净淨]世界|"
    r"希望之[声聲]|看中[国國]|^神[韵韻]|"
    r"(?<![a-z0-9])ntd(?=tv|news|television|asia|china|canada|west|east|global|\d|\b)|"
    r"new[\s_-]*tang[\s_-]*dynasty|falun[\s_-]*(?:gong|dafa)|"
    r"(?:the[\s_-]*)?epoch[\s_-]*(?:times|tv|media)|"
    r"gan[\s_-]*jing[\s_-]*world|shen[\s_-]*yun|"
    r"sound[\s_-]*of[\s_-]*hope|vision[\s_-]*times",
    re.IGNORECASE,
)
_DOMAINS = (
    'ntd.com', 'ntdtv.com', 'ntdtv.org', 'epochtimes.com', 'theepochtimes.com',
    'epochtv.com', 'epoch.cloud', 'ganjingworld.com', 'ganjing.com', 'ganjing.world',
    'shenyun.com', 'shenyuncreations.com', 'falundafa.org', 'falungong.org',
    'soundofhope.org', 'visiontimes.com',
)


def blocked_text(value):
    return isinstance(value, str) and bool(_NAMES.search(unicodedata.normalize('NFKC', value)))


def blocked_url(url):
    try:
        host = (urlsplit(url).hostname or '').lower().rstrip('.')
    except (ValueError, TypeError, AttributeError):
        return False
    return (any(host == domain or host.endswith('.' + domain) for domain in _DOMAINS)
            or bool(re.fullmatch(r'ntd\d*\.akamaized\.net', host)))


def blocked_channel(channel):
    if blocked_url(channel.url):
        return True
    if not channel.tags:
        return False
    names = [channel.name]
    names += re.findall(r'\btvg-(?:id|name)="([^"]*)"', channel.tags[0])
    return any(blocked_text(name) for name in names)


def blocked_metadata(meta):
    values = [meta.get('id'), meta.get('name'), meta.get('network')]
    for field in ('alt_names', 'owners'):
        value = meta.get(field) or []
        values.extend(value if isinstance(value, list) else [value])
    return any(blocked_text(value) for value in values) or blocked_url(meta.get('website'))
