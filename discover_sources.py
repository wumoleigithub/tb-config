"""Bounded online discovery of TVBox configuration candidates."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import re
from urllib.parse import quote, urlencode
from vod_sources import Client

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


def api_json(client, url, warnings):
    response = client.get(url)
    try:
        if response.status == 200 and not response.error:
            return json.loads(response.body)
    except ValueError:
        pass
    warnings.append(f'{url}: {response.error or response.status}')
    return {}


def discover_candidates(client, limit=40):
    """Search repositories and nested JSON paths; inspection happens before admission."""
    if limit <= 0:
        return [], []
    warnings = []
    repos = {name: {'paths': paths} for name, paths in KNOWN_REPOS}
    for query in ('tvbox in:name,description', '影视 TVBox in:description'):
        url = 'https://api.github.com/search/repositories?' + urlencode({
            'q': query, 'sort': 'updated', 'order': 'desc', 'per_page': 5})
        data = api_json(client, url, warnings)
        for item in data.get('items', []) if isinstance(data, dict) else []:
            if isinstance(item, dict) and item.get('full_name') and not item.get('archived'):
                repos.setdefault(item['full_name'], {'paths': [], 'branch': item.get('default_branch')})

    def scan(entry):
        name, info = entry
        branch = info.get('branch')
        if not branch:
            meta = api_json(client, f'https://api.github.com/repos/{name}', warnings)
            branch = meta.get('default_branch') if isinstance(meta, dict) else None
        if not branch:
            return []
        base = f'https://raw.githubusercontent.com/{name}/{quote(branch, safe="")}/'
        tree = api_json(client, f'https://api.github.com/repos/{name}/git/trees/{quote(branch, safe="")}?recursive=1', warnings)
        paths = list(info['paths'])
        if isinstance(tree, dict):
            candidates = [row['path'] for row in tree.get('tree', [])
                          if isinstance(row, dict) and row.get('type') == 'blob'
                          and isinstance(row.get('path'), str) and row['path'].lower().endswith('.json')
                          and row['path'].count('/') <= 2
                          and not row['path'].endswith(('package.json', 'package-lock.json'))]
            candidates.sort(key=lambda path: (path.count('/'), len(path), path))
            paths.extend(candidates[:8])
        urls = [base + quote(path, safe='/') for path in dict.fromkeys(paths)]
        readme = client.get(base + 'README.md')
        if readme.status == 200:
            text = readme.body.decode('utf-8', errors='replace')
            urls.extend(re.findall(r'https?://[^\s<>\'"()\]]+\.json(?:\?[^\s<>\'"()\]]*)?', text)[:8])
        return list(dict.fromkeys(urls))

    with ThreadPoolExecutor(max_workers=6) as executor:
        groups = list(executor.map(scan, repos.items()))
    # Round robin gives newly found repositories a chance alongside known repositories.
    results = []
    for index in range(max(map(len, groups), default=0)):
        for group in groups:
            if index < len(group) and group[index] not in results:
                results.append(group[index])
    return results[:limit], warnings


def main():
    parser = argparse.ArgumentParser(description='在线发现并检查点播源，更新资源池')
    parser.add_argument('--dry', '--dry-run', dest='dry_run', action='store_true')
    parser.add_argument('--limit', type=int, default=40)
    args = parser.parse_args()
    if args.limit < 0:
        parser.error('limit must be nonnegative')
    from vod_update import run_vod_update
    run_vod_update(dry_run=args.dry_run, discovery_limit=args.limit, update_config=False)


if __name__ == '__main__':
    main()
