# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 HopingStar
"""tModLoader .tmod 文件逆向解析器（独立模块）。

用途：从 .tmod 文件里提取游戏内图标（icon.png）和模组本名（Info.displayName），
用于在本地补全无 Steam workshop ID 的模组元数据。

格式依据社区文档（Trivaxy/.tmod file format）：
  TMOD 魔数 + 7-bit 长度字符串(tML版本) + SHA1(20B) + 签名(256B) + 数据长度(U32)
  + 模组名(字符串) + 模组版本(字符串) + 文件数(I32)
  + [文件数] 条目(文件名/未压缩长度/压缩长度)
  + [文件数] 数据（条目中的文件，DEFLATE 压缩，raw 流）

本模块不依赖启动器后端，路径参数由调用方传入，便于复用和测试。
"""
import os
import re
import struct
import zlib


def _read_7bit(f):
    """.NET BinaryReader 的 7-bit 编码整数"""
    result, shift, b = 0, 0, 0x80
    while b & 0x80:
        b = f.read(1)[0]
        result |= (b & 0x7f) << shift
        shift += 7
    return result


def _read_tmod_str(f):
    n = _read_7bit(f)
    return f.read(n).decode('utf-8', 'replace')


def _read_7bit_bytes(data, pos):
    result, shift, b = 0, 0, 0x80
    while b & 0x80:
        b = data[pos]
        pos += 1
        result |= (b & 0x7f) << shift
        shift += 7
    return result, pos


def _read_bytes_str(data, pos):
    n, pos = _read_7bit_bytes(data, pos)
    return data[pos:pos + n].decode('utf-8', 'replace'), pos + n


def parse_tmod_meta(tmod_path):
    """逆向解析 .tmod：只读文件头 + icon.png / Info 条目数据（不读整个文件）。

    返回 dict：{'name', 'display_name', 'author', 'icon_bytes', 'version'}
    解析失败或没有可用数据时返回 None。
    """
    try:
        with open(tmod_path, 'rb') as f:
            if f.read(4) != b'TMOD':
                return None
            _read_tmod_str(f)          # tML 版本
            f.read(20)                 # SHA1
            f.read(256)                # 签名
            f.read(4)                  # 数据长度
            mod_name = _read_tmod_str(f)
            mod_version = _read_tmod_str(f)   # 文件头里的模组版本
            count = struct.unpack('<i', f.read(4))[0]
            entries = []
            for _ in range(max(count, 0)):
                name = _read_tmod_str(f)
                ulen, clen = struct.unpack('<ii', f.read(8))
                entries.append((name, ulen, clen))
            # 计算各条目数据偏移，只 seek 到 icon.png / Info
            cur = f.tell()
            target_pos = {}
            for name, ulen, clen in entries:
                low = name.lower()
                # 只匹配真正的 icon.png（文件名本身就是 icon.png，避免误匹配 xxxIcon.png 之类路径）
                if os.path.basename(low) == 'icon.png':
                    target_pos.setdefault('icon', (cur, clen, ulen))
                elif name == 'Info':
                    target_pos.setdefault('info', (cur, clen, ulen))
                cur += clen
            target = {}
            for key, (pos, clen, ulen) in target_pos.items():
                f.seek(pos)
                data = f.read(clen)
                target[key] = zlib.decompress(data, -15) if ulen != clen else data
    except Exception:
        return None

    display_name, author, info_version = '', '', ''
    info = target.get('info')
    if info:
        pos = 0
        try:
            while pos < len(info):
                k, pos = _read_bytes_str(info, pos)
                v, pos = _read_bytes_str(info, pos)
                if k == 'displayName':
                    display_name = v
                elif k == 'author':
                    author = v
                elif k == 'version':
                    info_version = v
        except Exception:
            pass

    icon_bytes = target.get('icon')
    if not icon_bytes and not display_name:
        return None
    return {'name': mod_name, 'display_name': display_name,
            'author': author, 'icon_bytes': icon_bytes,
            'version': info_version or mod_version}


def save_icon(name, icon_bytes, icon_dir):
    """把 .tmod 里的 icon.png 保存到 icon_dir/，返回访问 URL 或 None。

    URL 固定为 /static/<icon_dir 的目录名>/<安全名>.png。
    """
    if not icon_bytes:
        return None
    safe = re.sub(r'[^A-Za-z0-9_\-]', '_', name) or 'mod'
    try:
        os.makedirs(icon_dir, exist_ok=True)
        with open(os.path.join(icon_dir, safe + '.png'), 'wb') as f:
            f.write(icon_bytes)
        return f'/static/{os.path.basename(icon_dir)}/{safe}.png'
    except Exception:
        return None


def find_tmod(name, mods_path, packs_path):
    """找模组对应的 .tmod 文件路径：先在全局 Mods 目录，再扫整合包目录。"""
    for ext in ('.tmod', '.tmod.disabled'):
        p = os.path.join(mods_path, name + ext)
        if os.path.isfile(p):
            return p
    if os.path.isdir(packs_path):
        try:
            for pack in os.listdir(packs_path):
                p = os.path.join(packs_path, pack, 'Mods', name + '.tmod')
                if os.path.isfile(p):
                    return p
        except Exception:
            pass
    return None
