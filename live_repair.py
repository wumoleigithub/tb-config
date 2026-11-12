"""Find validated replacements by channel identity, preserving working channels."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import unicodedata

from live_validation import Channel, parse_m3u
from channel_policy import blocked_channel, blocked_text


def _base_key(name):
    name = unicodedata.normalize("NFKC", name).upper().strip()
    central = re.match(r"^(?:CCTV|央视|中央)[\s_-]*0*(\d{1,2})(.*)$", name)
    if central:
        number, suffix = central.groups()
        # Ultra-HD channels and overseas editions have different schedules.
        if suffix.startswith("K"):
            return f"CCTV{int(number)}K"
        if re.search(r"AMERICA|美洲", suffix):
            return f"CCTV{int(number)}-AMERICA"
        if re.search(r"EUROPE|欧洲", suffix):
            return f"CCTV{int(number)}-EUROPE"
        plus = "+" if suffix.lstrip().startswith(("+", "PLUS")) or "体育赛事" in suffix else ""
        return f"CCTV{int(number)}{plus}"
    name = re.sub(r"\([^)]*\)|\[[^]]*\]", "", name)
    name = re.sub(r"[\s_-]+", "", name)
    return re.sub(r"(?:(?:超清|高清|标清|蓝光|HEVC|FHD|UHD|HD|SD|1080P|720P|4K|8K))+$", "", name)


CHANNEL_NAMES = {
    "港台电视31": ["RTHK TV 31", "港台電視31", "RTHK31"],
    "港台电视32": ["RTHK TV 32", "港台電視32", "RTHK32"],
    "HOY电视": ["HOY TV", "HOY電視", "香港开电视", "香港開電視"],
    "HOY资讯": ["HOY Infotainment", "HOY資訊"],
    "澳视澳门": ["TDM Ou Mun", "澳視澳門"],
    "澳门资讯": ["TDM Information", "澳門資訊"],
    "澳门综艺": ["TDM Entertainment", "澳門綜藝"],
    "澳门体育": ["TDM Sports", "澳門體育"],
    "澳视卫星": ["TDM-Macau Satellite", "澳視衛星"],
    "澳门莲花": ["Lotus TV", "澳門蓮花"],
    "台视新闻": ["TTV News", "台視新聞"],
    "客家电视": ["Hakka TV", "客家電視"],
    "原住民族电视": ["Taiwan Indigenous TV", "Indigenous TV", "原住民族電視"],
    "大爱一台": ["DaAi 1", "大愛一台"],
    "人间卫视": ["Beautiful Life TV", "人間衛視"],
    "凤凰中文": ["Phoenix Chinese Channel", "鳳凰中文"],
    "凤凰资讯": ["Phoenix InfoNews Channel", "鳳凰資訊"],
    "新唐人全球": ["NTD TV", "新唐人全球台"],
    "新唐人亚太": ["NTD TV Asia-Pacific", "新唐人亞太台"],
    "新唐人加拿大": ["NTD TV Canada", "新唐人加拿大台"],
}
ALIASES = {_base_key(alias): _base_key(name)
           for name, aliases in CHANNEL_NAMES.items() for alias in [name, *aliases]}


def channel_key(name):
    key = _base_key(name)
    return ALIASES.get(key, key)


def read_lines(path):
    with open(path, encoding="utf-8") as source:
        return [line.strip() for line in source
                if line.strip() and not line.strip().startswith("#")]


def repair_playlist(current, candidates, preferred, validator, max_alternatives=8):
    """Try current URLs first, then a bounded number of same-channel alternatives.

    Preferred names persist even when no source works today. Candidate playlists
    are only used to fill requested/current channels, not to add unrelated ones.
    """
    if max_alternatives < 1:
        raise ValueError("max_alternatives 必须大于 0")
    header, current_channels = parse_m3u(current)
    targets = OrderedDict()
    groups = {}
    for specification in preferred:
        group, separator, name = specification.partition("|")
        if not separator:
            name = group
        name = name.strip()
        if not name or (separator and not group.strip()):
            raise ValueError(f"无效目标频道：{specification!r}")
        if blocked_text(name):
            continue
        key = channel_key(name)
        targets.setdefault(key, name)
        if separator:
            groups[key] = group.strip().replace('"', '')
    originals = {}
    for channel in current_channels:
        if blocked_channel(channel):
            continue
        key = channel_key(channel.name)
        targets.setdefault(key, channel.name)
        originals.setdefault(key, []).append(channel)
    replacements = {}
    for content in candidates:
        for channel in parse_m3u(content)[1]:
            if blocked_channel(channel):
                continue
            key = channel_key(channel.name)
            if key in targets:
                replacements.setdefault(key, []).append(channel)

    def find(key):
        seen = set()
        # Existing working URLs do not count against the replacement budget.
        for channel in originals.get(key, []):
            identity = channel.url, channel.headers
            if identity in seen:
                continue
            seen.add(identity)
            if validator.verify(channel)[0]:
                return channel, "保留"
        tried = 0
        for channel in replacements.get(key, []):
            identity = channel.url, channel.headers
            if identity in seen:
                continue
            seen.add(identity)
            tried += 1
            if validator.verify(channel)[0]:
                # Keep EPG identity, name, logo and group; use the new URL's
                # directives instead of carrying old credentials/request headers.
                if key in originals:
                    channel = Channel((originals[key][0].tags[0], *channel.tags[1:]), channel.url)
                else:
                    group = groups.get(key, "央视频道" if key.startswith("CCTV") else "卫视频道")
                    name = targets[key].replace('"', '')
                    logo = re.search(r'\btvg-logo="[^"]*"', channel.tags[0])
                    logo_attr = f" {logo.group()}" if logo else ""
                    tag = (f'#EXTINF:-1 tvg-id="{name}" tvg-name="{name}"'
                           f'{logo_attr} group-title="{group}",{name}')
                    channel = Channel((tag, *channel.tags[1:]), channel.url)
                return channel, "补源"
            if tried >= max_alternatives:
                break
        return None, "未找到可用替代"

    found = {}
    stats = {"保留": 0, "补源": 0, "未找到可用替代": 0}
    print(f"── 按频道补源：{len(targets)} 个目标频道 ──", flush=True)
    with ThreadPoolExecutor(max_workers=validator.workers) as pool:
        futures = {pool.submit(find, key): key for key in targets}
        for future in as_completed(futures):
            key = futures[future]
            channel, status = future.result()
            stats[status] += 1
            found[key] = channel
            print(f"  [{len(found)}/{len(targets)}] {targets[key]}：{status}", flush=True)
    output = [header]
    for key in targets:
        channel = found[key]
        if channel:
            if key in groups:
                # Explicit grouping also applies to an already-working channel.
                tag = re.sub(r'\s+group-title="[^"]*"', '', channel.tags[0])
                metadata, name = tag.rsplit(',', 1)
                channel = Channel((f'{metadata} group-title="{groups[key]}",{name}',
                                   *channel.tags[1:]), channel.url)
            output.extend(channel.tags)
            output.append(channel.url)
    print(f"补源结果：{stats}", flush=True)
    return "\n".join(output) + "\n" if any(found.values()) else None
