import os
import json
import re
import shutil
import socket
import subprocess
import time
import uuid
import threading
import webbrowser
import warnings
import sys
from flask import Flask, request, jsonify, render_template

# Windows 下隐藏子进程控制台窗口（powershell/tasklist/wmic/taskkill 等），避免闪现黑窗
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# 尝试导入 requests（Steam API 查询）
try:
    import requests
    HAS_REQUESTS = True
    # 抑制"未验证 HTTPS"警告（本地工具，只读公开数据）
    try:
        from urllib3.exceptions import InsecureRequestWarning
        warnings.simplefilter('ignore', InsecureRequestWarning)
    except ImportError:
        pass
except ImportError:
    HAS_REQUESTS = False

# 尝试导入 psutil
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# 项目根目录加入模块搜索路径，确保可导入 modules/ 下的独立模块
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from modules import icon_util, net_fetch      # 图标加密 / 网络信息获取（独立模块）
from modules.tmod_parser import parse_tmod_meta, save_icon, find_tmod

LOCAL_ICON_DIR = os.path.join(BASE_DIR, 'static', 'mod_icons')

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False  # Flask <2.3
app.json.ensure_ascii = False        # Flask >=2.3（3.1 下 JSON_AS_ASCII 已失效）

DATA_FILE = os.path.join(BASE_DIR, 'data.json')
GLOBAL_MODPACKS_PATH = r'D:\Documents\My Games\Terraria\tModLoader\Mods\ModPacks'
DEFAULT_MODS_PATH = r'D:\Documents\My Games\Terraria\tModLoader\Mods'

# 初始化数据文件
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump({'versions': [], 'last_launch': None, 'pack_descs': {},
                   'global_special': {'active': False, 'mods': []},
                   'favorites': {'versions': [], 'packs': []}}, f, indent=2)


def read_data():
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def write_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_version(data, version_id):
    for v in data['versions']:
        if v['id'] == version_id:
            return v
    return None


def _find_tml_dll(bat_path):
    """根据启动 .bat 定位同目录下的 tModLoader.dll"""
    try:
        dll = os.path.join(os.path.dirname(bat_path), 'tModLoader.dll')
        return dll if os.path.isfile(dll) else None
    except Exception:
        return None


def _read_tml_version(dll_path):
    """读 tModLoader.dll 的 ProductVersion，提取 tML 版本号（如 '2026.06.3.6'）。

    ProductVersion 格式：1.4.4.9+2026.06.3.6|2026.06|stable|Stable|<commit>|<ticks>
    """
    try:
        safe = dll_path.replace("'", "''")
        cmd = ['powershell', '-NoProfile', '-Command',
               f"(Get-Item '{safe}').VersionInfo.ProductVersion"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        m = re.search(r'\+([0-9]+(?:\.[0-9]+)+)', result.stdout or '')
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _auto_detect_version(version):
    """自动识别 tML 版本号（带缓存：DLL 未变化则跳过检测）。

    成功返回 'v<版本>'（如 'v2026.06.3.6'）并写入 version；失败返回 None。
    """
    dll = _find_tml_dll(version.get('bat_path', ''))
    if not dll:
        return None
    try:
        mtime = os.path.getmtime(dll)
    except OSError:
        return None
    if version.get('_dll_mtime') == mtime and version.get('version_info'):
        return version['version_info']
    raw = _read_tml_version(dll)
    if raw:
        version['version_info'] = 'v' + raw
        version['_dll_mtime'] = mtime
        return version['version_info']
    return None


def _read_tml_versions(paths):
    """一次 PowerShell 调用批量读取多个 DLL 的版本号。

    逐个启动 PowerShell 进程很慢（每个都要加载 .NET 运行时），
    合并为单个 PS 进程循环读取可显著加快首次检测。
    返回 {dll_path: version 或 None}。
    """
    if not paths:
        return {}
    try:
        ps_list = ', '.join("'" + p.replace("'", "''") + "'" for p in paths)
        script = (
            "$r = @{};\n"
            "foreach ($p in @(" + ps_list + ")) {\n"
            "  try { $v = (Get-Item $p).VersionInfo.ProductVersion;\n"
            "        $m = [regex]::Match($v, '\\+([0-9]+(?:\\.[0-9]+)+)');\n"
            "        $r[$p] = $m.Groups[1].Value } catch { $r[$p] = $null }\n"
            "}\n"
            "$r | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command', script],
            capture_output=True, text=True, timeout=30,
            creationflags=_NO_WINDOW
        )
        data = json.loads(result.stdout or '{}')
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _batch_detect_versions(versions):
    """批量自动检测所有版本的 tML 版本号（合并为单次 PowerShell 调用）。

    只检测「无缓存」或「DLL 已变化」的版本；返回是否有更新。
    """
    todo = []
    for v in versions:
        dll = _find_tml_dll(v.get('bat_path', ''))
        if not dll:
            continue
        try:
            mtime = os.path.getmtime(dll)
        except OSError:
            continue
        if v.get('_dll_mtime') == mtime and v.get('version_info'):
            continue   # 缓存命中
        todo.append((v, dll, mtime))
    if not todo:
        return False
    raw_map = _read_tml_versions([dll for _, dll, _ in todo])
    changed = False
    for v, dll, mtime in todo:
        raw = raw_map.get(dll)
        if raw:
            v['version_info'] = 'v' + raw
            v['_dll_mtime'] = mtime
            changed = True
    return changed


# 版本号首次检测的后台预热标记与并发锁
_versions_warm_started = False
_versions_lock = threading.Lock()


def get_global_packs():
    """全局整合包列表。name=目录名（实时对应真实路径，不可改）；desc=描述（存 data.json pack_descs）"""
    packs = []
    if os.path.isdir(GLOBAL_MODPACKS_PATH):
        descs = read_data().get('pack_descs', {})
        for item in os.listdir(GLOBAL_MODPACKS_PATH):
            subdir = os.path.join(GLOBAL_MODPACKS_PATH, item)
            if os.path.isdir(subdir):
                enabled_file = os.path.join(subdir, 'Mods', 'enabled.json')
                if os.path.isfile(enabled_file):
                    packs.append({'id': item, 'name': item, 'desc': descs.get(item, '')})
    packs.sort(key=lambda x: x['name'])
    return packs


def _is_tmodloader_proc(name, cmdline):
    """精确判断进程是否为 tModLoader。

    tML 1.4 通过 dotnet 启动：进程名是 dotnet.exe，cmdline 含 tModLoader.dll；
    旧版直接是 tModLoader.exe。这里避免宽松子串匹配（'tmodloader' 会命中
    任何 cmdline 里出现该词的无关进程，导致误判/误杀）。
    """
    name = (name or '').lower()
    cmdline = ' '.join(cmdline or []).lower()
    if name in ('tmodloader.exe', 'tmodloader'):
        return True
    if name in ('dotnet.exe', 'dotnet') and 'tmodloader.dll' in cmdline:
        return True
    return False


_TML_STATUS_CACHE = {'time': 0.0, 'running': False}
_TML_STATUS_TTL = 3.0   # 大于前端轮询间隔(2s)，保证轮询大多命中缓存


def _detect_tmodloader_running():
    """真实检测 tModLoader 进程是否在运行（不走缓存）"""
    if HAS_PSUTIL:
        # 先按进程名快筛（绝大多数进程不读 cmdline，避免全量 process_iter 慢到 ~1s）
        for proc in psutil.process_iter(['name']):
            name = (proc.info['name'] or '').lower()
            if name not in ('dotnet.exe', 'dotnet', 'tmodloader.exe', 'tmodloader'):
                continue
            try:
                if _is_tmodloader_proc(name, proc.cmdline()):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False
    else:
        # 备选（无 psutil）：tasklist 查 tModLoader.exe + wmic 只查 dotnet 进程
        # 注意：不能全量扫 commandline，否则会命中 bash 等 cmdline 里带 tmodloader 路径的开发进程
        try:
            result = subprocess.run(
                ['tasklist', '/FI', 'IMAGENAME eq tModLoader.exe'],
                capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW
            )
            if 'tmodloader.exe' in result.stdout.lower():
                return True
            # wmic 只查 dotnet 进程（tML 1.4 通过 dotnet 启动 tModLoader.dll）
            result2 = subprocess.run(
                ['wmic', 'process', 'where', "name='dotnet.exe'", 'get', 'name,commandline'],
                capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW
            )
            if 'tmodloader.dll' in result2.stdout.lower():
                return True
            return False
        except:
            return False


def is_tmodloader_running(force=False):
    """检测 tModLoader 进程是否在运行。

    结果缓存 2 秒：前端每秒轮询 /api/status，若每次都全量扫进程表，
    实测 psutil 需 ~0.9~1.7s，会导致接口卡顿。启动/还原等关键操作传 force=True。
    """
    now = time.time()
    if not force and now - _TML_STATUS_CACHE['time'] < _TML_STATUS_TTL:
        return _TML_STATUS_CACHE['running']
    running = _detect_tmodloader_running()
    _TML_STATUS_CACHE['time'] = now
    _TML_STATUS_CACHE['running'] = running
    return running


@app.route('/')
def index():
    # desktop=1 表示在 pywebview 独立窗口内渲染（显示自绘标题栏）
    desktop = request.args.get('desktop') == '1'
    return render_template('index.html', default_mods_path=DEFAULT_MODS_PATH, desktop=desktop)


# ---------- 版本管理 ----------
@app.route('/api/versions', methods=['GET'])
def get_versions():
    """版本列表：立即返回当前缓存；首次启动在后台预热版本号检测，不阻塞首屏。

    用户环境的 PowerShell 启动需 ~3s（首次批量检测 ~4.6s），
    若同步阻塞会导致首屏卡顿 → 改为后台线程预热，前端稍后静默刷新。
    """
    global _versions_warm_started
    with _versions_lock:
        data = read_data()
    if not _versions_warm_started:
        _versions_warm_started = True
        threading.Thread(target=_warm_version_cache, daemon=True).start()
    return jsonify(data['versions'])


def _warm_version_cache():
    """后台预热：批量检测版本号并写回缓存（首次 PowerShell 慢，避免阻塞首屏）"""
    try:
        with _versions_lock:
            data = read_data()
            changed = _batch_detect_versions(data['versions'])
            if changed:
                write_data(data)
    except Exception:
        pass


@app.route('/api/versions', methods=['POST'])
def add_version():
    req = request.get_json()
    name = req.get('name', '').strip()
    bat_path = req.get('bat_path', '').strip()

    if not name or not bat_path:
        return jsonify({'error': '版本名称和启动文件路径必填'}), 400
    if not os.path.isfile(bat_path):
        return jsonify({'error': '启动文件 (.bat) 不存在'}), 400

    if not os.path.isdir(DEFAULT_MODS_PATH):
        os.makedirs(DEFAULT_MODS_PATH, exist_ok=True)

    data = read_data()
    new_version = {
        'id': str(uuid.uuid4()),
        'name': name,
        'version_info': '',  # 版本号自动从 tModLoader.dll 检测
        'bat_path': bat_path,
        'mods_path': DEFAULT_MODS_PATH,
        'packs': []
    }
    _auto_detect_version(new_version)
    data['versions'].append(new_version)
    write_data(data)
    return jsonify(new_version), 201


@app.route('/api/versions/<version_id>', methods=['DELETE'])
def delete_version(version_id):
    data = read_data()
    for i, v in enumerate(data['versions']):
        if v['id'] == version_id:
            if data.get('last_launch') and data['last_launch'].get('version_id') == version_id:
                data['last_launch'] = None
            del data['versions'][i]
            write_data(data)
            return jsonify({'message': '删除成功'}), 200
    return jsonify({'error': '版本不存在'}), 404


@app.route('/api/versions/<version_id>/restore', methods=['POST'])
def restore_version(version_id):
    data = read_data()
    version = get_version(data, version_id)
    if not version:
        return jsonify({'error': '版本不存在'}), 404

    if is_tmodloader_running(force=True):
        return jsonify({'error': 'tModLoader 正在运行，无法还原 enabled.json'}), 409

    mods_dir = version['mods_path']
    backup = os.path.join(mods_dir, 'enabled.json.bk')
    target = os.path.join(mods_dir, 'enabled.json')

    if not os.path.exists(backup):
        return jsonify({'error': '没有备份可恢复'}), 404

    shutil.copy2(backup, target)
    return jsonify({'message': '已还原到上次启动前的状态'}), 200


@app.route('/api/versions/<version_id>/rename', methods=['POST'])
def rename_version(version_id):
    """编辑版本：更新 name / bat_path（版本号自动从 tModLoader.dll 检测）"""
    data = read_data()
    version = get_version(data, version_id)
    if not version:
        return jsonify({'error': '版本不存在'}), 404

    req = request.get_json() or {}
    name = req.get('name')
    bat_path = req.get('bat_path')

    if name is not None:
        name = name.strip()
        if not name:
            return jsonify({'error': '版本名称不能为空'}), 400
        for v in data['versions']:
            if v['id'] != version_id and v['name'] == name:
                return jsonify({'error': '已存在同名版本'}), 400
        version['name'] = name
    if bat_path is not None:
        bat_path = bat_path.strip()
        if not bat_path:
            return jsonify({'error': '启动文件路径不能为空'}), 400
        if not os.path.isfile(bat_path):
            return jsonify({'error': '启动文件 (.bat) 不存在'}), 400
        version['bat_path'] = bat_path
        # 路径变化 → 强制重新检测版本号
        version['_dll_mtime'] = None
        _auto_detect_version(version)

    write_data(data)
    return jsonify({'message': '版本信息已更新', 'version': version}), 200


# ---------- 全局整合包 ----------
@app.route('/api/global_packs', methods=['GET'])
def global_packs():
    return jsonify(get_global_packs())


# ---------- 版本整合包管理 ----------
@app.route('/api/versions/<version_id>/packs', methods=['POST'])
def add_pack(version_id):
    data = read_data()
    version = get_version(data, version_id)
    if not version:
        return jsonify({'error': '版本不存在'}), 404

    req = request.get_json()
    pack_id = req.get('pack_id', '').strip()
    if not pack_id:
        return jsonify({'error': '未指定整合包'}), 400

    if pack_id not in [p['id'] for p in get_global_packs()]:
        return jsonify({'error': '整合包不存在'}), 400

    if pack_id in version['packs']:
        return jsonify({'error': '已添加该整合包'}), 400

    version['packs'].append(pack_id)
    write_data(data)
    return jsonify({'message': '添加成功'}), 200


@app.route('/api/versions/<version_id>/packs/<pack_id>', methods=['DELETE'])
def remove_pack(version_id, pack_id):
    data = read_data()
    version = get_version(data, version_id)
    if not version:
        return jsonify({'error': '版本不存在'}), 404

    if pack_id not in version['packs']:
        return jsonify({'error': '该版本未添加此整合包'}), 400

    version['packs'].remove(pack_id)
    write_data(data)
    return jsonify({'message': '移除成功'}), 200


@app.route('/api/packs/<pack_id>/desc', methods=['POST'])
def set_pack_desc(pack_id):
    """设置整合包描述：只存 data.json 的 pack_descs，目录名不可改"""
    req = request.get_json() or {}
    desc = (req.get('desc') or '').strip()

    old_path = os.path.join(GLOBAL_MODPACKS_PATH, pack_id)
    if not os.path.isdir(old_path):
        return jsonify({'error': '整合包不存在'}), 404

    data = read_data()
    descs = data.setdefault('pack_descs', {})
    if desc:
        descs[pack_id] = desc
    else:
        descs.pop(pack_id, None)  # 清空描述则移除记录
    write_data(data)
    return jsonify({'message': '描述已更新', 'pack_id': pack_id, 'desc': desc}), 200


@app.route('/api/packs/<pack_id>/mods', methods=['GET'])
def pack_mods(pack_id):
    """获取整合包的模组列表：已启用（enabled.json）+ 未启用（来源目录有 .tmod 但不在 enabled.json）。

    source=workshop（默认）：检测该整合包所在版本的 workshop 文件夹模组；
    source=folder：检测整合包文件夹 Mods 目录的模组。均与整合包 enabled.json 对照。
    每个模组标注：本地 Mods 目录 / workshop 目录是否同时存在同名模组（conflict）。"""
    mods_dir = os.path.join(GLOBAL_MODPACKS_PATH, pack_id, 'Mods')
    if not os.path.isdir(mods_dir):
        return jsonify({'error': f'整合包 Mods 目录不存在: {mods_dir}'}), 404

    # 检测来源：默认整合包文件夹 + 本地（统一检测整合包中的模组）；
    # 保留 source 参数兼容旧调用，但前端已不再切换。
    source = (request.args.get('source') or 'folder').lower()
    if source not in ('workshop', 'folder'):
        source = 'folder'
    version_id = request.args.get('version') or ''
    version_name = None

    # enabled.json 里已启用的模组（整合包文件夹）
    enabled = []
    enabled_file = os.path.join(mods_dir, 'enabled.json')
    if os.path.isfile(enabled_file):
        try:
            with open(enabled_file, 'r', encoding='utf-8') as f:
                enabled = json.load(f)
            if not isinstance(enabled, list):
                enabled = []
        except Exception:
            enabled = []
    enabled_set = set(enabled)

    # 定位该整合包所在版本：用其 .bat 推导 workshop 目录；缺失则聚合所有版本
    data = read_data()
    bat_paths = []
    if version_id:
        ver = get_version(data, version_id)
        if ver and ver.get('bat_path'):
            bat_paths = [ver['bat_path']]
            version_name = ver.get('name')
    if not bat_paths:
        bat_paths = _version_bat_paths()

    # 本地 Mods 目录 / workshop 目录 / 整合包文件夹 的模组名集合（用于来源标注 & 并入列表）
    local_mods = list_local_mods()
    local_names = {m['name'] for m in local_mods}
    ws_scan = net_fetch.scan_workshop_mods(bat_paths)
    ws_names = set(ws_scan.keys())
    pack_names = set(os.path.splitext(fn)[0] for fn in os.listdir(mods_dir) if fn.lower().endswith('.tmod'))

    if source == 'folder':
        # 整合包文件夹 + 本地 Mods 目录
        all_mods = sorted(set(pack_names) | local_names, key=str.lower)
    else:
        # workshop 文件夹 + 本地 Mods 目录（默认）
        all_mods = sorted(set(ws_names) | local_names, key=str.lower)

    all_set = set(all_mods)
    # 已启用 = enabled.json 里的、且来源目录里有对应 .tmod（真实能加载的）
    enabled_exist = sorted((enabled_set & all_set), key=str.lower)
    # 未启用 = 来源目录有 .tmod 但不在 enabled.json
    disabled = sorted((all_set - enabled_set), key=str.lower)
    # 缺失 = enabled.json 引用了但来源目录里没有对应 .tmod
    missing = sorted((enabled_set - all_set), key=str.lower)

    # 合并 workshop_id 与 PublishIds，用于匹配 Steam 元数据（标签/更新时间）
    cache = _load_meta_cache()
    ws_ids = {name: v['id'] for name, v in ws_scan.items()} or (cache.get('workshop_ids') or {})
    merged = dict(ws_ids)
    merged.update(get_publish_ids())
    smetas = cache.get('meta', {}) or {}
    now_ts = time.time()

    def build(names):
        result = []
        for n in names:
            pid = merged.get(n)
            sm = smetas.get(str(pid)) if pid else None
            steam_updated = (sm or {}).get('time_updated') or 0
            upd_days = int((now_ts - steam_updated) // 86400) if steam_updated else None
            result.append({
                'name': n,
                'has_local': n in local_names,
                'has_workshop': n in ws_names,
                'in_pack': n in pack_names,
                'conflict': n in local_names and n in ws_names,
                'update_days_ago': upd_days,
                'tags': (sm or {}).get('tags') or [],
                'description': (sm or {}).get('description', ''),
                'subscriptions': (sm or {}).get('subscriptions', 0),
                'favorited': (sm or {}).get('favorited', 0),
                'views': (sm or {}).get('views', 0),
                'time_created': (sm or {}).get('time_created', 0),
                'file_size': (sm or {}).get('file_size', 0),
                'visibility': (sm or {}).get('visibility', 0),
                'banned': (sm or {}).get('banned', 0),
            })
        return result

    return jsonify({
        'pack_id': pack_id,
        'source': source,
        'version_id': version_id,
        'version_name': version_name,
        'enabled': build(enabled_exist),
        'disabled': build(disabled),
        'missing': build(missing)
    }), 200


@app.route('/api/versions/<version_id>/mods', methods=['GET'])
def version_mods(version_id):
    """版本模组列表：检测该版本的 Workshop + 本地 Mods 目录，
    对照游戏加载时读取的 enabled.json（version.mods_path/enabled.json）。
    不检测整合包文件夹。界面与整合包模组列表一致（已启用 / 未启用 / 缺失）。"""
    data = read_data()
    version = get_version(data, version_id)
    if not version:
        return jsonify({'error': '版本不存在'}), 404
    mods_dir = version.get('mods_path')
    if not mods_dir or not os.path.isdir(mods_dir):
        return jsonify({'error': f'Mods 目录不存在: {mods_dir}'}), 400

    # 游戏加载时读取的 enabled.json（共享 Mods 目录）
    enabled = []
    enabled_file = os.path.join(mods_dir, 'enabled.json')
    if os.path.isfile(enabled_file):
        try:
            with open(enabled_file, 'r', encoding='utf-8') as f:
                enabled = json.load(f)
            if not isinstance(enabled, list):
                enabled = []
        except Exception:
            enabled = []
    enabled_set = set(enabled)

    # Workshop（该版本 bat 推导）+ 本地 Mods 目录
    bat_paths = [version['bat_path']] if version.get('bat_path') else []
    if not bat_paths:
        bat_paths = _version_bat_paths()
    ws_scan = net_fetch.scan_workshop_mods(bat_paths)
    ws_names = set(ws_scan.keys())
    local_mods = list_local_mods()
    local_names = {m['name'] for m in local_mods}

    all_mods = sorted(set(ws_names) | local_names, key=str.lower)
    all_set = set(all_mods)
    # 已启用 = enabled.json 里的、且 workshop/本地有对应 .tmod
    enabled_exist = sorted((enabled_set & all_set), key=str.lower)
    # 未启用 = workshop/本地有 .tmod 但不在 enabled.json
    disabled = sorted((all_set - enabled_set), key=str.lower)
    # 缺失 = enabled.json 引用了但 workshop/本地都没有
    missing = sorted((enabled_set - all_set), key=str.lower)

    # 合并 workshop_id 与 PublishIds，用于匹配 Steam 元数据（标签/更新时间）
    cache = _load_meta_cache()
    ws_ids = {name: v['id'] for name, v in ws_scan.items()} or (cache.get('workshop_ids') or {})
    merged = dict(ws_ids)
    merged.update(get_publish_ids())
    smetas = cache.get('meta', {}) or {}
    now_ts = time.time()

    def build(names):
        result = []
        for n in names:
            pid = merged.get(n)
            sm = smetas.get(str(pid)) if pid else None
            steam_updated = (sm or {}).get('time_updated') or 0
            upd_days = int((now_ts - steam_updated) // 86400) if steam_updated else None
            result.append({
                'name': n,
                'has_local': n in local_names,
                'has_workshop': n in ws_names,
                'in_pack': False,
                'conflict': n in local_names and n in ws_names,
                'update_days_ago': upd_days,
                'tags': (sm or {}).get('tags') or [],
                'description': (sm or {}).get('description', ''),
                'subscriptions': (sm or {}).get('subscriptions', 0),
                'favorited': (sm or {}).get('favorited', 0),
                'views': (sm or {}).get('views', 0),
                'time_created': (sm or {}).get('time_created', 0),
                'file_size': (sm or {}).get('file_size', 0),
                'visibility': (sm or {}).get('visibility', 0),
                'banned': (sm or {}).get('banned', 0),
            })
        return result

    return jsonify({
        'version_id': version_id,
        'version_name': version.get('name'),
        'source': 'workshop',
        'enabled': build(enabled_exist),
        'disabled': build(disabled),
        'missing': build(missing)
    }), 200


# ---------- 特殊加载项（禁用本地模组 → 优先加载 workshop 版本） ----------
# 模型：全局配置(global_special) + 每整合包独立配置(special_loads)，
# 未启用独立配置的整合包跟随全局；文件操作在【启动整合包时】应用，退出 tML 时还原。

def list_local_mods():
    """Mods 目录下所有本地模组（含已禁用 .tmod.disabled）"""
    mods = []
    if os.path.isdir(DEFAULT_MODS_PATH):
        for fn in sorted(os.listdir(DEFAULT_MODS_PATH), key=str.lower):
            lower = fn.lower()
            if lower.endswith('.tmod.disabled'):
                mods.append({'name': fn[:-len('.tmod.disabled')], 'disabled': True})
            elif lower.endswith('.tmod'):
                mods.append({'name': os.path.splitext(fn)[0], 'disabled': False})
    return mods


def _apply_disabled_set(disabled_set):
    """按目标禁用集合应用文件操作：集合内 .tmod → .tmod.disabled，集合外已禁用的还原"""
    current_disabled = {}   # name -> 完整文件名 (.tmod.disabled)
    current_enabled = {}    # name -> 完整文件名 (.tmod)
    if os.path.isdir(DEFAULT_MODS_PATH):
        for fn in os.listdir(DEFAULT_MODS_PATH):
            lower = fn.lower()
            if lower.endswith('.tmod.disabled'):
                current_disabled[fn[:-len('.tmod.disabled')]] = fn
            elif lower.endswith('.tmod'):
                current_enabled[os.path.splitext(fn)[0]] = fn

    errors = []
    def _toggle(src_fn, dst_fn):
        try:
            os.rename(os.path.join(DEFAULT_MODS_PATH, src_fn), os.path.join(DEFAULT_MODS_PATH, dst_fn))
        except Exception as e:
            errors.append(f'{src_fn}: {str(e)}')

    for name in disabled_set:
        if name in current_enabled:
            _toggle(current_enabled[name], current_enabled[name] + '.disabled')
    for name in current_disabled:
        if name not in disabled_set:
            _toggle(current_disabled[name], current_disabled[name][:-len('.disabled')])
    return errors


def _pack_special_set(pack_id):
    """某整合包生效的禁用集合：启用独立配置用自己列表，否则跟随全局"""
    data = read_data()
    cfg = data.get('special_loads', {}).get(pack_id, {})
    if cfg.get('independent', cfg.get('active', False)):  # 兼容旧字段 active
        return set(cfg.get('mods', []))
    gcfg = data.get('global_special', {})
    return set(gcfg.get('mods', [])) if gcfg.get('active') else set()


def _apply_pack_special(pack_id):
    """应用某整合包生效的特殊加载配置到 Mods 目录（启动整合包时调用）"""
    return _apply_disabled_set(_pack_special_set(pack_id))


def restore_all_disabled():
    """把所有 .tmod.disabled 还原为 .tmod（保留各配置的选中记忆）。退出 tModLoader 时调用。"""
    restored = []
    if os.path.isdir(DEFAULT_MODS_PATH):
        for fn in os.listdir(DEFAULT_MODS_PATH):
            if fn.lower().endswith('.tmod.disabled'):
                try:
                    os.rename(
                        os.path.join(DEFAULT_MODS_PATH, fn),
                        os.path.join(DEFAULT_MODS_PATH, fn[:-len('.disabled')])
                    )
                    restored.append(fn)
                except Exception:
                    pass
    return restored


@app.route('/api/special/<pack_id>', methods=['GET'])
def special_get(pack_id):
    """获取整合包特殊加载项配置 + 本地 Mods 目录模组列表"""
    data = read_data()
    special = data.setdefault('special_loads', {})
    cfg = special.get(pack_id, {'independent': False, 'mods': []})
    return jsonify({
        'pack_id': pack_id,
        'independent': cfg.get('independent', cfg.get('active', False)),  # 兼容旧字段 active
        'disabled_mods': cfg.get('mods', []),
        'mods': list_local_mods()
    }), 200


@app.route('/api/global_special', methods=['GET'])
def global_special_get():
    """获取全局特殊加载配置"""
    data = read_data()
    gcfg = data.setdefault('global_special', {'active': False, 'mods': []})
    return jsonify({
        'active': gcfg.get('active', False),
        'disabled_mods': gcfg.get('mods', []),
        'mods': list_local_mods()
    }), 200


@app.route('/api/special_status', methods=['GET'])
def special_status():
    """返回全局开关 + 每个整合包特殊加载是否生效（独立配置 || 全局开）"""
    data = read_data()
    global_active = bool(data.get('global_special', {}).get('active'))
    special = data.get('special_loads', {})
    packs = {}
    for pid, cfg in special.items():
        independent = bool(cfg.get('independent', cfg.get('active', False)))
        packs[pid] = {'independent': independent, 'effective': independent or global_active}
    return jsonify({'global_active': global_active, 'packs': packs}), 200


@app.route('/api/special/<pack_id>', methods=['POST'])
def special_set(pack_id):
    """保存整合包特殊加载项：只存配置（independent + mods），启动该整合包时应用"""
    req = request.get_json() or {}
    independent = bool(req.get('independent'))
    disabled_mods = req.get('mods') or []

    if not os.path.isdir(os.path.join(GLOBAL_MODPACKS_PATH, pack_id)):
        return jsonify({'error': '整合包不存在'}), 404

    data = read_data()
    special = data.setdefault('special_loads', {})
    special[pack_id] = {'independent': independent, 'mods': sorted(set(disabled_mods))}
    write_data(data)
    return jsonify({'message': '设置已保存（启动该整合包时生效）'}), 200


@app.route('/api/global_special', methods=['POST'])
def global_special_set():
    """保存全局特殊加载配置：只存配置（active + mods），未启用独立配置的整合包启动时应用"""
    req = request.get_json() or {}
    active = bool(req.get('active'))
    disabled_mods = req.get('mods') or []

    data = read_data()
    data['global_special'] = {'active': active, 'mods': sorted(set(disabled_mods))}
    write_data(data)
    return jsonify({'message': '全局设置已保存（未启用独立配置的整合包启动时生效）'}), 200


# ---------- 模组元数据（本名/图标，来自 Steam 创意工坊） ----------
META_FILE = os.path.join(BASE_DIR, 'mod_meta.json')


def _load_meta_cache():
    if os.path.isfile(META_FILE):
        try:
            with open(META_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'meta': {}, 'steam_api_key': '', 'updated': 0}


def _save_meta_cache(cache):
    try:
        with open(META_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_publish_ids():
    """读共享 Mods 目录的 ModFolderData.json.PublishIds：本地模组内部名 → workshop ID"""
    mfd = os.path.join(DEFAULT_MODS_PATH, 'ModFolderData.json')
    if not os.path.isfile(mfd):
        return {}
    try:
        with open(mfd, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('PublishIds', {}) or {}
    except Exception:
        return {}


def _version_bat_paths():
    """所有版本的 .bat 启动路径（供 net_fetch.scan_workshop_mods 推导 tML 安装目录）"""
    try:
        data = read_data()
        return [v.get('bat_path', '') for v in data.get('versions', []) if v.get('bat_path')]
    except Exception:
        return []


def refresh_mod_meta(force=False):
    """本地逆向 .tmod 优先 + Steam 补全 → 写缓存；返回 (cache, 失败原因)"""
    cache = _load_meta_cache()
    publish_ids = get_publish_ids()
    ws = net_fetch.scan_workshop_mods(_version_bat_paths())
    cache['workshop_ids'] = {name: v['id'] for name, v in ws.items()}
    # 合并映射：PublishIds 优先，workshop 目录补充
    merged = dict(cache['workshop_ids'])
    merged.update(publish_ids)

    # 收集所有模组名：映射 keys + 本地 Mods + 整合包目录
    all_names = set(merged.keys())
    all_names.update(m['name'] for m in list_local_mods())
    if os.path.isdir(GLOBAL_MODPACKS_PATH):
        try:
            for pack in os.listdir(GLOBAL_MODPACKS_PATH):
                pm = os.path.join(GLOBAL_MODPACKS_PATH, pack, 'Mods')
                if os.path.isdir(pm):
                    for fn in os.listdir(pm):
                        if fn.lower().endswith('.tmod'):
                            all_names.add(os.path.splitext(fn)[0])
        except Exception:
            pass

    # 本地逆向优先：解析每个 .tmod 的 icon.png + Info（独立模块 tmod_parser）
    local_meta = {}
    for name in all_names:
        tmod = find_tmod(name, DEFAULT_MODS_PATH, GLOBAL_MODPACKS_PATH)
        if tmod:
            meta = parse_tmod_meta(tmod)
            if meta and (meta.get('icon_bytes') or meta.get('display_name')):
                local_meta[name] = {
                    'display_name': meta.get('display_name', ''),
                    'author': meta.get('author', ''),
                    'icon': save_icon(name, meta.get('icon_bytes'), LOCAL_ICON_DIR),
                    'version': meta.get('version', ''),
                }
    cache['local_meta'] = local_meta

    # Steam 补全：确保每个 workshop 模组都有元数据（含 time_updated，供更新检测用）
    covered = {n for n, lm in local_meta.items() if lm.get('display_name') or lm.get('icon')}
    need = []
    for name, pid in merged.items():
        if name in covered:
            continue
        meta_entry = cache.get('meta', {}).get(str(pid)) or {}
        if force or not meta_entry or not meta_entry.get('time_updated') or not meta_entry.get('tags') or not meta_entry.get('file_size'):
            need.append(pid)
    # workshop 目录里的模组也确保有 time_updated / tags
    for v in ws.values():
        pid = str(v['id'])
        meta_entry = cache.get('meta', {}).get(pid) or {}
        if force or not meta_entry or not meta_entry.get('time_updated') or not meta_entry.get('tags') or not meta_entry.get('file_size'):
            if pid not in need:
                need.append(pid)
    errors = {}
    if need:
        fetched, errors = net_fetch.steam_query_publish_details(need)
        cache.setdefault('meta', {}).update(fetched)

    # 差异/更新检测已废弃（mtime 对比不可靠），清理旧缓存字段
    cache.pop('diffs', None)
    cache.pop('updates', None)
    cache['updated'] = time.time()
    _save_meta_cache(cache)
    return cache, errors


@app.route('/api/mod_meta', methods=['GET'])
def mod_meta():
    """模组元数据列表：本地 Mods ∪ PublishIds ∪ workshop_ids ∪ 整合包目录。
    每个模组：本地逆向 .tmod 优先（icon.png + Info 本名），Steam 缓存兜底。"""
    cache = _load_meta_cache()
    publish_ids = get_publish_ids()
    ws_ids = cache.get('workshop_ids', {}) or {}
    # 合并映射：PublishIds 优先，workshop 目录补充
    merged = dict(ws_ids)
    merged.update(publish_ids)
    local_disabled = {m['name']: m['disabled'] for m in list_local_mods()}
    local_meta = cache.get('local_meta', {}) or {}
    # 名字集合：本地 Mods + 映射 + 整合包目录 + 本地逆向结果
    all_names = set(local_disabled) | set(merged.keys()) | set(local_meta.keys())
    if os.path.isdir(GLOBAL_MODPACKS_PATH):
        try:
            for pack in os.listdir(GLOBAL_MODPACKS_PATH):
                pm = os.path.join(GLOBAL_MODPACKS_PATH, pack, 'Mods')
                if os.path.isdir(pm):
                    for fn in os.listdir(pm):
                        if fn.lower().endswith('.tmod'):
                            all_names.add(os.path.splitext(fn)[0])
        except Exception:
            pass
    mods = []
    patched_version = False
    for name in sorted(all_names):
        lm = local_meta.setdefault(name, {})
        pid = merged.get(name)
        smeta = cache.get('meta', {}).get(str(pid)) if pid else None
        # 本地逆向优先，Steam 兜底
        display_name = lm.get('display_name') or (smeta or {}).get('name', '')
        icon = lm.get('icon') or (smeta or {}).get('icon', '')
        smeta0 = smeta or {}
        steam_updated = smeta0.get('time_updated') or 0
        update_days_ago = int((time.time() - steam_updated) // 86400) if steam_updated else None
        # 模组版本号：缓存缺失时现场解析一次并回写缓存（后续直接命中）
        version = lm.get('version', '')
        if not version:
            tmod = find_tmod(name, DEFAULT_MODS_PATH, GLOBAL_MODPACKS_PATH)
            if tmod:
                m2 = parse_tmod_meta(tmod)
                if m2 and m2.get('version'):
                    version = m2['version']
                    lm['version'] = version
                    patched_version = True
        mods.append({
            'name': name,
            'disabled': local_disabled.get(name, False),
            'workshop_id': str(pid) if pid else None,
            'display_name': display_name,
            'icon': icon,
            'version': version,
            'update_days_ago': update_days_ago,
            'tags': smeta0.get('tags') or [],
            'description': smeta0.get('description', ''),
            'subscriptions': smeta0.get('subscriptions', 0),
            'favorited': smeta0.get('favorited', 0),
            'views': smeta0.get('views', 0),
            'time_created': smeta0.get('time_created', 0),
            'file_size': smeta0.get('file_size', 0),
            'visibility': smeta0.get('visibility', 0),
            'banned': smeta0.get('banned', 0),
        })
    if patched_version:
        cache['local_meta'] = local_meta
        _save_meta_cache(cache)
    # 首次（还没本地逆向过）或 Steam 缺数据 → 触发一次自动刷新
    needs_refresh = ('local_meta' not in cache) or any(
        m['workshop_id'] and not m['display_name'] and not m['icon'] for m in mods)
    return jsonify({
        'mods': mods,
        'needs_refresh': needs_refresh,
        'updated': cache.get('updated', 0)
    }), 200


@app.route('/api/mod_meta/refresh', methods=['POST'])
def mod_meta_refresh():
    req = request.get_json() or {}
    force = bool(req.get('force'))
    cache, errors = refresh_mod_meta(force=force)
    return jsonify({
        'message': '模组元数据已刷新',
        'updated': cache.get('updated', 0),
        'success_count': len(cache.get('meta', {})),
        'failed': errors
    }), 200




# ---------- 启动整合包 ----------
@app.route('/api/versions/<version_id>/launch/<pack_id>', methods=['POST'])
def launch(version_id, pack_id):
    data = read_data()
    version = get_version(data, version_id)
    if not version:
        return jsonify({'error': '版本不存在'}), 404

    if pack_id not in version['packs']:
        return jsonify({'error': '该版本未添加此整合包'}), 400

    # 检查是否已有 tModLoader 在运行
    if is_tmodloader_running(force=True):
        return jsonify({'error': 'tModLoader 正在运行，无法启动新的实例'}), 409

    mods_dir = version['mods_path']
    target = os.path.join(mods_dir, 'enabled.json')
    backup = os.path.join(mods_dir, 'enabled.json.bk')

    src = os.path.join(GLOBAL_MODPACKS_PATH, pack_id, 'Mods', 'enabled.json')
    if not os.path.isfile(src):
        return jsonify({'error': f'整合包文件缺失: {src}'}), 404

    if not os.path.isdir(mods_dir):
        return jsonify({'error': f'Mods 目录不存在: {mods_dir}'}), 400

    # 备份
    try:
        if os.path.exists(target):
            shutil.copy2(target, backup)
        else:
            with open(backup, 'w', encoding='utf-8') as f:
                json.dump([], f)
    except Exception as e:
        return jsonify({'error': f'备份失败: {str(e)}'}), 500

    # 覆盖
    try:
        shutil.copy2(src, target)
    except Exception as e:
        if os.path.exists(backup):
            shutil.copy2(backup, target)
        return jsonify({'error': f'覆盖失败: {str(e)}'}), 500

    # 应用特殊加载（禁用本地模组 → 优先 workshop 版本）
    sp_errors = _apply_pack_special(pack_id)
    sp_set = sorted(_pack_special_set(pack_id))  # 本次实际生效的禁用集合，供「启动当前配置」复用

    # 启动游戏
    try:
        subprocess.Popen([version['bat_path']], shell=True, cwd=os.path.dirname(version['bat_path']))
        # 记录最近启动
        data = read_data()
        data['last_launch'] = {
            'version_id': version_id,
            'pack_id': pack_id,
            'disabled': sp_set
        }
        write_data(data)
        message = '游戏启动成功！'
        if sp_errors:
            message += '（特殊加载部分文件操作失败: ' + '; '.join(sp_errors) + '）'
        return jsonify({'message': message}), 200
    except Exception as e:
        if os.path.exists(backup):
            shutil.copy2(backup, target)
        return jsonify({'error': f'启动失败: {str(e)}'}), 500


@app.route('/api/launch_current', methods=['POST'])
def launch_current():
    """用「上次启动」用的 .bat 直接启动，enabled.json 不做任何修改。

    与整合包启动一致：启动前应用特殊加载（禁用本地模组 → 优先 workshop 版本）。
    若没有上次启动记录（last_launch 为空），返回原因供前端弹窗显示。
    """
    data = read_data()
    last = data.get('last_launch')
    if not last or not last.get('version_id'):
        return jsonify({'error': '没有上次启动记录，无法按上次方式启动。请先从整合包或版本启动一次游戏。'}), 400
    version = get_version(data, last['version_id'])
    if not version:
        return jsonify({'error': '上次启动的版本已不存在'}), 400
    if is_tmodloader_running(force=True):
        return jsonify({'error': 'tModLoader 正在运行'}), 409
    if not os.path.isfile(version['bat_path']):
        return jsonify({'error': '启动文件不存在'}), 400

    try:
        # 与整合包启动一致：启动前应用特殊加载（禁用本地模组 → 优先 workshop 版本）。
        # 优先复用「上次启动」记录的禁用集合，保证直接点启动时本地禁用状态与上次完全一致；
        # 旧数据没有 disabled 字段时，回退为按当前配置计算。
        pack_id = last.get('pack_id')
        disabled = last.get('disabled')
        if disabled is not None:
            sp_errors = _apply_disabled_set(set(disabled))
        elif pack_id:
            sp_errors = _apply_pack_special(pack_id)
        else:
            sp_errors = []
        subprocess.Popen([version['bat_path']], shell=True, cwd=os.path.dirname(version['bat_path']))
        message = f'已用上次的启动方式启动（{version["name"]}）'
        if sp_errors:
            message += '（特殊加载部分文件操作失败: ' + '; '.join(sp_errors) + '）'
        return jsonify({'message': message}), 200
    except Exception as e:
        return jsonify({'error': f'启动失败: {str(e)}'}), 500


@app.route('/api/versions/<version_id>/launch_direct', methods=['POST'])
def launch_direct(version_id):
    """直接启动该版本：仅用当前 Mods 目录的 enabled.json 原样启动，不做任何修改
    （不覆盖 enabled.json、不应用特殊加载）。适合想用当前模组状态直接玩的情况。"""
    data = read_data()
    version = get_version(data, version_id)
    if not version:
        return jsonify({'error': '版本不存在'}), 404
    if is_tmodloader_running(force=True):
        return jsonify({'error': 'tModLoader 正在运行，无法启动新的实例'}), 409
    if not os.path.isfile(version['bat_path']):
        return jsonify({'error': '启动文件不存在'}), 400

    try:
        subprocess.Popen([version['bat_path']], shell=True, cwd=os.path.dirname(version['bat_path']))
        # 记录最近启动（无整合包、无特殊加载），后续「启动当前配置」沿用本版本
        data = read_data()
        data['last_launch'] = {
            'version_id': version_id,
            'pack_id': None,
            'disabled': []
        }
        write_data(data)
        return jsonify({'message': f'已直接启动（{version["name"]}），使用当前 enabled.json'}), 200
    except Exception as e:
        return jsonify({'error': f'启动失败: {str(e)}'}), 500


@app.route('/api/current_enabled', methods=['GET'])
def current_enabled():
    """当前 Mods 目录 enabled.json 里启用的模组列表"""
    target = os.path.join(DEFAULT_MODS_PATH, 'enabled.json')
    mods = []
    if os.path.isfile(target):
        try:
            with open(target, 'r', encoding='utf-8') as f:
                mods = json.load(f)
            if not isinstance(mods, list):
                mods = []
        except Exception:
            mods = []
    return jsonify({'mods': mods, 'count': len(mods)}), 200


# ---------- 状态检测 API ----------
@app.route('/api/status', methods=['GET'])
def get_status():
    try:
        data = read_data()
        last = data.get('last_launch')
        version_name = None
        pack_name = None
        version_path = None

        if last:
            version = get_version(data, last['version_id'])
            if version:
                version_name = f"{version['name']} ({version['version_info']})"
                version_path = version['bat_path']
                pack_id = last['pack_id']
                packs = get_global_packs()
                for p in packs:
                    if p['id'] == pack_id:
                        pack_name = p['name']
                        break

        running = is_tmodloader_running()

        # 存在性检测：版本（bat 启动文件）/ 整合包（文件夹）是否被移动或删除
        missing_versions = []
        for v in data.get('versions', []):
            bat = v.get('bat_path')
            if not (bat and os.path.isfile(bat)):
                missing_versions.append({'id': v.get('id'), 'name': v.get('name') or v.get('id')})
        missing_packs = []
        seen_packs = set()
        for v in data.get('versions', []):
            for pid in (v.get('packs') or []):
                if pid in seen_packs:
                    continue
                seen_packs.add(pid)
                if not os.path.isdir(os.path.join(GLOBAL_MODPACKS_PATH, pid)):
                    missing_packs.append({'id': pid, 'name': pid})

        return jsonify({
            'backend_ok': True,
            'tmodloader_running': running,
            'version_name': version_name,
            'pack_name': pack_name,
            'version_path': version_path,
            'missing_versions': missing_versions,
            'missing_packs': missing_packs
        })
    except Exception as e:
        return jsonify({
            'backend_ok': True,
            'tmodloader_running': False,
            'version_name': None,
            'pack_name': None,
            'version_path': None,
            'missing_versions': [],
            'missing_packs': [],
            'error': str(e)
        }), 200


# ---------- 强制关闭 tModLoader ----------
@app.route('/api/kill_tmodloader', methods=['POST'])
def kill_tmodloader():
    try:
        killed = False
        if HAS_PSUTIL:
            for proc in psutil.process_iter(['name', 'cmdline']):
                try:
                    if _is_tmodloader_proc(proc.info['name'], proc.info['cmdline']):
                        proc.kill()
                        killed = True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        else:
            # 备选（无 psutil）：tasklist 杀 tModLoader.exe + wmic 精确杀 dotnet
            try:
                result = subprocess.run(
                    ['tasklist', '/FI', 'IMAGENAME eq tModLoader.exe'],
                    capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW
                )
                if 'tmodloader.exe' in result.stdout.lower():
                    subprocess.run(['taskkill', '/F', '/IM', 'tModLoader.exe'], capture_output=True, creationflags=_NO_WINDOW)
                    killed = True
            except:
                pass
            try:
                # 通过 wmic 拿到 dotnet 进程的命令行与 PID，只杀命令行含 tModLoader.dll 的
                result = subprocess.run(
                    ['wmic', 'process', 'where', 'name="dotnet.exe"', 'get', 'commandline,processid'],
                    capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW
                )
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if not line or 'tmodloader.dll' not in line.lower():
                        continue
                    # 行尾最后一段是 PID（commandline 可能含空格，故从右切）
                    parts = line.rsplit(None, 1)
                    if parts and parts[-1].isdigit():
                        subprocess.run(['taskkill', '/F', '/PID', parts[-1]], capture_output=True, creationflags=_NO_WINDOW)
                        killed = True
            except:
                pass

        # 注意：不清空 last_launch，保留最近启动记录，方便用户接着点"还原"
        if killed:
            return jsonify({'message': '游戏已强制关闭'}), 200
        else:
            return jsonify({'error': '未找到正在运行的 tModLoader 进程'}), 404
    except Exception as e:
        return jsonify({'error': f'关闭失败: {str(e)}'}), 500


# ---------- 打开 ModPacks 文件夹 ----------
@app.route('/api/open_modpacks', methods=['POST'])
def open_modpacks():
    if not os.path.exists(GLOBAL_MODPACKS_PATH):
        os.makedirs(GLOBAL_MODPACKS_PATH, exist_ok=True)
    try:
        if os.name == 'nt':
            os.startfile(GLOBAL_MODPACKS_PATH)
        else:
            subprocess.Popen(['open' if os.name == 'darwin' else 'xdg-open', GLOBAL_MODPACKS_PATH])
        return jsonify({'message': '已打开 ModPacks 文件夹'}), 200
    except Exception as e:
        return jsonify({'error': f'打开失败: {str(e)}'}), 500


# ---------- 独立窗口：关闭即退出 ----------
def _terminate_process():
    """强杀当前进程（必须正确设置 HANDLE 类型，64 位下否则句柄被 32 位截断导致失败）"""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), 0)
    except Exception:
        os._exit(0)


@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    """独立窗口关闭：立即终止整个进程（窗口随进程销毁，后端随之退出）。

    前端不等待响应（fire-and-forget），收到请求即 TerminateProcess 强杀，实现瞬间关闭。
    """
    _terminate_process()


# 显示开关设置持久化（存 data.json，跨会话可靠；localStorage 在 WebView2 强杀下会丢）
_meta_settings_lock = threading.Lock()


@app.route('/api/meta_settings', methods=['GET'])
def meta_settings_get():
    with _meta_settings_lock:
        return jsonify(read_data().get('meta_settings', {}))


@app.route('/api/meta_settings', methods=['POST'])
def meta_settings_set():
    payload = request.get_json(silent=True) or {}
    with _meta_settings_lock:
        data = read_data()
        data.setdefault('meta_settings', {}).update(payload)
        write_data(data)
    return jsonify({'ok': True})


# 收藏持久化（存 data.json，跨会话可靠；localStorage 在 WebView2 强杀下会丢）
_favorites_lock = threading.Lock()


@app.route('/api/favorites', methods=['GET'])
def favorites_get():
    with _favorites_lock:
        return jsonify(read_data().get('favorites', {'versions': [], 'packs': []}))


@app.route('/api/favorites', methods=['POST'])
def favorites_set():
    payload = request.get_json(silent=True) or {}
    versions = payload.get('versions')
    packs = payload.get('packs')
    seq = payload.get('seq', 0)
    if versions is None and packs is None:
        return jsonify({'ok': False, 'error': 'no data'}), 400
    with _favorites_lock:
        data = read_data()
        fav = data.setdefault('favorites', {'versions': [], 'packs': []})
        # 只接受更新的 seq，快速连点（请求乱序到达）时保证最后一次生效
        if seq and seq < fav.get('_seq', 0):
            return jsonify({'ok': True, 'stale': True})
        if isinstance(versions, list):
            fav['versions'] = [str(v) for v in versions]
        if isinstance(packs, list):
            fav['packs'] = [str(p) for p in packs]
        if seq:
            fav['_seq'] = seq
        write_data(data)
    return jsonify({'ok': True})


# tModLoader 版本更新检测已抽离到 modules/net_fetch.py
# （含 GitHub 拉取 / 失败重试 / 离线判定 / 版本比较，供 /api/tmod_update 调用）


@app.route('/api/pick_bat', methods=['POST'])
def pick_bat():
    """弹出系统文件选择框，选择 .bat 启动文件（tkinter 独立线程，避免占用 pywebview UI 线程）"""
    result = {'path': ''}
    done = threading.Event()

    def _pick():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)  # 尽量置于最前
            path = filedialog.askopenfilename(
                title='选择启动文件 (.bat)',
                filetypes=[('启动脚本', '*.bat'), ('所有文件', '*.*')]
            )
            root.destroy()
            result['path'] = path or ''
        except Exception:
            result['path'] = ''
        finally:
            done.set()

    threading.Thread(target=_pick, daemon=True).start()
    done.wait(timeout=120)  # 阻塞本请求直到用户选择 / 取消 / 超时
    return jsonify(result)


@app.route('/api/tmod_update')
def tmod_update_get():
    """返回最新 stable/preview 版本；缓存/失败重试/离线判定逻辑在 net_fetch 模块"""
    return jsonify(net_fetch.get_tmod_update())


@app.route('/api/open_url', methods=['POST'])
def open_url():
    """用系统默认浏览器打开外部链接（点击更新徽章时跳转 GitHub Releases）"""
    req = request.get_json(silent=True) or {}
    url = (req.get('url') or '').strip()
    if url.startswith('http://') or url.startswith('https://'):
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': '非法链接'}), 400


# 页面就绪通知：前端页面加载完成后调用，显示窗口（避免首帧纯色闪烁）
_desktop_window = None


@app.route('/api/ready', methods=['POST'])
def ready():
    """前端页面加载完成后调用 → 显示窗口（避免首帧纯色闪烁）"""
    global _desktop_window
    try:
        if _desktop_window:
            _desktop_window.show()
    except Exception:
        pass
    return jsonify({'ok': True})


def _monitor_tmodloader():
    """后台监控：检测 tModLoader 从运行→退出，触发 .disabled 还原"""
    was_running = is_tmodloader_running()
    while True:
        time.sleep(3)
        try:
            running = is_tmodloader_running()
            if was_running and not running:
                restore_all_disabled()
            was_running = running
        except Exception:
            pass


def _free_port():
    """找一个空闲端口"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _hide_console_and_redirect_log():
    """独立窗口模式：隐藏控制台黑窗，日志重定向到 app.log。

    无论用 python app.py 还是 pythonw 启动，都消除后端控制台窗口；
    print/werkzeug 日志写入 BASE_DIR/app.log 便于排查。
    """
    try:
        log_path = os.path.join(BASE_DIR, 'app.log')
        log_file = open(log_path, 'a', encoding='utf-8', buffering=1)
        sys.stdout = log_file
        sys.stderr = log_file
        print(f'[{time.strftime("%H:%M:%S")}] desktop mode: console hidden, logs -> app.log',
              flush=True)
    except Exception:
        pass
    if os.name == 'nt':
        try:
            import ctypes
            # 1) 直接隐藏 console 窗口（ShowWindow 比 FreeConsole 更可靠，能隐藏 py.exe 等宿主持有的窗口）
            hwnd_console = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd_console:
                ctypes.windll.user32.ShowWindow(hwnd_console, 0)  # SW_HIDE
            # 2) 分离进程与控制台关联
            ctypes.windll.kernel32.FreeConsole()
        except Exception:
            pass


def run_desktop(show_console=False):
    """pywebview 独立窗口模式：关闭窗口即退出后端。

    - frameless：去掉系统标题栏白条
    - easy_drag=False + 原生 WM_NCLBUTTONDOWN 拖动：系统级流畅，且只在标题栏触发
    - Flask 在后台 daemon 线程运行；窗口关闭 → 进程退出 → 后端自动退出
    - show_console=False：隐藏控制台黑窗，日志写 app.log；True（debug.py）保留控制台显示日志
    """
    try:
        import webview
    except ImportError:
        print('未安装 pywebview，请先执行: pip install pywebview')
        return
    if not show_console:
        # 隐藏控制台黑窗 + 日志重定向到 app.log
        _hide_console_and_redirect_log()
    port = _free_port()
    threading.Thread(
        target=lambda: app.run(host='127.0.0.1', port=port,
                               debug=False, use_reloader=False),
        daemon=True
    ).start()
    # 等待后端就绪
    deadline = time.time() + 15
    ready = False
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                ready = True
                break
        except OSError:
            time.sleep(0.1)
    if not ready:
        print('后端启动失败')
        return

    # Win32 原生拖动（WM_NCLBUTTONDOWN + HTCAPTION，系统级流畅）
    user32 = None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                                 ctypes.c_void_p, wintypes.UINT]
        user32.SystemParametersInfoW.restype = wintypes.BOOL
    except Exception:
        user32 = None
    hwnd_state = {'hwnd': None}
    max_state = {'saved': None}   # 最大化前窗口位置大小，None 表示当前未处于工作区最大化

    class _WindowApi:
        """暴露给前端 JS 的桥接（pywebview js_api）"""
        def start_drag(self):
            """标题栏按下 → 交给系统拖动循环（流畅，鼠标不滞后）。

            js_api 回调运行在独立线程，Win32 拖动必须在 UI 线程 → 用 native.Invoke 调度。
            """
            if not user32 or not hwnd_state['hwnd']:
                return
            def _do_drag():
                try:
                    pt = wintypes.POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    lparam = (pt.y << 16) | (pt.x & 0xFFFF)   # WM_NCLBUTTONDOWN 需要鼠标坐标
                    user32.ReleaseCapture()
                    user32.SendMessageW(hwnd_state['hwnd'], 0xA1, 2, lparam)
                except Exception:
                    pass
            try:
                native = window.native
                if native is not None and hasattr(native, 'Invoke'):
                    from System import Action
                    native.Invoke(Action(_do_drag))
                else:
                    _do_drag()
            except Exception:
                _do_drag()

        def toggle_fullscreen(self):
            """最大化到工作区（不覆盖任务栏，等同普通软件最大化）/ 还原 切换。
            pywebview 原生 fullscreen 会覆盖任务栏；frameless 窗口用 ShowWindow(SW_MAXIMIZE)
            也会被当作全屏处理，故改用 SetWindowPos 精确铺满工作区（保留任务栏）。"""
            if not user32 or not hwnd_state['hwnd']:
                return False
            def _do():
                try:
                    hwnd = hwnd_state['hwnd']
                    if max_state['saved'] is not None:
                        # 还原到最大化前的位置大小
                        x, y, w, h = max_state['saved']
                        user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x14)  # SWP_NOZORDER|SWP_NOACTIVATE
                        max_state['saved'] = None
                    else:
                        # 记录当前窗口位置大小
                        r = wintypes.RECT()
                        user32.GetWindowRect(hwnd, ctypes.byref(r))
                        max_state['saved'] = (r.left, r.top, r.right - r.left, r.bottom - r.top)
                        # 铺满工作区（SPI_GETWORKAREA=0x30，不含任务栏）
                        wa = wintypes.RECT()
                        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)
                        user32.SetWindowPos(hwnd, 0, wa.left, wa.top,
                                            wa.right - wa.left, wa.bottom - wa.top, 0x14)
                except Exception:
                    pass
            try:
                native = window.native
                if native is not None and hasattr(native, 'Invoke'):
                    from System import Action
                    native.Invoke(Action(_do))
                else:
                    _do()
            except Exception:
                _do()
            return max_state['saved'] is not None  # 切换后是否处于最大化

        def is_fullscreen(self):
            """查询当前是否处于工作区最大化状态（供前端同步按钮图标）"""
            return max_state['saved'] is not None

        def minimize(self):
            """最小化窗口"""
            try:
                window.minimize()
            except Exception:
                pass

        def shutdown(self):
            """关闭应用：终止整个进程 → 窗口销毁 → 后端退出（瞬间生效）"""
            _terminate_process()

    window = webview.create_window(
        'tModLoader 启动器', f'http://127.0.0.1:{port}/?desktop=1',
        width=1280, height=820, min_size=(960, 640),
        frameless=True, easy_drag=False, shadow=False,
        # Win10 WebView2 的 transparent 会失败（四角露白）→ 用深色窗口底色 + 页面圆角壳近似
        background_color='#16213e',
        # 先隐藏：前端页面加载完成后通过 /api/ready 通知显示，避免首帧纯色闪烁
        hidden=True,
        js_api=_WindowApi())

    global _desktop_window
    _desktop_window = window

    def _show_window():
        try:
            window.show()
        except Exception:
            pass

    # 兜底：若前端通知未到达，1.5s 后强制显示（防页面异常导致窗口永不显示）
    threading.Thread(target=lambda: (time.sleep(1.5), _show_window()), daemon=True).start()

    def _capture_hwnd():
        """窗口完全创建后（native 才赋值）拿到原生句柄，供拖动/图标用"""
        def _grab():
            time.sleep(3)
            try:
                native = window.native or webview.windows[0].native
                hwnd_state['hwnd'] = native.Handle.ToInt32()
                icon_util.set_window_icon(   # 设置窗口/任务栏图标（独立模块）
                    hwnd_state['hwnd'],
                    os.path.join(BASE_DIR, 'static', 'tmod_icon.dat'))
            except Exception:
                pass
        threading.Thread(target=_grab, daemon=True).start()

    # private_mode=False：WebView2 用持久 user data（%APPDATA%/pywebview），localStorage 才能跨会话保存
    webview.start(func=_capture_hwnd, private_mode=False)


if __name__ == '__main__':
    if not os.environ.get('WERKZEUG_RUN_MAIN'):
        threading.Thread(target=_monitor_tmodloader, daemon=True).start()
        if '--web' in sys.argv:
            # 浏览器模式（原行为）
            threading.Timer(1.5, lambda: webbrowser.open('http://127.0.0.1:5000')).start()
            app.run(debug=True, host='127.0.0.1', port=5000)
        else:
            # 默认：pywebview 独立窗口
            run_desktop()