"""Inspect and refresh complete, compatible TVBox VOD bundles."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from discover_sources import discover_candidates
from vod_sources import Client, inspect_config, inspect_source


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def atomic_write(path, text):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


def write_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def pool_urls(lines):
    return list(dict.fromkeys(line.strip()[6:].strip() if line.strip().startswith('#dead ')
        else line.strip() for line in lines if line.strip().startswith(('http://', 'https://', '#dead '))))


def update_pool(lines, results, history):
    health = deepcopy(history)
    by_url = {item['url']: item for item in results}
    for url, item in by_url.items():
        previous = history.get(url, {})
        health[url] = {'missing_count': previous.get('missing_count', 0) + 1
                       if item.get('http_status') in (404, 410) else 0,
                       'state': item['state']}
    output, known = [], set(pool_urls(lines))
    for line in lines:
        stripped = line.strip()
        url = stripped[6:].strip() if stripped.startswith('#dead ') else stripped
        if url in by_url:
            if health[url]['missing_count'] >= 2:
                line = '#dead ' + url
            elif by_url[url]['state'] == 'ready':
                line = url
        output.append(line.rstrip('\n'))
    additions = [item['url'] for item in results if item['url'] not in known and item['state'] == 'ready']
    if additions:
        output.extend(['', '# 自动发现：配置及至少一个站点依赖检查通过；播放需在客户端验证'] + additions)
    return '\n'.join(output).rstrip() + '\n', health, len(additions)


def quality(result):
    count = result.get('ready_count', 0)
    # Prefer coverage ratio and bounded diversity, not hundreds of unchecked entries.
    return (40 * count / max(result.get('site_count', 0), 1) + min(count, 60),
            -result.get('elapsed', 999))


def choose_bundle(current, current_check, candidates):
    ready = [item for item in candidates if item.get('state') == 'ready']
    origin = current.get('_source_url')
    matches = [item for item in ready if item['url'] == origin]
    if not matches and not origin:
        # Migration from old configs: an identical full JAR reference is required.
        matches = [item for item in ready if current.get('spider') and
                   item['config'].get('spider') == current['spider']]
    if matches:
        selected = max(matches, key=quality)
        if (selected['ready_count'] < current_check.get('ready_count', 0)
                and any(site['state'] == 'unknown' for site in selected.get('sites', []))):
            return None, 'upstream partially unverifiable; preserve current verified bundle'
    elif current_check.get('ready_count', 0):
        return None, 'current dependencies pass; keep current bundle while alternatives remain in pool'
    elif ready:
        selected = max(ready, key=quality)
    else:
        return None, 'no verified replacement; preserve current configuration'
    updated = {k: deepcopy(v) for k, v in selected['config'].items() if not k.startswith('_')}
    updated['sites'] = deepcopy(selected['ready_sites'])
    updated['lives'] = deepcopy(current.get('lives', []))
    updated['_source_url'] = selected['url']
    return updated, 'refresh source as a complete bundle; exclude unverified sites'


def public_result(result):
    return {key: value for key, value in result.items() if key not in ('config', 'ready_sites')}


def run_vod_update(dry_run=False, discover=True, discovery_limit=40, workers=6,
                   root='.', client=None, update_config=True):
    root = Path(root)
    client = client or Client()
    current = read_json(root / 'config.json', {})
    policy = read_json(root / 'vod_policy.json', {})
    excluded = policy.get('excluded_site_keys', [])
    lines = (root / 'sources_pool.txt').read_text(encoding='utf-8').splitlines()
    urls = pool_urls(lines)
    if current.get('_source_url') and current['_source_url'] not in urls:
        urls.insert(0, current['_source_url'])
    print('检查当前点播配置、在线发现候选配置…', flush=True)
    candidates, warnings = discover_candidates(client, discovery_limit) if discover else ([], [])
    urls = list(dict.fromkeys(urls + candidates))
    current_check = inspect_config(current, current.get('_source_url', ''), client, excluded)
    def inspect(url):
        result = inspect_source(url, client, excluded)
        print(f"  {result['state']:7} {result.get('ready_count', 0):3}/{result.get('site_count', 0):3} {url}", flush=True)
        return result
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(inspect, urls))
    selected, reason = choose_bundle(current, current_check, results) if update_config else (None, 'discovery only')
    pool, health, added = update_pool(lines, results, read_json(root / 'vod_health.json', {}))
    changed = selected is not None and selected != current
    report = {'checked_at': datetime.now(timezone.utc).isoformat(),
              'validation_scope': 'Configuration, JAR checksum/classes and public catalog APIs only. No Android plugin execution or movie playback verification.',
              'current': public_result(current_check), 'candidates': [public_result(r) for r in results],
              'discovery_warnings': warnings, 'added_sources': added,
              'selected_source': (selected or current).get('_source_url'),
              'config_changed': changed, 'decision': reason}
    if not dry_run:
        if changed:
            write_json(root / 'config.previous.json', current)
            write_json(root / 'config.json', selected)
        atomic_write(root / 'sources_pool.txt', pool)
        write_json(root / 'vod_health.json', health)
        write_json(root / 'vod_report.json', report)
    live_urls = []
    for item in results:
        lives = item.get('config', {}).get('lives', [])
        for live in lives if isinstance(lives, list) else []:
            if isinstance(live, dict) and isinstance(live.get('url'), str) and live['url'].startswith(('http://', 'https://')):
                live_urls.append(live['url'])
    print(f"点播检查完成：{len(results)} 个配置，新增 {added} 个候选源；"
          f"当前依赖通过 {current_check.get('ready_count', 0)}/{current_check.get('site_count', 0)}；"
          f"{'拟更新' if dry_run else '更新'}配置：{changed}", flush=True)
    return dict(report, live_urls=list(dict.fromkeys(live_urls)))
