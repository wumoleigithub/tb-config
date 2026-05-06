"""
discover_sources.py — 自动发现可用 TVBox 配置源并更新 sources_pool.txt

用法：
  python3 discover_sources.py          # 发现 + 验证 + 写入
  python3 discover_sources.py --dry    # 只打印，不写文件
"""

import urllib.request
import urllib.parse
import urllib.error
import json
import re
import ssl
import time
import sys
import os

DRY_RUN = "--dry" in sys.argv

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

POOL_FILE = "sources_pool.txt"

# ── 已知优质仓库（每次都检查）──────────────────────────
KNOWN_REPOS = [
    ("qist/tvbox",               ["fty.json", "jsm.json", "xiaosa/api.json"]),
    ("yoursmile66/TVBox",        ["XC.json"]),
    ("hkuc/tvbox-config",        ["tv/1/urls.json"]),
    ("tv51818/TVbox",            ["config.json", "ok.json"]),
    ("tv51818/My-TV",            ["ok.json", "config.json"]),
    ("Newtxin/TVBoxSource",      ["config.json", "tvbox.json"]),
    ("wuxierj/TVBox",            ["config.json"]),
    ("scovis/TVBox",             ["config.json", "tvbox.json"]),
    ("Zhou-Li-Bin/Tvbox-QingNing", ["config.json"]),
]

# GitHub Search API 关键词（纯 ASCII 避免编码问题）
GITHUB_SEARCH_QUERIES = [
    "TVBox config spider sites",
    "tvbox csp_Xdai OR csp_AppV6 OR csp_Libvio config",
]

# ── 网络工具 ───────────────────────────────────────────

def fetch(url, timeout=12, method="GET"):
    try:
        req = urllib.request.Request(
            url,
            method=method,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; TVBox-discover)",
                "Accept": "application/json, text/plain, */*",
            },
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as r:
            body = r.read().decode("utf-8", errors="ignore") if method == "GET" else ""
            return body, r.status, time.time() - t0
    except Exception as e:
        return None, 0, 0

def head_ok(url, timeout=8):
    _, status, _ = fetch(url, timeout=timeout, method="HEAD")
    return status == 200

def is_http_dead(url, timeout=10):
    """返回 True 仅当服务器明确返回 4xx/5xx（不包括连接拒绝/超时）"""
    body, status, _ = fetch(url, timeout=timeout, method="GET")
    if status in range(400, 600):
        return True
    if status == 0 and body is None:
        # 连接失败：可能地区限制，不确定
        return False
    return False

# ── 读取已有 sources_pool.txt ──────────────────────────

def load_existing_urls():
    if not os.path.exists(POOL_FILE):
        return set()
    with open(POOL_FILE, encoding="utf-8") as f:
        return {
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        }

def prune_dead_sources():
    """把明确失效的 URL 注释为 #dead，返回标记数量"""
    if not os.path.exists(POOL_FILE):
        return 0
    with open(POOL_FILE, encoding="utf-8") as f:
        lines = f.readlines()

    new_lines = []
    marked = 0
    for line in lines:
        stripped = line.rstrip("\n")
        url = stripped.strip()
        if url and not url.startswith("#"):
            if is_http_dead(url):
                new_lines.append(f"#dead {url}\n")
                marked += 1
                print(f"  #dead: {url[:70]}")
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    if marked and not DRY_RUN:
        with open(POOL_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    return marked

# ── GitHub API：搜索仓库 ───────────────────────────────

def github_search_repos(query, per_page=8):
    """返回 [(full_name, updated_at, description), ...]"""
    params = urllib.parse.urlencode({"q": query, "sort": "updated", "order": "desc", "per_page": per_page})
    url = f"https://api.github.com/search/repositories?{params}"
    body, status, _ = fetch(url, timeout=15)
    if not body or status != 200:
        print(f"  GitHub search 失败 (status={status})")
        return []
    try:
        data = json.loads(body)
        return [
            (item["full_name"], item.get("updated_at", ""), item.get("description", ""))
            for item in data.get("items", [])
        ]
    except Exception:
        return []

# ── GitHub API：获取仓库根目录文件列表 ────────────────

def github_list_files(full_name, path=""):
    url = f"https://api.github.com/repos/{full_name}/contents/{path}"
    body, status, _ = fetch(url, timeout=12)
    if not body or status != 200:
        return []
    try:
        items = json.loads(body)
        if not isinstance(items, list):
            return []
        return [
            (item["name"], item["type"], item.get("download_url") or item.get("html_url", ""))
            for item in items
        ]
    except Exception:
        return []

def raw_url(full_name, branch, path):
    return f"https://raw.githubusercontent.com/{full_name}/{branch}/{path}"

def get_default_branch(full_name):
    url = f"https://api.github.com/repos/{full_name}"
    body, status, _ = fetch(url, timeout=10)
    if body and status == 200:
        try:
            return json.loads(body).get("default_branch", "main")
        except Exception:
            pass
    return "main"

# ── 从仓库中提取候选 URL ───────────────────────────────

JSON_PATTERN = re.compile(r'https?://[^\s\'")\]]+\.json[^\s\'")\]]*', re.IGNORECASE)
RAW_GH_PATTERN = re.compile(r'https?://raw\.githubusercontent\.com/[^\s\'")\]]+', re.IGNORECASE)

def extract_urls_from_readme(full_name, branch="main"):
    """从 README 里抓所有 http(s) JSON 链接"""
    for fname in ("README.md", "README.MD", "readme.md"):
        url = raw_url(full_name, branch, fname)
        body, status, _ = fetch(url, timeout=10)
        if body and status == 200:
            found = set(JSON_PATTERN.findall(body)) | set(RAW_GH_PATTERN.findall(body))
            return found
    return set()

def candidate_urls_from_repo(full_name):
    """合并：已知路径 + README 抓取 + 根目录 JSON 文件"""
    branch = get_default_branch(full_name)
    candidates = set()

    # 1. 根目录 JSON 文件
    files = github_list_files(full_name)
    for name, ftype, _ in files:
        if ftype == "file" and name.lower().endswith(".json"):
            candidates.add(raw_url(full_name, branch, name))

    # 2. README 中的链接
    candidates |= extract_urls_from_readme(full_name, branch)

    # 3. 过滤：保留看起来像 TVBox 接口的 URL
    def looks_like_tvbox(u):
        return any(kw in u.lower() for kw in ["tvbox", "config", "api", "tv", "jsm", "fty"])
    candidates = {u for u in candidates if looks_like_tvbox(u)}

    return candidates, branch

# ── 验证 URL 是否为有效的 TVBox 配置 ──────────────────

def resolve_spider(spider_raw, source_url):
    """把相对 spider 路径转成绝对 URL"""
    url = spider_raw.split(";")[0].strip()
    if not url:
        return spider_raw
    if url.startswith("http"):
        return spider_raw
    # 相对路径：基于 source_url 的目录
    if url.startswith("./"):
        base = source_url.rsplit("/", 1)[0] + "/"
        abs_url = base + url[2:]
        return abs_url + (";" + ";".join(spider_raw.split(";")[1:]) if ";" in spider_raw else "")
    return spider_raw

def validate_url(url):
    """
    返回 (valid: bool, info: str, meta: dict)
    meta 包含 sites_count, spider_ok, has_lives
    """
    body, status, elapsed = fetch(url, timeout=15)
    if not body or status != 200:
        return False, f"HTTP {status}", {}

    # 识别类型
    stripped = body.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(body)
        except Exception:
            return False, "JSON 解析失败", {}

        sites = [s for s in data.get("sites", []) if s.get("key") and s.get("api")]
        if len(sites) < 3:
            return False, f"sites 太少 ({len(sites)})", {}

        spider_raw = data.get("spider", "")
        spider_raw_abs = resolve_spider(spider_raw, url)
        spider_url = spider_raw_abs.split(";")[0].strip()
        spider_ok = head_ok(spider_url) if spider_url.startswith("http") else False

        has_lives = bool(data.get("lives"))
        info = f"sites={len(sites)}  spider={'✅' if spider_ok else '❌'}  lives={'✅' if has_lives else '-'}  {elapsed:.1f}s"
        return True, info, {
            "sites_count": len(sites),
            "spider_ok": spider_ok,
            "has_lives": has_lives,
            "elapsed": elapsed,
        }

    elif "#EXTM3U" in body[:1024]:
        return False, "M3U 直播源（跳过）", {}

    return False, "未知格式", {}

# ── 主流程 ─────────────────────────────────────────────

def main():
    # 0. 检测现有 pool 地址，标记失效的
    print("── 检测现有 pool 地址 ──")
    marked = prune_dead_sources()
    if marked:
        print(f"  已标记 {marked} 个失效地址为 #dead")
    else:
        print("  所有现有地址均无明确失效")
    print()

    existing = load_existing_urls()
    print(f"已有 {len(existing)} 个有效源\n")

    candidate_map = {}  # url -> (comment, source_repo)

    # 1. 已知仓库
    print("── 检查已知仓库 ──")
    for full_name, paths in KNOWN_REPOS:
        branch = get_default_branch(full_name)
        for path in paths:
            u = raw_url(full_name, branch, path)
            if u not in existing:
                candidate_map[u] = (f"#{full_name}/{path}", full_name)
        print(f"  {full_name}  ({len(paths)} 个路径)")
    print()

    # 2. GitHub 搜索
    print("── GitHub 搜索 ──")
    seen_repos = {r for r, _ in KNOWN_REPOS}
    for query in GITHUB_SEARCH_QUERIES:
        print(f"  搜索: {query}")
        results = github_search_repos(query, per_page=6)
        time.sleep(1)  # GitHub API rate limit
        for full_name, updated, desc in results:
            if full_name in seen_repos:
                continue
            seen_repos.add(full_name)
            print(f"    {full_name}  ({updated[:10]})  {(desc or '')[:40]}")
            try:
                urls, branch = candidate_urls_from_repo(full_name)
                for u in urls:
                    if u not in existing and u not in candidate_map:
                        candidate_map[u] = (f"#{full_name}", full_name)
            except Exception as e:
                print(f"      提取失败: {e}")
            time.sleep(0.5)
    print()

    if not candidate_map:
        print("没有发现新的候选源")
        return

    print(f"── 验证 {len(candidate_map)} 个候选 URL ──\n")
    new_entries = []  # [(comment, url, meta)]

    for url, (comment, repo) in candidate_map.items():
        print(f"  {url[:70]}")
        valid, info, meta = validate_url(url)
        status_icon = "✅" if valid else "❌"
        print(f"    {status_icon} {info}\n")
        if valid:
            new_entries.append((comment, url, meta))

    # 按 sites_count 排序
    new_entries.sort(key=lambda x: x[2].get("sites_count", 0), reverse=True)

    print("=" * 50)
    print(f"📊 新增有效源: {len(new_entries)} 个\n")
    for comment, url, meta in new_entries:
        print(f"  {comment}")
        print(f"  {url}")
        print()

    if not new_entries:
        print("无新源可添加")
        return

    if DRY_RUN:
        print("（--dry 模式，不写文件）")
        return

    # 追加到 sources_pool.txt
    with open(POOL_FILE, "a", encoding="utf-8") as f:
        f.write("\n")
        for comment, url, _ in new_entries:
            f.write(f"{comment}\n{url}\n")

    print(f"✅ 已追加 {len(new_entries)} 条到 {POOL_FILE}")

if __name__ == "__main__":
    main()
