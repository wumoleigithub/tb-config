import urllib.request
import urllib.error
import urllib.parse
import json
import sys
import re
import time
import ssl

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

# ── 工具函数 ──────────────────────────────────────────

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

def check_spider(spider_url, timeout=10):
    if not spider_url or not spider_url.startswith('http'):
        return False, 0
    try:
        req = urllib.request.Request(
            encode_url(spider_url),
            headers={"User-Agent": "Mozilla/5.0 (compatible; TVBox checker)"},
            method="HEAD"
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
            return resp.status == 200, time.time() - t0
    except Exception as e:
        print(f"    spider 验证失败: {e}")
        return False, 0

def resolve_spider(spider_raw, source_url):
    """相对路径 spider 转绝对 URL"""
    url = spider_raw.split(";")[0].strip()
    if not url or url.startswith('http'):
        return spider_raw
    if url.startswith('./'):
        base = source_url.rsplit("/", 1)[0] + "/"
        abs_url = base + url[2:]
        suffix = spider_raw[len(url):]
        return abs_url + suffix
    return spider_raw

# ── 输入层：识别来源类型 ──────────────────────────────

def detect_source_type(url, content):
    if url.endswith('.jar') or (content and content[:2] == 'PK'):
        return 'jar'
    if content and content.lstrip().startswith('{'):
        try:
            json.loads(content)
            return 'json'
        except Exception:
            pass
    if content and '#EXTM3U' in content[:1024]:
        return 'm3u'
    return 'unknown'

# ── 解析层：过滤频道 ──────────────────────────────────

ALLOWED    = re.compile(r'\.(m3u8|flv|ts)(\?|$)', re.IGNORECASE)
BLOCKED    = re.compile(r'^(proxy://|ext://)', re.IGNORECASE)
BLOCKED_KW = re.compile(r'spider|#EXT-X-KEY', re.IGNORECASE)

def is_clean_stream(stream_url):
    if BLOCKED.match(stream_url):
        return False
    if BLOCKED_KW.search(stream_url):
        return False
    if ALLOWED.search(stream_url):
        return True
    if stream_url.startswith('http'):
        return True
    return False

def filter_m3u(content):
    lines = content.splitlines()
    output = ['#EXTM3U']
    i = 0
    if lines and lines[0].startswith('#EXTM3U'):
        i = 1
    kept = 0
    dropped = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                stream = lines[j].strip()
                if is_clean_stream(stream):
                    output.append(line)
                    output.append(stream)
                    kept += 1
                else:
                    dropped += 1
                i = j + 1
            else:
                i += 1
        else:
            if line.startswith('#'):
                output.append(line)
            i += 1
    print(f"    保留频道: {kept}，丢弃频道: {dropped}")
    if kept == 0:
        return None
    return '\n'.join(output)

# ── 评分函数 ──────────────────────────────────────────

def score_cfg(sites_count, spider_ok, response_time):
    score = sites_count * 10
    if spider_ok:
        score += 50
    if response_time < 2:
        score += 20
    elif response_time < 5:
        score += 10
    return score

def score_m3u(channel_count, response_time):
    score = channel_count * 0.5
    if response_time < 2:
        score += 20
    elif response_time < 5:
        score += 10
    return score

# ── 检查现有 config 是否仍然有效 ──────────────────────

def check_current_config(config):
    spider_raw = config.get("spider", "")
    spider_url = spider_raw.split(";")[0].strip()
    if not spider_url:
        print("当前 config 无 spider")
        return False
    print(f"检查当前 spider: {spider_url[:80]}")
    ok, elapsed = check_spider(spider_url)
    if ok:
        print(f"  ✅ 当前 spider 仍可访问 ({elapsed:.1f}s)")
    else:
        print(f"  ❌ 当前 spider 不可访问")
    return ok

def count_current_live_channels():
    try:
        with open("active_lives.m3u", encoding="utf-8") as f:
            return f.read().count('#EXTINF')
    except Exception:
        return 0

# ── 主流程 ────────────────────────────────────────────

# 用法：
#   python3 check_sources.py            全量扫描，智能更新
#   python3 check_sources.py 3          单源调试（第 3 个源，不写文件）
target_index = int(sys.argv[1]) if len(sys.argv) > 1 else None

with open("sources_pool.txt", encoding="utf-8") as f:
    sources = [
        line.strip()
        for line in f
        if line.strip() and not line.startswith("#")
    ]

if target_index:
    sources = [sources[target_index - 1]]
    print(f"🔍 单源调试模式，测试第 {target_index} 个源\n")
else:
    print(f"共找到 {len(sources)} 个候选源，开始检测...\n")

# 读取现有 config
with open("config.json", encoding="utf-8") as f:
    config = json.load(f)

dry_run = bool(target_index)

# 检查现有 config 是否仍然有效
if not dry_run:
    print("── 检查现有配置 ──────────────────────────────────")
    current_cfg_ok = check_current_config(config)
    current_live_channels = count_current_live_channels()
    print(f"当前直播源频道数: {current_live_channels}\n")
else:
    current_cfg_ok = False
    current_live_channels = 0

cfg_candidates = []   # {"url", "spider", "sites", "parses", "score", "response_time"}
m3u_candidates = []   # {"url", "content", "channel_count", "score", "response_time"}

print("── 扫描候选源 ─────────────────────────────────────")
for i, url in enumerate(sources, 1):
    print(f"[{i}/{len(sources)}] {url}")
    raw, elapsed = fetch_text(url)
    if not raw:
        print(f"  ❌ 无法获取内容\n")
        continue

    stype = detect_source_type(url, raw)
    print(f"  类型: {stype}  响应: {elapsed:.1f}s")

    if stype == 'jar':
        print(f"  ⛔ jar 接口，丢弃\n")
        continue

    elif stype == 'unknown':
        print(f"  ❌ 无法识别格式，跳过\n")
        if target_index:
            print(f"  --- 原始内容前 500 字符 ---\n{raw[:500]}\n")
        continue

    elif stype == 'json':
        data = json.loads(raw)

        # ── 配置候选 ────────────────────────────────
        sites = data.get("sites", [])
        valid_sites = [s for s in sites if s.get("key") and s.get("api")]
        spider_raw = data.get("spider", "")
        spider_raw = resolve_spider(spider_raw, url)
        spider_url = spider_raw.split(";")[0].strip()

        if len(valid_sites) >= 3:
            if spider_url:
                print(f"  → 验证 spider: {spider_url[:80]}")
                spider_ok, spider_time = check_spider(spider_url)
                if spider_ok:
                    s = score_cfg(len(valid_sites), True, elapsed)
                    print(f"    ✅ spider 可访问 ({spider_time:.1f}s)，评分: {s}")
                    cfg_candidates.append({
                        "url": url, "spider": spider_raw,
                        "sites": valid_sites, "parses": data.get("parses", []),
                        "score": s, "response_time": elapsed
                    })
                else:
                    print(f"    ❌ spider 不可访问，跳过此配置源")
            else:
                no_spider_sites = [s for s in valid_sites if s.get("type", 3) in (0, 1)]
                if len(no_spider_sites) >= 3:
                    s = score_cfg(len(no_spider_sites), False, elapsed)
                    print(f"  ✅ 无 spider，type 0/1 站点 {len(no_spider_sites)} 个，评分: {s}")
                    cfg_candidates.append({
                        "url": url, "spider": "",
                        "sites": no_spider_sites, "parses": data.get("parses", []),
                        "score": s, "response_time": elapsed
                    })
                else:
                    print(f"  ❌ 无 spider 且 type 0/1 站点不足（{len(no_spider_sites)} 个），跳过")
        elif target_index:
            print(f"  ℹ️  sites 数量: {len(sites)}（需 ≥3 才采用）")

        # ── 直播候选 ────────────────────────────────
        lives = data.get("lives", [])
        for item in lives:
            live_url = item.get("url", "")
            if not live_url:
                continue
            print(f"  → 提取直播 URL: {live_url}")
            m3u_raw, m3u_elapsed = fetch_text(live_url)
            if not m3u_raw or '#EXTM3U' not in m3u_raw[:1024]:
                print(f"    ❌ 不是合法 m3u")
                continue
            result = filter_m3u(m3u_raw)
            if result:
                ch = result.count('#EXTINF')
                s = score_m3u(ch, m3u_elapsed)
                print(f"    ✅ {ch} 个频道  响应: {m3u_elapsed:.1f}s  评分: {s:.0f}")
                m3u_candidates.append({
                    "url": live_url, "content": result,
                    "channel_count": ch, "score": s, "response_time": m3u_elapsed
                })
                break

    elif stype == 'm3u':
        result = filter_m3u(raw)
        if result:
            ch = result.count('#EXTINF')
            s = score_m3u(ch, elapsed)
            print(f"  ✅ {ch} 个频道  评分: {s:.0f}")
            m3u_candidates.append({
                "url": url, "content": result,
                "channel_count": ch, "score": s, "response_time": elapsed
            })

    print()

# ── 评分汇总 ──────────────────────────────────────────

cfg_candidates.sort(key=lambda x: x["score"], reverse=True)
m3u_candidates.sort(key=lambda x: x["score"], reverse=True)

print("=" * 50)
print("📊 评分结果\n")

if cfg_candidates:
    print(f"配置源 Top {min(3, len(cfg_candidates))}:")
    for c in cfg_candidates[:3]:
        spider_tag = "有spider" if c["spider"] else "无spider"
        print(f"  [{c['score']:4.0f}分] 站点:{len(c['sites']):3d}  {c['response_time']:.1f}s  {spider_tag}  {c['url'][:60]}")
else:
    print("配置源：无有效候选")

print()

if m3u_candidates:
    print(f"直播源 Top {min(3, len(m3u_candidates))}:")
    for m in m3u_candidates[:3]:
        print(f"  [{m['score']:6.1f}分] 频道:{m['channel_count']:4d}  {m['response_time']:.1f}s  {m['url'][:60]}")
else:
    print("直播源：无有效候选")

print()

# ── 写入决策 ──────────────────────────────────────────

if dry_run:
    if cfg_candidates:
        best = cfg_candidates[0]
        print(f"✅ 配置源（调试）: {best['url']}")
        print(f"   前 5 个站点:")
        for s in best["sites"][:5]:
            print(f"     [{s.get('key','')}] {s.get('name','')}")
    if m3u_candidates:
        best = m3u_candidates[0]
        print(f"✅ 直播源（调试）: {best['url']}  {best['channel_count']} 频道")
    print("（调试模式，不写文件）")
    sys.exit(0)

# 直播源：只有当前失效，或新源频道数超出 20% 才替换
if m3u_candidates:
    best = m3u_candidates[0]
    if current_live_channels == 0:
        print(f"✅ 直播源：当前失效，替换为 {best['url']} ({best['channel_count']} 频道)")
        with open("active_lives.m3u", "w", encoding="utf-8") as f:
            f.write(best["content"])
    elif best["channel_count"] > current_live_channels * 1.2:
        print(f"✅ 直播源：新源频道数 {best['channel_count']} 比当前 {current_live_channels} 多 20%+，替换")
        with open("active_lives.m3u", "w", encoding="utf-8") as f:
            f.write(best["content"])
    else:
        print(f"✅ 直播源：当前 {current_live_channels} 频道仍有效，保留不变")
else:
    print("⚠️  未找到有效直播源，active_lives.m3u 保持不变")

print()

# 配置源：当前 spider 有效则保留，失效才替换
if current_cfg_ok:
    print(f"✅ 配置源：当前 spider 仍可用，保留现有 {len(config.get('sites', []))} 个站点不变")
else:
    if cfg_candidates:
        best = cfg_candidates[0]
        print(f"✅ 配置源：当前 spider 失效，替换为 {best['url']}")
        print(f"   站点数: {len(best['sites'])}  解析数: {len(best['parses'])}")
        config["spider"] = best["spider"]
        config["sites"]  = best["sites"]
        config["parses"] = best["parses"]
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print(f"   → 已写入 config.json")
    else:
        print("⚠️  当前 spider 失效且无有效候选源，config.json 保持不变")
