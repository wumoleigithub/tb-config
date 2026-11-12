import urllib.request
import urllib.error
import urllib.parse
import json
import argparse
from vod_update import run_vod_update
from vod_sources import Client, inspect_source
from live_validation import StreamValidator
from live_repair import channel_key, read_lines, repair_playlist
from live_discovery import discover_live_channels
import time
import ssl

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

# ── 网络工具 ──────────────────────────────────────────

def encode_url(url):
    try:
        url.encode('ascii')
        return url
    except UnicodeEncodeError:
        parsed = urllib.parse.urlsplit(url)
        try:
            host = parsed.hostname.encode('idna').decode('ascii')
        except Exception:
            host = parsed.hostname
        netloc = host + (f":{parsed.port}" if parsed.port else "")
        path  = urllib.parse.quote(parsed.path, safe='/')
        query = urllib.parse.quote(parsed.query, safe='=&')
        return urllib.parse.urlunsplit((parsed.scheme, netloc, path, query, ''))

def fetch_text(url, timeout=15):
    try:
        req = urllib.request.Request(
            encode_url(url),
            headers={"User-Agent": "Mozilla/5.0 (compatible; TVBox checker)"}
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
            if resp.status != 200:
                return None, 0
            content = resp.read().decode("utf-8", errors="ignore")
            return content, time.time() - t0
    except Exception as e:
        print(f"    fetch 失败: {e}")
        return None, 0

def read_current_playlist():
    try:
        with open("active_lives.m3u", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "#EXTM3U\n"


def write_playlist(content):
    with open("active_lives.m3u", "w", encoding="utf-8") as f:
        f.write(content or "#EXTM3U\n")


def write_config(config):
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def load_live_candidates(path):
    candidates = []
    for url in read_lines(path):
        print(f"  获取补源列表：{url}", flush=True)
        content, _ = fetch_text(url)
        if content and '#EXTM3U' in content[:1024]:
            candidates.append(content)
        else:
            print("    补源列表不可用，继续其他来源", flush=True)
    return candidates


def prepare_live_repair(args, current, validator, extra=()):
    preferred = read_lines(args.preferred_channels)
    candidates = load_live_candidates(args.live_sources)
    if not args.no_discovery:
        discovered, online_candidates = discover_live_channels(fetch_text, args.discovery_limit)
        known = {channel_key(name.rsplit('|', 1)[-1]) for name in preferred}
        preferred.extend(spec for spec in discovered
                         if channel_key(spec.rsplit('|', 1)[-1]) not in known)
        candidates = online_candidates + candidates
    return repair_playlist(current, candidates + list(extra), preferred, validator, args.max_alternatives)

# ── 主流程 ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="检测 TVBox 配置及直播频道的实际播放能力")
    parser.add_argument("source_index", nargs="?", type=int, help="单源调试，不写文件（从 1 开始）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--vod-only", action="store_true", help="只检查和自动扩展点播资源池")
    mode.add_argument("--live-only", action="store_true", help="只清理现有直播列表，不扫描候选源或修改配置")
    mode.add_argument("--repair-live", action="store_true", help="检测直播并按频道寻找替代地址，不修改 TVBox 配置")
    parser.add_argument("--dry-run", action="store_true", help="验证但不写文件")
    parser.add_argument("--workers", type=int, default=8, help="并发播放检查数（默认 8）")
    parser.add_argument("--timeout", type=float, default=30, help="单个播放地址的最长检测秒数（默认 30）")
    parser.add_argument("--sample-seconds", type=float, default=5, help="必须成功解码的视频秒数（默认 5）")
    parser.add_argument("--live-sources", default="live_sources.txt", help="直播候选列表文件")
    parser.add_argument("--preferred-channels", default="preferred_channels.txt", help="持续补源的目标频道清单")
    parser.add_argument("--max-alternatives", type=int, default=8, help="每个频道最多尝试的替代地址数（默认 8）")
    parser.add_argument("--no-discovery", action="store_true", help="关闭直播及点播在线发现，仍检查已有资源池")
    parser.add_argument("--discovery-limit", type=int, default=40, help="每次自动发现的最大目标频道数（默认 40，0 为关闭）")
    parser.add_argument("--vod-discovery-limit", type=int, default=40, help="每次发现点播候选配置上限")
    args = parser.parse_args()
    if args.source_index is not None and args.source_index < 1:
        parser.error("source_index 必须从 1 开始")
    if (args.live_only or args.repair_live or args.vod_only) and args.source_index is not None:
        parser.error("直播模式不能同时指定 source_index")
    if args.max_alternatives < 1:
        parser.error("max-alternatives 必须大于 0")
    if args.discovery_limit < 0 or args.vod_discovery_limit < 0:
        parser.error("discovery-limit 不能小于 0")
    if args.workers < 1 or args.sample_seconds <= 0 or args.timeout <= args.sample_seconds:
        parser.error("workers 必须大于 0，timeout 必须大于 sample-seconds，sample-seconds 必须大于 0")
    if args.source_index is not None:
        sources = read_lines('sources_pool.txt')
        if args.source_index > len(sources):
            parser.error(f'source_index 超出范围（共 {len(sources)} 个源）')
        result = inspect_source(sources[args.source_index - 1], Client())
        print(json.dumps({k: v for k, v in result.items() if k not in ('config', 'ready_sites')}, ensure_ascii=False, indent=2))
        return
    if args.vod_only:
        run_vod_update(dry_run=args.dry_run, discover=not args.no_discovery,
                       discovery_limit=args.vod_discovery_limit, workers=args.workers)
        return
    validator = StreamValidator(args.workers, args.timeout, args.sample_seconds)
    if args.live_only:
        result = validator.filter_m3u(read_current_playlist())
        if not args.dry_run:
            write_playlist(result)
        return
    if args.repair_live:
        result = prepare_live_repair(args, read_current_playlist(), validator)
        if not args.dry_run:
            write_playlist(result)
        return

    report = run_vod_update(dry_run=args.dry_run, discover=not args.no_discovery,
                            discovery_limit=args.vod_discovery_limit, workers=args.workers)
    extra = []
    for url in report['live_urls']:
        content, _ = fetch_text(url)
        if content and '#EXTM3U' in content[:1024]:
            extra.append(content)
    result = prepare_live_repair(args, read_current_playlist(), validator, extra)
    if not args.dry_run:
        write_playlist(result)


if __name__ == '__main__':
    main()
