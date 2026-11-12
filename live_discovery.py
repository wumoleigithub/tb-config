"""Discover new channels from online free-TV and broadcast metadata catalogs."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
import re
from urllib.parse import urlsplit

from live_repair import CHANNEL_NAMES, channel_key
from live_validation import Channel, parse_m3u
from channel_policy import blocked_channel, blocked_metadata


CATALOG_URL = "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"
API_URL = "https://iptv-org.github.io/api/"
CHINESE = {"zho", "cmn", "yue", "nan", "hak"}


def attribute(channel, name):
    match = re.search(rf'\b{re.escape(name)}="([^"]*)"', channel.tags[0])
    return match.group(1) if match else ""


def _safe_text(value):
    return str(value).replace('"', "'").replace("\r", " ").replace("\n", " ").replace("|", " ")


def _direct_url(url):
    if not isinstance(url, str) or any(c in url for c in "\r\n"):
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme in {"http", "https"} and bool(host) and
                not any(host == domain or host.endswith('.' + domain)
                        for domain in ("youtube.com", "youtu.be", "twitch.tv", "dailymotion.com")))
    except ValueError:
        return False


def build_discovery(catalog, metadata, feeds, streams, limit=40):
    """Use free-catalog membership, metadata language/category, and exact IDs.

    Only named channels in the free-TV catalog are discovered. The general stream
    API supplies alternatives for those exact IDs, never an unrelated channel.
    Regional feeds are not combined with a channel's main feed.
    """
    if limit < 0:
        raise ValueError("discovery-limit 不能小于 0")
    if not limit:
        return [], []
    by_id = {c['id']: c for c in metadata if isinstance(c.get('id'), str)}
    feed_index = defaultdict(list)
    for feed in feeds:
        feed_index[feed.get('channel')].append(feed)
    stream_index = defaultdict(list)
    for stream in streams:
        stream_index[stream.get('channel')].append(stream)
    discovered = {}
    for original in parse_m3u(catalog)[1]:
        channel_id, _, feed_id = attribute(original, 'tvg-id').partition('@')
        meta = by_id.get(channel_id)
        if (not meta or meta.get('is_nsfw') or meta.get('closed')
                or blocked_metadata(meta) or blocked_channel(original)):
            continue
        channel_feeds = feed_index[channel_id]
        matching_feeds = ([f for f in channel_feeds if f.get('id') == feed_id] if feed_id
                          else [f for f in channel_feeds if f.get('is_main')])
        # Unknown/ambiguous feed metadata cannot establish the language reliably.
        if not matching_feeds:
            continue
        languages = {language for feed in matching_feeds for language in (feed.get('languages') or [])}
        categories = set(meta.get('categories') or [])
        country = meta.get('country')
        chinese_primary = any((feed.get('languages') or [None])[0] in CHINESE
                              for feed in matching_feeds)
        if chinese_primary and country != 'CN':
            group = ('港澳中文' if country in {'HK', 'MO'} else
                     '台湾中文' if country == 'TW' else '海外中文')
            rank = 0
        elif 'sports' in categories and languages & (CHINESE | {'eng'}):
            group = '体育频道' if languages & CHINESE else '免费体育（外语）'
            rank = 1
        elif 'movies' in categories and languages & (CHINESE | {'eng'}):
            group = '电影频道' if languages & CHINESE else '免费电影（外语）'
            rank = 2
        else:
            continue
        chinese_name = next((name for name in (meta.get('alt_names') or [])
                             if isinstance(name, str) and re.search(r'[\u3400-\u9fff]', name)), None)
        canonical = meta.get('name') or original.name
        known_name = next((name for name in CHANNEL_NAMES
                           if channel_key(name) == channel_key(canonical)), None)
        name = _safe_text(known_name or chinese_name or canonical)
        key = channel_key(name)
        identity = channel_id, feed_id
        # Same spelling does not establish that two catalog IDs are one channel.
        if key in discovered and discovered[key]['identity'] != identity:
            continue
        allowed_feeds = {f.get('id') for f in matching_feeds}
        alternatives = []
        for stream in stream_index[channel_id]:
            if stream.get('feed') and stream['feed'] not in allowed_feeds:
                continue
            tags = []
            for field, option in [('user_agent', 'http-user-agent'), ('referrer', 'http-referrer')]:
                value = stream.get(field)
                if isinstance(value, str) and not any(c in value for c in "\r\n"):
                    tags.append(f'#EXTVLCOPT:{option}={value}')
            alternatives.append(Channel(tuple(tags), stream.get('url')))
        logo = _safe_text(attribute(original, 'tvg-logo'))
        tag = (f'#EXTINF:-1 tvg-id="{_safe_text(channel_id)}" tvg-name="{name}"'
               f' tvg-logo="{logo}" group-title="{group}",{name}')
        options = discovered.setdefault(key, {
            'identity': identity, 'name': name, 'group': group, 'rank': rank, 'channels': {},
        })['channels']
        for channel in [original, *alternatives]:
            if _direct_url(channel.url):
                directives = channel.tags[1:] if channel is original else channel.tags
                renamed = Channel((tag, *directives), channel.url)
                if not blocked_channel(renamed):
                    options.setdefault((renamed.url, renamed.headers), renamed)
    eligible = sorted((item for item in discovered.values() if item['channels']),
                      key=lambda item: (item['rank'], item['name'].casefold()))
    # Interleave categories so one large catalog cannot starve the other interests.
    regional = [item for item in eligible if item['rank'] == 0]
    sports = [item for item in eligible if item['rank'] == 1]
    movies = [item for item in eligible if item['rank'] == 2]
    ordered = []
    for index in range(max(len(regional), len(sports), len(movies))):
        for category in (regional, sports, movies):
            if index < len(category):
                ordered.append(category[index])
    selected = ordered[:limit]
    preferred = [f"{item['group']}|{item['name']}" for item in selected]
    lines = ['#EXTM3U']
    for item in selected:
        for channel in item['channels'].values():
            lines.extend((*channel.tags, channel.url))
    print(f"自动发现：{len(eligible)} 个符合范围的免费目录频道，本次验证 {len(selected)} 个", flush=True)
    return preferred, ['\n'.join(lines) + '\n'] if selected else []


def discover_live_channels(fetch_text, limit=40):
    if limit == 0:
        return [], []
    sources = {'catalog': CATALOG_URL, 'channels': API_URL + 'channels.json',
               'feeds': API_URL + 'feeds.json', 'streams': API_URL + 'streams.json'}

    def fetch(item):
        name, url = item
        print(f"  获取自动发现目录：{url}", flush=True)
        try:
            body, _ = fetch_text(url, timeout=30)
            if not body:
                return name, None
            if name == 'catalog':
                return name, body if '#EXTM3U' in body[:1024] else None
            data = json.loads(body)
            if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
                return name, None
            return name, data
        except (OSError, ValueError, TypeError) as error:
            print(f"  自动发现目录不可用：{name} ({error})", flush=True)
            return name, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        data = dict(pool.map(fetch, sources.items()))
    if any(data.get(name) is None for name in ('catalog', 'channels', 'feeds')):
        print("自动发现目录暂不可用，继续验证和修复现有频道", flush=True)
        return [], []
    return build_discovery(data['catalog'], data['channels'], data['feeds'], data['streams'] or [], limit)
