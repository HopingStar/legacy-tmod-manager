# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 HopingStar
"""图标工具模块（独立于 app.py，供其调用）

职责：
  1. encrypt_icon() / convert_and_encrypt() —— 把图片加密成 .dat（异或混淆，看图软件无法直接打开）
  2. encrypt_auto()                          —— 加密到源文件同目录（供拖放加密）
  3. set_window_icon()                       —— 解密 .dat 并设为窗口/任务栏图标（WM_SETICON）

密钥 ICON_KEY 由本模块统一持有，生成 .dat 与读取必须使用同一密钥。
程序内用法：
  from modules import icon_util
  icon_util.set_window_icon(hwnd, 'static/tmod_icon.dat')

拖放加密（无需 GUI）：把图片文件直接拖到 icon_util.py 图标上（等价于"用本程序打开"），
或运行：python icon_util.py <图片1> [图片2 ...]，会自动在各自原目录生成同名 .dat。
"""
import os
import io
import struct
import ctypes
from ctypes import wintypes

# 图标加密密钥（与生成 tmod_icon.dat 时保持一致，勿改）
ICON_KEY = 0x5A


def _is_ico(data):
    """校验是否为有效 ICO：前 4 字节 00 00 01 00"""
    return len(data) >= 6 and data[:2] == b'\x00\x00' and data[2:4] == b'\x01\x00'


def encrypt_icon(src_ico, dst_dat, key=ICON_KEY):
    """把 .ico 异或加密写入 .dat。源文件不存在或非 ICO 返回 False。"""
    try:
        if not os.path.isfile(src_ico):
            return False
        with open(src_ico, 'rb') as f:
            data = f.read()
        if not _is_ico(data):
            return False
        enc = bytes(b ^ key for b in data)
        with open(dst_dat, 'wb') as f:
            f.write(enc)
        return True
    except Exception:
        return False


def _decrypt(dat_path, key=ICON_KEY):
    """读取并解密 .dat，校验是 ICO。返回原始 ICO bytes，失败返回 None。"""
    try:
        if not os.path.isfile(dat_path):
            return None
        with open(dat_path, 'rb') as f:
            enc = f.read()
        data = bytes(b ^ key for b in enc)
        if not _is_ico(data):
            return None
        return data
    except Exception:
        return None


def _pick_best_image(data):
    """从 ICO 数据中挑选 32x32 或尺寸最大的图像段，返回 (img_off, size) 或 None"""
    try:
        count = struct.unpack('<H', data[4:6])[0]
        best = None
        for i in range(count):
            off = 6 + i * 16
            if off + 16 > len(data):
                break
            w = data[off] or 256
            h = data[off + 1] or 256
            size_in_res = struct.unpack('<I', data[off + 8:off + 12])[0]
            img_off = struct.unpack('<I', data[off + 12:off + 16])[0]
            if size_in_res and img_off + size_in_res <= len(data):
                if best is None or (w == 32 and h == 32) or (best[0] != 32 and w <= 32 and w >= best[0]):
                    best = (w, h, img_off, size_in_res)
        if not best:
            return None
        return best[2], best[3]
    except Exception:
        return None


def set_window_icon(hwnd, dat_path, key=ICON_KEY):
    """解密 .dat 并设置为窗口/任务栏图标（WM_SETICON）。失败静默，不影响启动。"""
    try:
        data = _decrypt(dat_path, key)
        if data is None:
            return
        pick = _pick_best_image(data)
        if pick is None:
            return
        img_off, size_in_res = pick
        icon_data = data[img_off:img_off + size_in_res]

        user32 = ctypes.windll.user32
        user32.CreateIconFromResourceEx.restype = wintypes.HANDLE
        user32.CreateIconFromResourceEx.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
            wintypes.DWORD, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        buf = ctypes.create_string_buffer(icon_data, len(icon_data))
        hicon = user32.CreateIconFromResourceEx(
            ctypes.cast(buf, ctypes.c_void_p), len(icon_data), True, 0x30000, 0, 0, 0)
        if not hicon:
            return
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.SendMessageW(hwnd, 0x0080, 0, hicon)   # WM_SETICON 小图标
        user32.SendMessageW(hwnd, 0x0080, 1, hicon)   # WM_SETICON 大图标（任务栏）
    except Exception:
        pass


def convert_and_encrypt(src, dst, key=ICON_KEY):
    """用 Pillow 把 PNG / JPG 等图片转换为 ICO 后加密写入 dst。成功返回 True。"""
    try:
        from PIL import Image
        buf = io.BytesIO()
        img = Image.open(src).convert('RGBA')
        img.save(buf, format='ICO', sizes=[(256, 256), (48, 48), (32, 32), (16, 16)])
        data = buf.getvalue()
        if not _is_ico(data):
            return False
        enc = bytes(b ^ key for b in data)
        with open(dst, 'wb') as f:
            f.write(enc)
        return True
    except Exception:
        return False


def encrypt_auto(src, key=ICON_KEY):
    """把图片（.ico 或 .png/.jpg 等）加密生成 .dat，保存到源文件同目录。
    返回输出 .dat 路径；失败返回 None。"""
    try:
        if not os.path.isfile(src):
            return None
        base = os.path.splitext(src)[0]
        dst = base + '.dat'
        if os.path.abspath(src).lower() == os.path.abspath(dst).lower():
            return None  # 避免同名冲突
        ext = os.path.splitext(src)[1].lower()
        if ext == '.ico':
            ok = encrypt_icon(src, dst, key)
        else:
            ok = convert_and_encrypt(src, dst, key)
        return dst if ok else None
    except Exception:
        return None


# 支持的图片扩展名（拖放加密时识别）
_IMAGE_EXTS = ('.ico', '.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp')


def _looks_like_image(path):
    return os.path.splitext(str(path))[1].lower() in _IMAGE_EXTS


def main():
    """命令行入口（把图片拖到本文件图标上即可触发，等价于"用本程序打开"）：
      python icon_util.py <图片1> [图片2 ...]   → 逐个加密到各自同目录
      python icon_util.py <源.ico> <输出.dat>   → 指定输出路径（兼容旧用法）
    """
    import sys
    args = [a for a in sys.argv[1:] if a]
    if not args:
        print('用法: 把图片文件拖到 icon_util.py 文件上（相当于"用本程序打开"），')
        print('      或运行:  python icon_util.py <图片1> [图片2 ...]')
        print('      自动在每个图片的同目录生成同名 .dat。')
        try:
            input('按回车退出...')
        except EOFError:
            pass
        return
    # 兼容旧用法：两个参数且第二个不是图片 → 当作「源 → 输出」
    if len(args) == 2 and not _looks_like_image(args[1]):
        ok = encrypt_icon(args[0], args[1])
        print('加密成功' if ok else '加密失败（源文件不存在或不是有效的 ICO）')
        return
    # 拖放 / 多文件：逐个加密到各自原目录
    ok_any = False
    for f in args:
        out = encrypt_auto(f)
        if out:
            print('OK   %s  →  %s' % (os.path.basename(f), out))
            ok_any = True
        else:
            print('FAIL  %s（非图片或加密失败）' % os.path.basename(f))
    print('已保存到原图片同目录。' if ok_any else '未生成任何加密文件。')
    try:
        input('按回车退出...')
    except EOFError:
        pass


if __name__ == '__main__':
    main()
