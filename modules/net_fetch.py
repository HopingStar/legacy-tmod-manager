# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 HopingStar
"""网络信息获取模块（独立于 app.py，供其调用）

包含：
  1. tModLoader 版本更新检测（GitHub Releases API）
     —— 带缓存 / 失败自动重试 / 离线判定 / 版本比较
  2. 整合包内模组的更新检查
     —— workshop 目录扫描（Steam 下载的最新版 .tmod，供更新检测）
     —— Steam GetPublishedFileDetails 查询（time_updated 供『更新于 N 天前』）
  3. Steam 创意工坊模组元数据获取
     —— steam_request / steam_query_publish_details（本名 / 图标 / 标签 / 热度 / 描述等）
"""
import os
import re
import glob
import json
import time
import socket
import threading
import datetime

# requests 可用性（Steam / GitHub 查询）
try:
    import requests
    HAS_REQUESTS = True
    try:
        from urllib3.exceptions import InsecureRequestWarning
        import warnings
        warnings.simplefilter('ignore', InsecureRequestWarning)
    except ImportError:
        pass
except ImportError:
    HAS_REQUESTS = False


# ---------- tModLoader 版本更新检测（GitHub Releases） ----------
TMOD_REPO_URL = 'https://api.github.com/repos/tModLoader/tModLoader/releases?per_page=20'
_tmod_update_lock = threading.Lock()
_tmod_update_cache = None            # 最近一次拉取结果（成功或失败）
_tmod_update_success = False         # 最近一次是否成功
_tmod_update_time = 0.0              # 最近一次拉取完成的时间戳
_tmod_update_checking = False        # 是否正在后台拉取（防止并发重复拉取）
_TMOD_UPDATE_TTL = 6 * 3600          # 成功缓存 6 小时
_TMOD_UPDATE_RETRY_COOLDOWN = 60     # 失败后冷却 60 秒再重试


def compare_versions(a, b):
    """逐段数值比较版本号。返回 1(a>b) / -1(a<b) / 0(a==b)。
    提取第一段连续的 x.y.z 数字串（容忍 'v' 前缀 / '+'-后缀等），如 'v2026.06.3.6' → '2026.06.3.6'。"""
    def parts(s):
        m = re.search(r'\d+(?:\.\d+)+', str(s))
        seg = m.group(0) if m else '0'
        return [int(x) for x in seg.split('.')] or [0]
    pa, pb = parts(a), parts(b)
    for x, y in zip(pa, pb):
        if x != y:
            return 1 if x > y else -1
    return (len(pa) > len(pb)) - (len(pa) < len(pb))


def _extract_version_from_tag(tag):
    """从 GitHub release tag（如 'v2026.06.3.6' / 'v2026.06.3.6-preview'）提取纯数字版本号"""
    m = re.search(r'\d+(?:\.\d+)+', tag or '')
    return m.group(0) if m else ''


def days_since(iso):
    """ISO 时间串 → 距今天数（UTC）。解析失败返回 None。"""
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - dt).days
    except Exception:
        return None


def has_internet():
    """粗略探测是否可联网（TCP 连接国内稳定站点），用于区分『离线』与『GitHub 不可达』"""
    for host in ('www.baidu.com', 'www.qq.com'):
        try:
            socket.create_connection((host, 443), timeout=3).close()
            return True
        except Exception:
            continue
    return False


def _tmod_update_stale():
    """是否需要重新拉取：无缓存 / 成功但超 TTL / 失败且已过冷却"""
    if _tmod_update_cache is None:
        return True
    age = time.time() - _tmod_update_time
    if _tmod_update_success:
        return age > _TMOD_UPDATE_TTL
    return age > _TMOD_UPDATE_RETRY_COOLDOWN


def _fetch_tmod_releases():
    """拉取 tModLoader GitHub Releases，返回 {'ok': True, 'latest_stable': {...}, 'latest_preview': {...}}"""
    headers = {'User-Agent': 'tmod-manager', 'Accept': 'application/vnd.github+json'}
    if HAS_REQUESTS:
        # 证书校验失败时降级不验证（本地工具，只读公开数据）
        try:
            resp = requests.get(TMOD_REPO_URL, headers=headers, timeout=10, verify=True)
        except requests.exceptions.SSLError:
            resp = requests.get(TMOD_REPO_URL, headers=headers, timeout=10, verify=False)
        if resp.status_code != 200:
            raise RuntimeError('GitHub API status %s' % resp.status_code)
        releases = resp.json()
    else:
        import urllib.request
        req = urllib.request.Request(TMOD_REPO_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            releases = json.loads(r.read().decode('utf-8'))
    stable = preview = None
    for rel in releases or []:
        ver = _extract_version_from_tag(rel.get('tag_name', ''))
        if not ver:
            continue
        item = {'version': ver,
                'days': days_since(rel.get('published_at', '')),
                'url': rel.get('html_url', '') or ''}
        if rel.get('prerelease'):
            if preview is None:
                preview = item
        elif stable is None:
            stable = item
        if stable is not None and preview is not None:
            break
    if stable is None and preview is None:
        return {'ok': False, 'reason': 'no_release'}
    return {'ok': True, 'latest_stable': stable, 'latest_preview': preview}


def _fetch_tmod_releases_thread():
    global _tmod_update_cache, _tmod_update_success, _tmod_update_time, _tmod_update_checking
    try:
        result = _fetch_tmod_releases()
        ok = bool(result and result.get('ok'))
        if not ok:
            result = {'ok': False, 'reason': 'no_release'}
    except Exception:
        # 区分『离线』（无网络）与『GitHub 不可达』（有网但拉取失败）
        if has_internet():
            result = {'ok': False, 'reason': 'fetch_failed'}
        else:
            result = {'ok': False, 'reason': 'offline'}
        ok = False
    with _tmod_update_lock:
        _tmod_update_cache = result
        _tmod_update_success = ok
        _tmod_update_time = time.time()
        _tmod_update_checking = False


def get_tmod_update():
    """返回最新 stable/preview 版本；缓存过期（成功 6h / 失败 60s 冷却）时后台重新拉取。"""
    global _tmod_update_checking
    with _tmod_update_lock:
        cache = dict(_tmod_update_cache or {})
        need_fetch = _tmod_update_stale() and not _tmod_update_checking
        if need_fetch:
            _tmod_update_checking = True
    if need_fetch:
        threading.Thread(target=_fetch_tmod_releases_thread, daemon=True).start()
    if not cache:
        return {'ok': False, 'reason': 'checking'}
    return cache


# ---------- 整合包模组更新（workshop 扫描 + Steam 元数据） ----------
STEAM_API = 'https://api.steampowered.com'


def steam_request(url, data=None):
    """Steam API 请求：优先证书验证；失败则降级不验证（本地工具，只读公开数据）。
    解决部分 Python 环境 SSL 证书链缺失导致 requests 无法访问 HTTPS 的问题。"""
    kwargs = {'url': url, 'timeout': 20}
    if data is not None:
        kwargs['data'] = data
        send = requests.post
    else:
        send = requests.get
    try:
        return send(verify=True, **kwargs)
    except requests.exceptions.SSLError:
        return send(verify=False, **kwargs)


def scan_workshop_mods(bat_paths):
    """扫描各版本 tML 的 workshop 内容目录，返回 {模组名: {'id': workshop_id, 'path': .tmod 完整路径}}。
    workshop 目录里的 .tmod 是 Steam 下载的最新版，mtime 可用于更新检测。
    bat_paths：各版本 .bat 启动文件路径列表（由此推导 tML 安装目录）。"""
    result = {}
    bases = set()
    for bat in bat_paths or []:
        if bat:
            bases.add(os.path.dirname(bat))
    for base in bases:
        content = os.path.join(base, 'steamapps', 'workshop', 'content', '1281930')
        if not os.path.isdir(content):
            continue
        try:
            for wid in os.listdir(content):
                wid_dir = os.path.join(content, wid)
                if not os.path.isdir(wid_dir):
                    continue
                for tmod in glob.glob(os.path.join(wid_dir, '**', '*.tmod'), recursive=True):
                    name = os.path.splitext(os.path.basename(tmod))[0]
                    if name not in result:
                        result[name] = {'id': wid, 'path': tmod}
        except Exception:
            continue
    return result


def get_workshop_ids(bat_paths):
    """模组名 → workshop ID 映射（补充 PublishIds 未覆盖的整合包目录模组）"""
    return {name: v['id'] for name, v in scan_workshop_mods(bat_paths).items()}


def steam_query_publish_details(ids):
    """调 GetPublishedFileDetails（匿名）批量拿本名/图标/更新时间；返回 (成功结果, 失败原因)"""
    if not HAS_REQUESTS:
        return {}, {str(pid): '未安装 requests 库' for pid in ids}
    if not ids:
        return {}, {}
    results = {}
    errors = {}
    url = STEAM_API + '/ISteamRemoteStorage/GetPublishedFileDetails/v1/'
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        fields = {'itemcount': len(batch)}
        for idx, pid in enumerate(batch):
            fields[f'publishedfileids[{idx}]'] = pid
        try:
            r = steam_request(url, data=fields)
            r.raise_for_status()
            found = set()
            for item in r.json().get('response', {}).get('publishedfiledetails', []):
                pid = str(item.get('publishedfileid', ''))
                if pid:
                    results[pid] = {
                        'name': item.get('title', ''),
                        'icon': item.get('preview_url', ''),
                        'time_updated': item.get('time_updated', 0),
                        'tags': [t.get('tag', '') for t in (item.get('tags') or []) if t.get('tag')],
                        'description': item.get('description', ''),
                        'subscriptions': item.get('subscriptions', 0),
                        'favorited': item.get('favorited', 0),
                        'views': item.get('views', 0),
                        'time_created': item.get('time_created', 0),
                        'file_size': item.get('file_size', 0),
                        'visibility': item.get('visibility', 0),
                        'banned': item.get('banned', 0),
                    }
                    found.add(pid)
            # batch 中未返回的 ID：记录具体原因
            for pid in batch:
                if str(pid) not in found:
                    errors[str(pid)] = '未找到该模组（可能已下架、私有或 ID 错误）'
        except Exception as e:
            for pid in batch:
                errors[str(pid)] = f'网络请求失败: {type(e).__name__}'
    return results, errors
