#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fs_packs.fat — FAT 系列包（FAT12 / FAT16 / FAT32 / exFAT）

FAT 与 exFAT 同属 "FAT 表 + 簇堆" 家族，放在同一模块里共享二进制读取与
空闲簇扫描代码：

    collector : collect_fat()    （root 侧，直接读引导扇区 + FAT 表）
                collect_exfat()  （root 侧，直接读引导扇区 + 分配位图）
    tabs      : 概览 / 簇分布 / 簇热力图 / 空闲可视化（FAT 与 exFAT 各一套）

不依赖 exfatprogs / dosfstools：引导扇区、FAT 表、分配位图都由本模块直接解析，
只要求采集时有权限打开设备（采集器已由 pkexec/sudo 提权运行）。

带走这个包 = 复制本文件（同时得到 FAT 与 exFAT 支持）。
"""
import array
import re
import struct
import sys

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QGroupBox, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QPlainTextEdit, QScrollArea,
                               QScrollBar, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .core import (BlockRangeChart, FSPack, TabPlugin, human_size,
                   register_collector, register_pack, register_tab)

# 空闲可视化把簇空间切成多少行（与 ext4 的块组数量级接近，便于浏览）
TARGET_ROWS = 128
READ_CAP = 1 << 20          # 目录链 / 位图单次读取上限（字节）


# =============================================================================
# Part 0 · 二进制读取通用工具
# =============================================================================
def _u16(b, o):
    return struct.unpack_from('<H', b, o)[0]


def _u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


def _u64(b, o):
    return struct.unpack_from('<Q', b, o)[0]


def _ceil_div(a, b):
    return (a + b - 1) // b


def _read_at(fh, offset, size):
    fh.seek(offset)
    return fh.read(size)


def _hexdump(data, base=0):
    """把二进制数据格式化为 16 字节/行的 hexdump 文本"""
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hx = ' '.join(f"{b:02X}" for b in chunk)
        hx = hx.ljust(16 * 3 - 1)
        asc = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f"{base + off:08X}  {hx}  |{asc}|")
    return '\n'.join(lines)


def _chunk_rows(cluster_count, runs, target=TARGET_ROWS):
    """
    把连续簇空间切成若干"区"，返回 (rows, row_size)。
    rows: [[区号, [(区内起始偏移, 簇数), ...]], ...]
    runs: [(起始簇号, 簇数), ...]（簇号从 2 开始）
    """
    if cluster_count <= 0:
        return [], 1
    row_size = max(1, _ceil_div(cluster_count, target))
    nrows = _ceil_div(cluster_count, row_size)
    rows = [[k, []] for k in range(nrows)]

    for start, count in runs:
        s, e = start, start + count          # [s, e)
        while s < e:
            i = (s - 2) // row_size
            if not (0 <= i < nrows):
                break
            c0 = 2 + i * row_size
            c1 = 2 + min(cluster_count, (i + 1) * row_size)
            a, b = max(s, c0), min(e, c1)
            if b > a:
                rows[i][1].append((a - c0, b - a))
            s = b
    return rows, row_size


def _runs_from_flags(flags, cluster_count):
    """由 "是否空闲" 序列（簇 2 起）生成空闲区间与空闲簇数（仅用于小表）"""
    runs = []
    free = 0
    run_start = None
    for i, is_free in enumerate(flags):
        c = 2 + i
        if is_free:
            if run_start is None:
                run_start = c
            free += 1
        elif run_start is not None:
            runs.append((run_start, c - run_start))
            run_start = None
    if run_start is not None:
        runs.append((run_start, cluster_count + 2 - run_start))
    return runs, free


def _free_byte_runs():
    """每个字节值 → 该字节内空闲位（0 位）的 (起始位, 长度) 列表"""
    table = []
    for v in range(256):
        lst = []
        k = 0
        while k < 8:
            if not ((v >> k) & 1):
                s = k
                while k < 8 and not ((v >> k) & 1):
                    k += 1
                lst.append((s, k - s))
            else:
                k += 1
        table.append(tuple(lst))
    return table


_FREE_BYTE_RUNS = _free_byte_runs()

# 成片的 0x00/0xFF 字节整段跳过，只有"混合"字节才逐位处理
_BITMAP_SPLIT = re.compile(rb'\x00+|\xff+|[^\x00\xff]+')
# FAT32 表项：连续 4 个 0x00 字节 = 一个空闲簇
_ZERO_RUN = re.compile(rb'\x00{4,}')


def _scan_bitmap_free(bm, cluster_count, base=2):
    """
    扫描分配位图（exFAT），返回 (空闲区间, 空闲簇数)。
    第 i 位对应簇 base+i；与 FAT/exFAT 的从 2 起编号一致（base=2）。
    """
    runs = []
    free = 0
    run_start = None
    run_end = 0
    total = cluster_count

    for m in _BITMAP_SPLIT.finditer(bm):
        seg = m.group()
        b0 = m.start() * 8
        first = seg[0]
        if first == 0x00:                       # 整段空闲
            if b0 >= total:
                break
            a = base + b0
            b = base + min(b0 + len(seg) * 8, total)
            if run_start is not None and a == run_end:
                run_end = b
            else:
                if run_start is not None:
                    runs.append((run_start, run_end - run_start))
                run_start, run_end = a, b
            free += b - a
        elif first == 0xFF:                     # 整段占用
            if b0 >= total:
                break
            if run_start is not None:
                runs.append((run_start, run_end - run_start))
                run_start = None
        else:                                   # 混合字节：查表逐位
            for j, byte in enumerate(seg):
                bb = b0 + j * 8
                if bb >= total:
                    break
                for sb, ln in _FREE_BYTE_RUNS[byte]:
                    a = base + bb + sb
                    if a >= base + total:
                        break
                    b = min(a + ln, base + total)
                    if run_start is not None and a == run_end:
                        run_end = b
                    else:
                        if run_start is not None:
                            runs.append((run_start, run_end - run_start))
                        run_start, run_end = a, b
                    free += b - a

    if run_start is not None:
        runs.append((run_start, run_end - run_start))
    return runs, free


# 单个数据块里若空闲"段"过多，说明表被极度碎片化，改用逐项扫描更划算
MAX_REGEX_RUNS = 8192


def _fat32_chunk_regex(buf, base_index, cluster_count):
    """
    用 C 级正则扫描一块 FAT32 表：连续 4 个零字节的表项 = 空闲簇。
    若空闲段数超过阈值（极度碎片化）则返回 None，由调用方回退逐项扫描。
    """
    out = []
    for m in _ZERO_RUN.finditer(buf):
        zs, ze = m.start(), m.end()
        a = -(-zs // 4) * 4                 # 向上取整到表项边界
        b = (ze // 4) * 4                   # 向下取整到表项边界
        if b <= a:
            continue
        first = base_index + a // 4
        last = min(base_index + b // 4, cluster_count)   # 左闭右开
        if first >= last:
            continue
        out.append((2 + first, 2 + last))
        if len(out) > MAX_REGEX_RUNS:
            return None
    return out


def _scan_fat32_free(fh, fat_start, cluster_count, chunk_bytes=1 << 22):
    """
    扫描 FAT32 表，返回 (空闲区间, 空闲簇数)。
    主流情况用 C 级正则整段跳过成片空闲区，避免 O(簇数) 的 Python 循环；
    极度碎片化时按块回退为逐项扫描，避免正则匹配对象过多反而更慢。
    """
    start_byte = fat_start + 8              # 簇 2 的表项起点
    total_bytes = cluster_count * 4
    runs = []
    free = 0
    run_start = None
    run_end = 0
    done = 0

    while done < total_bytes:
        n = min(chunk_bytes, total_bytes - done)
        n -= n % 4
        if n <= 0:
            n = min(4, total_bytes - done)
        buf = _read_at(fh, start_byte + done, n)
        if len(buf) < n:
            raise RuntimeError("FAT 表读取不完整")
        base_index = done // 4              # 该块第一项对应的 (簇号 - 2)

        local = _fat32_chunk_regex(buf, base_index, cluster_count)
        if local is None:
            # 极度碎片化：逐项扫描并直接并入全局状态（不建中间列表）
            arr = array.array('I')
            arr.frombytes(buf)
            if sys.byteorder != 'little':
                arr.byteswap()
            i, cnt = 0, len(arr)
            while i < cnt:
                if (arr[i] & 0x0FFFFFFF) == 0:
                    j = i + 1
                    while j < cnt and (arr[j] & 0x0FFFFFFF) == 0:
                        j += 1
                    rs = 2 + base_index + i
                    re_ = 2 + min(base_index + j, cluster_count)
                    if rs < re_:
                        if run_start is not None and rs == run_end:
                            run_end = re_
                        else:
                            if run_start is not None:
                                runs.append((run_start, run_end - run_start))
                            run_start, run_end = rs, re_
                        free += re_ - rs
                    i = j
                else:
                    i += 1
            done += n
            continue

        for rs, re_ in local:
            if run_start is not None and rs == run_end:
                run_end = re_
            else:
                if run_start is not None:
                    runs.append((run_start, run_end - run_start))
                run_start, run_end = rs, re_
            free += re_ - rs
        done += n

    if run_start is not None:
        runs.append((run_start, run_end - run_start))
    return runs, free


# =============================================================================
# Part 1 · FAT12/16/32 采集（root 侧）
# =============================================================================
def _parse_bpb(bs):
    if len(bs) < 512 or bs[510] != 0x55 or bs[511] != 0xAA:
        raise RuntimeError("不是有效的 FAT 引导扇区（缺少 0xAA55 签名）")

    bps = _u16(bs, 0x0B)
    spc = bs[0x0D]
    reserved = _u16(bs, 0x0E)
    nfats = bs[0x10]
    root_entries = _u16(bs, 0x11)
    total16 = _u16(bs, 0x13)
    fat16 = _u16(bs, 0x16)
    total32 = _u32(bs, 0x20)
    fat32 = _u32(bs, 0x24)

    if bps not in (512, 1024, 2048, 4096) or spc == 0 or nfats == 0:
        raise RuntimeError("不是有效的 FAT 引导扇区（BPB 字段非法）")

    total_sectors = total16 or total32
    fat_size = fat16 or fat32
    if not total_sectors or not fat_size:
        raise RuntimeError("不是有效的 FAT 引导扇区（扇区/FAT 大小为空）")

    root_dir_sectors = _ceil_div(root_entries * 32, bps) if root_entries else 0
    data_sectors = total_sectors - (reserved + nfats * fat_size
                                    + root_dir_sectors)
    cluster_count = data_sectors // spc

    if cluster_count < 4085:
        ftype = 12
    elif cluster_count < 65525:
        ftype = 16
    else:
        ftype = 32
    if cluster_count <= 0:
        raise RuntimeError("不是有效的 FAT 文件系统（数据簇数为 0）")

    data_start = reserved + nfats * fat_size + root_dir_sectors
    if ftype == 32:
        vol_id = _u32(bs, 0x43)
        bpb_label = bs[0x47:0x52]
        type_str = bs[0x52:0x5A]
        root_cluster = _u32(bs, 0x2C)
    else:
        vol_id = _u32(bs, 0x27)
        bpb_label = bs[0x2B:0x36]
        type_str = bs[0x36:0x3E]
        root_cluster = 0

    return {
        'bytes_per_sector': bps,
        'sectors_per_cluster': spc,
        'reserved': reserved,
        'nfats': nfats,
        'root_entries': root_entries,
        'total_sectors': total_sectors,
        'fat_size': fat_size,
        'root_dir_sectors': root_dir_sectors,
        'cluster_count': cluster_count,
        'fat_type': ftype,
        'data_start': data_start,
        'fat_start': reserved * bps,
        'root_cluster': root_cluster,
        'media': bs[0x15],
        'volume_id': vol_id,
        'bpb_label': bpb_label.decode('latin-1').rstrip(' '),
        'oem': bs[3:11].decode('latin-1').rstrip(' '),
        'type_str': type_str.decode('latin-1').rstrip(' '),
    }


def _fat32_next(fh, fat_start, cluster):
    return _u32(_read_at(fh, fat_start + cluster * 4, 4), 0) & 0x0FFFFFFF


def _read_chain(fh, fat_start, heap_offset, cluster_size, start, max_bytes):
    """沿 FAT 链读取簇数据（用于 FAT32 根目录）"""
    out = bytearray()
    seen = set()
    c = start
    while 2 <= c < 0x0FFFFFF8 and len(out) < max_bytes:
        if c in seen:
            break
        seen.add(c)
        out += _read_at(fh, heap_offset + (c - 2) * cluster_size,
                        cluster_size)
        c = _fat32_next(fh, fat_start, c)
    return bytes(out)


def _scan_fat_free(fh, bpb):
    """读取 FAT 表，统计空闲簇并生成空闲区间"""
    fat_start = bpb['fat_start']
    ftype = bpb['fat_type']
    cluster_count = bpb['cluster_count']

    if ftype == 32:
        return _scan_fat32_free(fh, fat_start, cluster_count)

    if ftype == 16:
        buf = _read_at(fh, fat_start + 4, cluster_count * 2)
        if len(buf) < cluster_count * 2:
            raise RuntimeError("FAT 表读取不完整")
        arr = array.array('H')
        arr.frombytes(buf)
        if sys.byteorder != 'little':
            arr.byteswap()
        return _runs_from_flags((v == 0 for v in arr), cluster_count)

    # FAT12：整表很小（< 4085 项），逐项解包 1.5 字节
    raw = _read_at(fh, fat_start, _ceil_div((cluster_count + 2) * 3, 2) + 2)
    flags = []
    for i in range(2, cluster_count + 2):
        off = i + i // 2
        if i & 1:
            v = (raw[off] >> 4) | (raw[off + 1] << 4)
        else:
            v = raw[off] | ((raw[off + 1] & 0x0F) << 8)
        flags.append(v == 0)
    return _runs_from_flags(flags, cluster_count)


def _label_from_dir(data):
    for i in range(0, len(data) - 31, 32):
        e = data[i:i + 32]
        if e[0] == 0x00:
            break
        if e[0] == 0xE5:
            continue
        attr = e[11]
        if attr == 0x0F:            # LFN 长文件名项
            continue
        if attr & 0x08:             # 卷标项
            name = bytearray(e[0:11])
            if name[0] == 0x05:
                name[0] = 0xE5
            return name.decode('latin-1').rstrip(' ')
    return ''


@register_collector('fat', 'full')
def collect_fat(dev):
    try:
        fh = open(dev, 'rb', buffering=0)
    except OSError as e:
        raise RuntimeError(f"无法打开设备 {dev}: {e}")

    with fh:
        bs = _read_at(fh, 0, 512)
        bpb = _parse_bpb(bs)

        bps = bpb['bytes_per_sector']
        spc = bpb['sectors_per_cluster']
        cluster_size = bps * spc
        cluster_count = bpb['cluster_count']

        if bpb['fat_type'] == 32:
            root_off = (bpb['data_start'] * bps
                        + (bpb['root_cluster'] - 2) * cluster_size)
            root = _read_chain(fh, bpb['fat_start'], bpb['data_start'] * bps,
                               cluster_size, bpb['root_cluster'], READ_CAP)
        else:
            root_off = ((bpb['reserved'] + bpb['nfats'] * bpb['fat_size'])
                        * bps)
            root = _read_at(fh, root_off, bpb['root_entries'] * 32)

        label = _label_from_dir(root) or bpb['bpb_label'] or '-'

        runs, free = _scan_fat_free(fh, bpb)

    used = cluster_count - free
    pct = free / cluster_count * 100 if cluster_count else 0
    rows, row_size = _chunk_rows(cluster_count, runs)

    fields = [
        ('文件系统', f"FAT{bpb['fat_type']}"),
        ('卷标', label),
        ('卷序列号', f"0x{bpb['volume_id']:08X}"),
        ('OEM 标识', bpb['oem'] or '-'),
        ('每扇区字节', f"{bps}"),
        ('每簇扇区', f"{spc}"),
        ('簇大小', human_size(cluster_size)),
        ('扇区总数', f"{bpb['total_sectors']:,}"),
        ('设备容量', human_size(bpb['total_sectors'] * bps)),
        ('保留扇区', f"{bpb['reserved']:,}"),
        ('FAT 数量', f"{bpb['nfats']}"),
        ('FAT 大小', f"{bpb['fat_size']:,} 扇区 "
                   f"({human_size(bpb['fat_size'] * bps)})"),
        ('数据区起始', f"扇区 {bpb['data_start']:,}"),
        ('簇总数', f"{cluster_count:,}"),
        ('空闲簇', f"{free:,}"),
        ('已用簇', f"{used:,}"),
        ('空闲比例', f"{pct:.2f}%"),
    ]

    info = (f"引导扇区 (Boot Sector)\n"
            f"{_hexdump(bs)}\n\n"
            f"根目录起始: 扇区 {root_off // bps:,}"
            f"    根目录项数: {bpb['root_entries'] or '可变 (FAT32)'}\n"
            f"BPB 文件系统类型字符串: {bpb['type_str'] or '-'}\n")

    return {
        'dev': dev, 'fs': 'fat',
        'fields': fields,
        'info': info,
        'cluster_size': cluster_size,
        'cluster_count': cluster_count,
        'free_clusters': free,
        'rows': rows,
        'row_size': row_size,
    }


# =============================================================================
# Part 2 · exFAT 采集（root 侧）
# =============================================================================
def _exfat_next(fh, fat_start, cluster):
    return _u32(_read_at(fh, fat_start + cluster * 4, 4), 0) & 0x0FFFFFFF


def _exfat_chain(fh, fat_start, heap_start, cluster_size, start, max_bytes):
    out = bytearray()
    seen = set()
    c = start
    while 2 <= c < 0x0FFFFFF8 and len(out) < max_bytes:
        if c in seen:
            break
        seen.add(c)
        out += _read_at(fh, heap_start + (c - 2) * cluster_size, cluster_size)
        c = _exfat_next(fh, fat_start, c)
    return bytes(out)


def _exfat_root_entries(root):
    """扫描根目录，取出分配位图 / 大写表 / 卷标条目"""
    bitmap = upcase = None
    label = ''
    i = 0
    while i + 32 <= len(root):
        e = root[i:i + 32]
        t = e[0]
        if t == 0x00:
            break
        if t == 0x81:
            bitmap = {'cluster': _u32(e, 20), 'length': _u64(e, 24)}
        elif t == 0x82:
            upcase = {'cluster': _u32(e, 20), 'length': _u64(e, 24)}
        elif t == 0x83:
            n = min(e[1], 11)
            label = e[2:2 + n * 2].decode('utf-16-le', 'replace')
        # 关键条目 (>=0x80) 的 byte[1] 低 5 位是二级条目数
        step = 1 + (e[1] & 0x1F) if (t >= 0x80 and t != 0x83) else 1
        i += step * 32
    return bitmap, upcase, label


@register_collector('exfat', 'full')
def collect_exfat(dev):
    try:
        fh = open(dev, 'rb', buffering=0)
    except OSError as e:
        raise RuntimeError(f"无法打开设备 {dev}: {e}")

    with fh:
        bs = _read_at(fh, 0, 512)
        if len(bs) < 512 or bs[3:11] != b'EXFAT   ':
            raise RuntimeError("不是有效的 exFAT 引导扇区")

        volume_length = _u64(bs, 0x48)
        fat_offset = _u32(bs, 0x50)
        fat_length = _u32(bs, 0x54)
        heap_offset = _u32(bs, 0x58)
        cluster_count = _u32(bs, 0x5C)
        root_cluster = _u32(bs, 0x60)
        serial = _u32(bs, 0x64)
        revision = _u16(bs, 0x68)
        percent = bs[0x70]
        bps = 1 << bs[0x6C]
        spc = 1 << bs[0x6D]
        nfats = bs[0x6E]

        if bps not in (512, 1024, 2048, 4096) or spc == 0:
            raise RuntimeError("exFAT 引导扇区字段非法")
        cluster_size = bps * spc
        fat_start = fat_offset * bps
        heap_start = heap_offset * bps

        root = _exfat_chain(fh, fat_start, heap_start, cluster_size,
                            root_cluster, READ_CAP)
        bitmap, upcase, label = _exfat_root_entries(root)
        if bitmap is None or bitmap['cluster'] < 2:
            raise RuntimeError("exFAT 根目录中未找到分配位图")

        bm = _exfat_chain(fh, fat_start, heap_start, cluster_size,
                          bitmap['cluster'], bitmap['length'] or READ_CAP)
        if len(bm) < _ceil_div(cluster_count, 8):
            raise RuntimeError("exFAT 分配位图读取不完整")

    runs, free = _scan_bitmap_free(bm, cluster_count, base=2)

    used = cluster_count - free
    pct = free / cluster_count * 100 if cluster_count else 0
    rows, row_size = _chunk_rows(cluster_count, runs)

    fields = [
        ('文件系统', 'exFAT'),
        ('卷标', label or '-'),
        ('卷序列号', f"0x{serial:08X}"),
        ('版本', f"{revision >> 8}.{revision & 0xFF}"),
        ('每扇区字节', f"{bps}"),
        ('每簇扇区', f"{spc}"),
        ('簇大小', human_size(cluster_size)),
        ('卷长度', f"{volume_length:,} 扇区 "
                 f"({human_size(volume_length * bps)})"),
        ('FAT 偏移', f"扇区 {fat_offset:,}"),
        ('FAT 长度', f"{fat_length:,} 扇区 "
                   f"({human_size(fat_length * bps)})"),
        ('FAT 数量', f"{nfats}"),
        ('簇堆偏移', f"扇区 {heap_offset:,}"),
        ('簇总数', f"{cluster_count:,}"),
        ('空闲簇', f"{free:,}"),
        ('已用簇', f"{used:,}"),
        ('空闲比例', f"{pct:.2f}%"),
        ('根目录簇', f"{root_cluster}"),
        ('分配位图', f"簇 {bitmap['cluster']} · "
                   f"{human_size(bitmap['length'])}"),
        ('大写表', (f"簇 {upcase['cluster']} · {human_size(upcase['length'])}"
                  if upcase else '-')),
        ('卷内占用记录', f"{percent}%" if percent not in (0, 0xFF) else '-'),
    ]

    info = (f"引导扇区 (Boot Sector)\n"
            f"{_hexdump(bs)}\n\n"
            f"FAT 起始字节: {fat_start:,}    "
            f"簇堆起始字节: {heap_start:,}\n"
            f"分配位图 (Allocation Bitmap): 簇 {bitmap['cluster']} · "
            f"{bitmap['length']:,} 字节\n")

    return {
        'dev': dev, 'fs': 'exfat',
        'fields': fields,
        'info': info,
        'cluster_size': cluster_size,
        'cluster_count': cluster_count,
        'free_clusters': free,
        'rows': rows,
        'row_size': row_size,
    }


# =============================================================================
# Part 2.5 · 簇热力图（主视图：回答"全局分布"）
# =============================================================================
class _ClusterHeatmap(QWidget):
    """
    借鉴 ext4 的块组热力图：把等大小的"区"（连续簇段）排成矩阵，
    一格 = 一个区，颜色浓淡 = 使用率（绿=空 → 黄=半 → 红=满）。
    左键点击某格 → cell_clicked(区号)，用于跳转到空闲可视化对应行。
    """
    cell_clicked = Signal(int)

    MARGIN = 10
    GAP = 3

    C_BG    = QColor('#ffffff')
    C_EMPTY = QColor('#22c55e')   # 全空
    C_HALF  = QColor('#eab308')   # 半满
    C_FULL  = QColor('#ef4444')   # 全满
    C_DIM   = QColor('#666666')
    C_HOVER = QColor('#111111')

    def __init__(self, empty_text='请在起始页选择一个设备', parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setStyleSheet("background: white;")

        self.empty_text = empty_text
        self.dev = None
        self.rows = []
        self.cell = 11
        self.cluster_size = 4096
        self._hover = None
        self._hover_pos = None

    # ---------------- 数据 / 尺寸 ----------------
    def set_data(self, dev, rows, cluster_size=4096):
        self.dev = dev
        self.rows = list(rows or [])
        self.cluster_size = int(cluster_size or 4096)
        self._hover = None
        self._hover_pos = None
        self._relayout()
        self.update()

    def clear(self):
        self.dev = None
        self.rows = []
        self._hover = None
        self._hover_pos = None
        self._relayout()
        self.update()

    def set_cell_size(self, px):
        px = max(8, min(14, int(px)))
        if px != self.cell:
            self.cell = px
            self._relayout()
            self.update()

    def _pitch(self):
        return self.cell + self.GAP

    def _cols(self):
        usable = max(1, self.width() - 2 * self.MARGIN)
        return max(1, (usable + self.GAP) // self._pitch())

    def _rows(self):
        if not self.rows:
            return 0
        n = len(self.rows)
        return (n + self._cols() - 1) // self._cols()

    def _needed_height(self):
        if not self.rows:
            return 2 * self.MARGIN
        return 2 * self.MARGIN + self._rows() * self._pitch() - self.GAP

    def _relayout(self):
        h = self._needed_height()
        if self.minimumHeight() != h:
            self.setMinimumHeight(h)
        self.updateGeometry()

    def sizeHint(self):
        return QSize(0, self._needed_height())

    def resizeEvent(self, event):
        self._relayout()
        super().resizeEvent(event)

    # ---------------- 颜色 ----------------
    def _used_ratio(self, r):
        length = r.get('length') or 0
        free = r.get('free') or 0
        if not length:
            return 0.0
        return max(0.0, min(1.0, 1.0 - free / length))

    def _color_for(self, used):
        if used <= 0.5:
            t = used / 0.5
            a, b = self.C_EMPTY, self.C_HALF
        else:
            t = (used - 0.5) / 0.5
            a, b = self.C_HALF, self.C_FULL
        return QColor(int(a.red() + (b.red() - a.red()) * t),
                      int(a.green() + (b.green() - a.green()) * t),
                      int(a.blue() + (b.blue() - a.blue()) * t))

    # ---------------- 绘制 ----------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), self.C_BG)

        if not self.rows:
            p.setPen(self.C_DIM)
            f = QFont(); f.setPointSizeF(12); p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, self.empty_text)
            return

        cols = self._cols()
        pitch = self._pitch()
        clip = event.rect()

        r0 = max(0, (clip.top() - self.MARGIN) // pitch)
        r1 = min(self._rows() - 1, (clip.bottom() - self.MARGIN) // pitch)

        p.setPen(Qt.NoPen)
        for r in range(r0, r1 + 1):
            y = self.MARGIN + r * pitch
            base = r * cols
            for c in range(cols):
                i = base + c
                if i >= len(self.rows):
                    break
                p.setBrush(self._color_for(self._used_ratio(self.rows[i])))
                p.drawRect(self.MARGIN + c * pitch, y, self.cell, self.cell)

        if self._hover is not None and 0 <= self._hover < len(self.rows):
            i = self._hover
            x = self.MARGIN + (i % cols) * pitch
            y = self.MARGIN + (i // cols) * pitch
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(self.C_HOVER, 2))
            p.drawRect(x - 1, y - 1, self.cell + 2, self.cell + 2)

        self._draw_hover_box(p)

    def _hover_info(self):
        if self._hover is None:
            return None
        r = self.rows[self._hover]
        length = r.get('length') or 0
        free = r.get('free') or 0
        pct = free / length * 100 if length else 0
        return {
            'title': f"区 {r.get('row')}   ·   空闲 {pct:.2f}%",
            'lines': [
                ('起始簇', f"{r.get('start', 0):,}"),
                ('簇数',   f"{length:,}   "
                          f"({human_size(length * self.cluster_size)})"),
                ('空闲簇', f"{free:,}"),
                ('空闲段数', f"{r.get('segments', 0):,}"),
            ],
        }

    def _draw_hover_box(self, p):
        info = self._hover_info()
        pos = self._hover_pos
        if info is None or pos is None:
            return

        f_title = QFont(); f_title.setPointSizeF(9.5); f_title.setBold(True)
        f_body = QFont(); f_body.setPointSizeF(9)

        p.setFont(f_body)
        fm_body = p.fontMetrics()
        lw = max(fm_body.horizontalAdvance(k) for k, _ in info['lines'])
        vw = max(fm_body.horizontalAdvance(v) for _, v in info['lines'])
        lh = fm_body.height() + 3
        p.setFont(f_title)
        fm_title = p.fontMetrics()
        tw = fm_title.horizontalAdvance(info['title'])
        th = fm_title.height() + 2

        gap = 12
        box_w = max(lw + gap + vw, tw) + 22
        box_h = th + 10 + len(info['lines']) * lh + 8

        x = pos.x() + 14
        y = pos.y() + 14
        if x + box_w > self.width() - 4:
            x = pos.x() - box_w - 14
        if y + box_h > self.height() - 4:
            y = pos.y() - box_h - 14
        x = max(4, x); y = max(4, y)

        box = QRectF(x, y, box_w, box_h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 28))
        p.drawRoundedRect(box.translated(2, 2), 6, 6)
        p.setBrush(QColor(255, 255, 255, 245))
        p.setPen(QPen(QColor('#7a7a7a'), 1))
        p.drawRoundedRect(box, 6, 6)

        p.setFont(f_title); p.setPen(QColor('#1a1a1a'))
        p.drawText(QRectF(x + 10, y + 6, box_w - 20, th),
                   Qt.AlignLeft | Qt.AlignVCenter, info['title'])

        sep_y = y + 6 + th + 3
        p.setPen(QPen(QColor('#e0e0e0'), 1))
        p.drawLine(QPointF(x + 10, sep_y), QPointF(x + box_w - 8, sep_y))

        p.setFont(f_body)
        for i, (k, v) in enumerate(info['lines']):
            ly = sep_y + 3 + i * lh
            p.setPen(QColor('#777777'))
            p.drawText(QRectF(x + 10, ly, lw, lh),
                       Qt.AlignLeft | Qt.AlignVCenter, k)
            p.setPen(QColor('#1a1a1a'))
            p.drawText(QRectF(x + 10 + lw + gap, ly, vw + 4, lh),
                       Qt.AlignLeft | Qt.AlignVCenter, v)

    # ---------------- 交互 ----------------
    def _index_at(self, pos):
        if not self.rows:
            return None
        pitch = self._pitch()
        cols = self._cols()
        x = pos.x() - self.MARGIN
        y = pos.y() - self.MARGIN
        if x < 0 or y < 0:
            return None
        c = int(x // pitch)
        r = int(y // pitch)
        if not (0 <= c < cols):
            return None
        if x - c * pitch > self.cell or y - r * pitch > self.cell:
            return None
        i = r * cols + c
        return i if 0 <= i < len(self.rows) else None

    def mouseMoveEvent(self, event):
        self._hover_pos = event.position()
        self._hover = self._index_at(event.position())
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            i = self._index_at(event.position())
            if i is not None:
                self._hover = i
                self.cell_clicked.emit(i)
                self.update()
        else:
            super().mousePressEvent(event)

    def leaveEvent(self, event):
        self._hover = None
        self._hover_pos = None
        self.update()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            d = event.angleDelta().y()
            if d:
                self.set_cell_size(self.cell + (1 if d > 0 else -1))
            event.accept()
        else:
            event.ignore()


# =============================================================================
# Part 3 · Tab 集合（FAT 与 exFAT 共用，靠子类区分 TAB_FS）
# =============================================================================
class _ClusterOverviewTab(TabPlugin):
    TAB_ORDER = 10
    TAB_ID = 'overview'
    TAB_TITLE = '概览'
    NEEDS = ('full',)
    EMPTY_TEXT = '请在起始页选择一个设备'

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)

        self.title = QLabel(self.EMPTY_TEXT)
        self.title.setStyleSheet(
            "font-size:16px; font-weight:bold; padding:6px;")
        layout.addWidget(self.title)

        self.gb = QGroupBox("基本信息")
        self.grid = QGridLayout(self.gb)
        layout.addWidget(self.gb)

        gb_info = QGroupBox("引导扇区 / 结构明细")
        li = QVBoxLayout(gb_info)
        self.info_text = QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setFont(QFont("Monospace", 10))
        self.info_text.setMinimumHeight(260)
        li.addWidget(self.info_text)
        layout.addWidget(gb_info)

        scroll.setWidget(content)
        outer.addWidget(scroll)

    def update_data(self, payloads):
        payload = payloads.get('full')
        if not payload:
            return
        self.title.setText(f"设备: {payload.get('dev')}")

        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        for i, (k, v) in enumerate(payload.get('fields', [])):
            key = QLabel(f"{k}:")
            key.setStyleSheet("color:#555;")
            val = QLabel(str(v))
            val.setStyleSheet("font-family: monospace;")
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            val.setWordWrap(True)
            self.grid.addWidget(key, i, 0)
            self.grid.addWidget(val, i, 1)
        self.grid.setColumnStretch(1, 1)

        self.info_text.setPlainText(payload.get('info', ''))

    def clear(self):
        self.title.setText(self.EMPTY_TEXT)
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.info_text.clear()


class _ClusterTableTab(TabPlugin):
    """簇分布：每一"区"（连续簇段）的空闲/使用统计，点击跳到可视化"""

    TAB_ORDER = 20
    TAB_ID = 'chunks'
    TAB_TITLE = '簇分布'
    NEEDS = ('full',)

    group_activated = Signal(int)   # 点击某区 → 跳转到空闲可视化对应行

    COLS = ['区', '起始簇', '簇数', '空闲簇', '已用簇', '空闲%', '空闲段数']

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.cellClicked.connect(
            lambda r, _c: self.group_activated.emit(r))
        layout.addWidget(self.table)

    def update_data(self, payloads):
        payload = payloads.get('full')
        if not payload:
            return
        rows = payload.get('rows', [])
        row_size = int(payload.get('row_size') or 1)
        cluster_count = int(payload.get('cluster_count') or 0)

        self.table.setRowCount(len(rows))
        for i, (row_no, recs) in enumerate(rows):
            total = min(row_size, max(0, cluster_count - i * row_size))
            free = sum(c for _, c in recs)
            used = total - free
            pct = free / total * 100 if total else 0
            vals = [
                f"区 {row_no}",
                f"{2 + i * row_size:,}",
                f"{total:,}",
                f"{free:,}",
                f"{used:,}",
                f"{pct:.2f}%",
                f"{len(recs)}",
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, c, item)

    def clear(self):
        self.table.setRowCount(0)


class _ClusterHeatmapTab(TabPlugin):
    """主视图：簇热力图，回答"全局分布"（哪片区域空、哪片区域满）"""

    TAB_ORDER = 30
    TAB_ID = 'heatmap'
    TAB_TITLE = '簇热力图'
    NEEDS = ('full',)
    EMPTY_TEXT = '请在起始页选择一个设备'

    group_activated = Signal(int)   # 点击某区 → 跳转到空闲可视化

    LEGEND = ("<span style='color:#22c55e'>■</span> 空　"
              "<span style='color:#eab308'>■</span> 半　"
              "<span style='color:#ef4444'>■</span> 满　"
              "<span style='color:#999'>（Ctrl+滚轮 调格子大小）</span>")

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        self.title = QLabel(self.EMPTY_TEXT)
        self.title.setTextFormat(Qt.RichText)
        self.title.setStyleSheet(
            "font-size:14px; font-weight:bold; padding:6px;")
        layout.addWidget(self.title)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.heat = _ClusterHeatmap(self.EMPTY_TEXT)
        self.heat.cell_clicked.connect(self.group_activated)
        self.scroll.setWidget(self.heat)
        layout.addWidget(self.scroll)

    def update_data(self, payloads):
        payload = payloads.get('full')
        if not payload:
            return
        row_size = int(payload.get('row_size') or 1)
        cluster_count = int(payload.get('cluster_count') or 0)
        cluster_size = int(payload.get('cluster_size') or 4096)

        rows = []
        for i, (row_no, recs) in enumerate(payload.get('rows', [])):
            length = min(row_size, max(0, cluster_count - i * row_size))
            free = sum(c for _, c in recs)
            rows.append({'row': row_no, 'start': 2 + i * row_size,
                         'length': length, 'free': free,
                         'segments': len(recs)})

        total_free = sum(r['free'] for r in rows)
        pct = total_free / cluster_count * 100 if cluster_count else 0

        self.title.setText(
            f"设备: {payload.get('dev')}　·　{len(rows)} 个区　·　"
            f"总空闲 {pct:.2f}%　·　每格 = 1 个区　　{self.LEGEND}")
        self.heat.set_data(payload.get('dev'), rows, cluster_size)

    def clear(self):
        self.title.setText(self.EMPTY_TEXT)
        self.heat.clear()


class _ClusterPlotTab(TabPlugin):
    TAB_ORDER = 40
    TAB_ID = 'plot'
    TAB_TITLE = '空闲可视化'
    NEEDS = ('full',)
    EMPTY_TEXT = '请在起始页选择一个设备'
    ROW_PREFIX = '区'

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        grid = QGridLayout()
        grid.setSpacing(0)

        self.chart = BlockRangeChart(row_prefix=self.ROW_PREFIX,
                                     axis_label='簇号 (cluster)',
                                     empty_text=self.EMPTY_TEXT)
        self.vbar = QScrollBar(Qt.Vertical)
        self.hbar = QScrollBar(Qt.Horizontal)

        vbar_holder = QWidget()
        vl = QVBoxLayout(vbar_holder)
        vl.setContentsMargins(0, BlockRangeChart.MT, 0, BlockRangeChart.MB)
        vl.setSpacing(0)
        vl.addWidget(self.vbar)

        hbar_holder = QWidget()
        hl = QHBoxLayout(hbar_holder)
        hl.setContentsMargins(BlockRangeChart.ML, 0, BlockRangeChart.MR, 0)
        hl.setSpacing(0)
        hl.addWidget(self.hbar)

        grid.addWidget(self.chart,  0, 0)
        grid.addWidget(vbar_holder, 0, 1)
        grid.addWidget(hbar_holder, 1, 0)
        grid.setRowStretch(0, 1)
        grid.setColumnStretch(0, 1)
        layout.addLayout(grid)

        self.chart.attach_scrollbars(self.vbar, self.hbar)

    def update_data(self, payloads):
        payload = payloads.get('full')
        if not payload:
            return
        rows = [(n, [tuple(r) for r in recs])
                for n, recs in payload.get('rows', [])]
        self.chart.set_data(payload.get('dev'), rows,
                            int(payload.get('row_size') or 1),
                            int(payload.get('cluster_size') or 4096))

    def focus_group(self, row_index):
        """定位到指定"区"（供簇分布表点击跳转）"""
        return self.chart.focus_row(row_index)

    def clear(self):
        self.chart.clear()


# --------------------------- FAT12/16/32 Tab ---------------------------
@register_tab
class FatOverviewTab(_ClusterOverviewTab):
    TAB_FS = 'fat'
    EMPTY_TEXT = '请在起始页选择一个 FAT 设备'


@register_tab
class FatClusterTableTab(_ClusterTableTab):
    TAB_FS = 'fat'


@register_tab
class FatHeatmapTab(_ClusterHeatmapTab):
    TAB_FS = 'fat'
    EMPTY_TEXT = '请在起始页选择一个 FAT 设备'


@register_tab
class FatPlotTab(_ClusterPlotTab):
    TAB_FS = 'fat'
    EMPTY_TEXT = '请在起始页选择一个 FAT 设备'


# --------------------------- exFAT Tab ---------------------------
@register_tab
class ExfatOverviewTab(_ClusterOverviewTab):
    TAB_FS = 'exfat'
    EMPTY_TEXT = '请在起始页选择一个 exFAT 设备'


@register_tab
class ExfatClusterTableTab(_ClusterTableTab):
    TAB_FS = 'exfat'


@register_tab
class ExfatHeatmapTab(_ClusterHeatmapTab):
    TAB_FS = 'exfat'
    EMPTY_TEXT = '请在起始页选择一个 exFAT 设备'


@register_tab
class ExfatPlotTab(_ClusterPlotTab):
    TAB_FS = 'exfat'
    EMPTY_TEXT = '请在起始页选择一个 exFAT 设备'


# =============================================================================
# Part 4 · 包描述
# =============================================================================
@register_pack
class FatPack(FSPack):
    FS_ID = 'fat'
    FS_NAME = 'FAT12/16/32'
    ORDER = 30

    @classmethod
    def matches_fstype(cls, fstype):
        return (fstype or '').lower() in (
            'vfat', 'fat', 'msdos', 'fat12', 'fat16', 'fat32')


@register_pack
class ExfatPack(FSPack):
    FS_ID = 'exfat'
    FS_NAME = 'exFAT'
    ORDER = 31

    @classmethod
    def matches_fstype(cls, fstype):
        return (fstype or '').lower() == 'exfat'
