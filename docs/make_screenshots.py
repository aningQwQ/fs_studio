#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 README 用的界面截图（offscreen 渲染，无需显示器 / 真实设备 / root）

    python3 docs/make_screenshots.py

会在 docs/ 下输出 start.png / overview.png / heatmap.png / freemap.png。
数据为可复现的合成数据；引导扇区 hexdump 也由脚本合成，仅用于展示。
"""
import importlib.util
import os
import random
import struct
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from PySide6.QtGui import QPixmap                    # noqa: E402
from PySide6.QtWidgets import QApplication           # noqa: E402

from fs_packs import all_packs, find_pack_for_fstype, load_packs  # noqa: E402

W, H = 1240, 780


def hexdump(data):
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hx = ' '.join(f"{b:02X}" for b in chunk).ljust(16 * 3 - 1)
        asc = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f"{off:08X}  {hx}  |{asc}|")
    return '\n'.join(lines)


def synth_boot_sector():
    """合成一个看起来像真机的 NTFS 引导扇区，用于概览页 hexdump 展示"""
    bs = bytearray(512)
    bs[0:3] = b'\xeb\x52\x90'
    bs[3:11] = b'NTFS    '
    struct.pack_into('<H', bs, 0x0B, 512)
    bs[0x0D] = 8
    struct.pack_into('<Q', bs, 0x28, 536870911)
    struct.pack_into('<Q', bs, 0x30, 4)
    struct.pack_into('<Q', bs, 0x38, 2097151)
    bs[0x40] = 0xF6
    bs[0x44] = 1
    struct.pack_into('<Q', bs, 0x48, 0x4E2A9C1B77D4F012)
    bs[510:512] = b'\x55\xaa'
    return bytes(bs)


def ntfs_payload():
    rng = random.Random(7)
    row_size, nrows = 524288, 128          # 128 区 × 512K 簇 = 256GB / 4KB
    rows = []
    for i in range(nrows):
        frac = max(0.05, min(0.97, 0.86 - i / 170 + rng.uniform(-0.06, 0.06)))
        free = int(row_size * frac)
        recs = [(0, free - 60000), (free - 30000, 30000)] \
            if free > 100000 else [(0, free)]
        rows.append([i, recs])

    total = nrows * row_size
    free_total = sum(c for _, recs in rows for _, c in recs)
    fields = [
        ('文件系统', 'NTFS'),
        ('卷标', 'DATA'),
        ('版本', '3.1'),
        ('卷序列号', '0x4E2A9C1B77D4F012'),
        ('每扇区字节', '512'),
        ('每簇扇区', '8'),
        ('簇大小', '4.00 K'),
        ('扇区总数', '536,870,911'),
        ('设备容量', '256.00 G'),
        ('簇总数', f'{total:,}'),
        ('空闲簇', f'{free_total:,}'),
        ('已用簇', f'{total - free_total:,}'),
        ('空闲比例', f'{free_total / total * 100:.2f}%'),
        ('MFT 起始簇', '4'),
        ('MFT 镜像起始簇', '2,097,151'),
        ('MFT 记录大小', '1024 B'),
        ('MFT 记录数', '196,608（在用 12,431）'),
        ('索引记录大小', '4096 B'),
    ]
    info = ('引导扇区 (Boot Sector)\n' + hexdump(synth_boot_sector())
            + '\n\n$MFT 数据运行段数: 3    $MFT 已分配: 192.00 M\n'
            '$Bitmap 数据: 8.00 M 字节    空闲区间数: 1,246\n')
    return {
        'dev': '/dev/sdb1', 'fs': 'ntfs', 'fields': fields, 'info': info,
        'cluster_size': 4096, 'cluster_count': total,
        'free_clusters': free_total, 'rows': rows, 'row_size': row_size,
    }


def ext4_payload():
    """块组热力图的示例数据：4096 个块组 × 32768 块 × 4KB = 512GB"""
    rng = random.Random(3)
    ngroups, bpg, bsize = 4096, 32768, 4096
    groups = []
    for i in range(ngroups):
        frac = max(0.02, min(0.99, 0.9 - i / 4800 + rng.uniform(-0.05, 0.05)))
        free = int(bpg * frac)
        groups.append({
            'group': i, 'start': i * bpg, 'end': (i + 1) * bpg - 1,
            'length': bpg, 'free_blocks': free,
            'free_inodes': int(8192 * frac),
            'free_ranges': [[0, free]] if free < bpg else [],
        })
    total = ngroups * bpg
    free_total = sum(g['free_blocks'] for g in groups)
    sb = {
        'Filesystem volume name': 'root',
        'Filesystem UUID': '7c9a1e40-2b6d-4f18-9a3c-5e0b8d2f1a77',
        'Filesystem magic number': '0xEF53',
        'Filesystem features': ('has_journal ext_attr resize_inode dir_index '
                                'filetype extent 64bit flex_bg sparse_super '
                                'large_file huge_file dir_nlink metadata_csum'),
        'Filesystem state': 'clean',
        'Inode count': f'{ngroups * 8192:,}',
        'Block count': f'{total:,}',
        'Reserved block count': '6,553,600',
        'Free blocks': f'{free_total:,}',
        'Free inodes': f'{int(ngroups * 8192 * 0.82):,}',
        'Block size': f'{bsize}',
        'Inode size': '256',
        'Blocks per group': f'{bpg}',
        'Inodes per group': '8192',
        'Journal inode': '8',
        'Filesystem created': 'Tue Mar 12 09:24:51 2024',
    }
    return {'dev': '/dev/nvme0n1p2', 'fs': 'ext4', 'sb': sb,
            'info': 'dumpe2fs 1.47.0 (5-Feb-2023)\n' + '\n'.join(
                f'{k}: {v}' for k, v in list(sb.items())[:8]),
            'frag': '', 'groups': groups}


def fake_devices():
    spec = [
        ('/dev/nvme0n1p2', 'ext4', '/', '512G'),
        ('/dev/sda1', 'xfs', '/data', '2T'),
        ('/dev/sdb1', 'ntfs', '/mnt/win', '256G'),
        ('/dev/sdc1', 'vfat', '/mnt/usb', '32G'),
        ('/dev/sdd1', 'exfat', '-', '64G'),
    ]
    out = []
    for dev, fstype, mount, size in spec:
        pack = find_pack_for_fstype(fstype)
        if pack is not None:
            out.append({'dev': dev, 'fstype': fstype, 'mount': mount,
                        'size': size, 'pack': pack})
    return out


def shot(widget, name):
    pm = QPixmap(widget.size())
    pm.fill()
    widget.render(pm)
    path = os.path.join(HERE, name)
    pm.save(path)
    print('saved', path)


def main():
    app = QApplication(sys.argv)
    load_packs()

    # 主文件名不是合法模块名，用 importlib 按路径加载
    spec = importlib.util.spec_from_file_location(
        'fs_studio', os.path.join(ROOT, 'fs_studio_v2.0.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    MainWindow = mod.MainWindow

    # 起始页
    win = MainWindow()
    win.resize(W, H)
    win.current_pack = None
    win.start_page.set_packs(all_packs())
    win.start_page.set_devices(fake_devices())
    win.tabs.setCurrentIndex(0)
    win.show()
    app.processEvents()
    shot(win, 'start.png')
    win.close()

    def render_tabs(fstype, payload, targets):
        win = MainWindow()
        win.resize(W, H)
        win._build_tabs(find_pack_for_fstype(fstype))
        for tab in win.tab_instances:
            tab.update_data({'full': payload})
        for tab_id, name, focus_row in targets:
            idx = next(i for i, t in enumerate(win.tab_instances)
                       if t.TAB_ID == tab_id)
            win.tabs.setCurrentIndex(idx + 1)   # 第 0 页是起始页
            if focus_row is not None:
                win.tab_instances[idx].chart.focus_row(focus_row)
            win.show()
            app.processEvents()
            shot(win, name)
        win.close()

    # 概览 + 空闲可视化：以 NTFS 为例
    render_tabs('ntfs', ntfs_payload(),
                [('overview', 'overview.png', None),
                 ('plot', 'freemap.png', 9)])       # 9：演示定位高亮

    # 热力图：以块组数量多的 ext4 为例（FAT/NTFS 的"区"较少，格子较稀疏）
    render_tabs('ext4', ext4_payload(),
                [('heatmap', 'heatmap.png', None)])


if __name__ == '__main__':
    main()
