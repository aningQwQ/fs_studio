#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fs_packs.ntfs — NTFS 包

自包含一个文件系统所需的全部内容:
    collector : collect_ntfs()   （root 侧，直接读引导扇区 + $MFT + $Bitmap）
    tabs      : 概览 / 簇分布 / 簇热力图 / 空闲可视化

直接解析 NTFS 结构（引导扇区 → $MFT 数据运行 → $Bitmap 分配位图），
不依赖 ntfsprogs / ntfs-3g，只要求采集时有权限打开设备。

带走这个包 = 复制本文件。
"""
import re
import struct

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QGroupBox, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QPlainTextEdit, QScrollArea,
                               QScrollBar, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .core import (BlockRangeChart, FSPack, TabPlugin, human_size,
                   register_collector, register_pack, register_tab)

TARGET_ROWS = 128

ATTR_ATTRIBUTE_LIST = 0x20
ATTR_VOLUME_NAME = 0x60
ATTR_VOLUME_INFO = 0x70
ATTR_DATA = 0x80
ATTR_BITMAP = 0xB0

MFT_MFT = 0
MFT_VOLUME = 3
MFT_BITMAP = 6


# =============================================================================
# Part 0 · 二进制读取通用工具
# =============================================================================
def _u16(b, o):
    return struct.unpack_from('<H', b, o)[0]


def _u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


def _u64(b, o):
    return struct.unpack_from('<Q', b, o)[0]


def _s8(b, o):
    return struct.unpack_from('<b', b, o)[0]


def _ceil_div(a, b):
    return (a + b - 1) // b


def _read_at(fh, offset, size):
    fh.seek(offset)
    return fh.read(size)


def _hexdump(data, base=0):
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hx = ' '.join(f"{b:02X}" for b in chunk)
        hx = hx.ljust(16 * 3 - 1)
        asc = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f"{base + off:08X}  {hx}  |{asc}|")
    return '\n'.join(lines)


def _chunk_rows(cluster_count, runs, base=0, target=TARGET_ROWS):
    if cluster_count <= 0:
        return [], 1
    row_size = max(1, _ceil_div(cluster_count, target))
    nrows = _ceil_div(cluster_count, row_size)
    rows = [[k, []] for k in range(nrows)]
    for start, count in runs:
        s, e = start, start + count
        while s < e:
            i = (s - base) // row_size
            if not (0 <= i < nrows):
                break
            c0 = base + i * row_size
            c1 = base + min(cluster_count, (i + 1) * row_size)
            a, b = max(s, c0), min(e, c1)
            if b > a:
                rows[i][1].append((a - c0, b - a))
            s = b
    return rows, row_size


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

# 成片的 0x00（全空闲）/ 0xFF（全占用）字节由 C 级正则在字节层面整段跳过，
# 只有"混合"字节才需要逐位处理，避免 O(簇数) 的 Python 循环。
_BITMAP_SPLIT = re.compile(rb'\x00+|\xff+|[^\x00\xff]+')


def _scan_bitmap_free(bm, cluster_count):
    """
    扫描 NTFS $Bitmap，返回 (空闲区间, 空闲簇数)。
    注意：NTFS 位图第 i 位对应 LCN i（簇号从 0 开始，与 FAT/exFAT 从 2 起不同）。
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
            a = b0
            b = min(b0 + len(seg) * 8, total)
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
                base = b0 + j * 8
                if base >= total:
                    break
                for sb, ln in _FREE_BYTE_RUNS[byte]:
                    a = base + sb
                    if a >= total:
                        break
                    b = min(a + ln, total)
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


# =============================================================================
# Part 1 · NTFS 结构解析
# =============================================================================
def _parse_boot(bs):
    if len(bs) < 512 or bs[510] != 0x55 or bs[511] != 0xAA:
        raise RuntimeError("不是有效的 NTFS 引导扇区（缺少 0xAA55 签名）")
    if bs[3:11] != b'NTFS    ':
        raise RuntimeError("不是有效的 NTFS 引导扇区（OEM 标识不是 NTFS）")

    bps = _u16(bs, 0x0B)
    spc = bs[0x0D]
    total_sectors = _u64(bs, 0x28)
    if bps not in (512, 1024, 2048, 4096) or spc == 0 or not total_sectors:
        raise RuntimeError("NTFS 引导扇区字段非法")

    cluster_size = bps * spc
    cpm = _s8(bs, 0x40)   # clusters per MFT record（0x41..0x43 保留）
    cpi = _s8(bs, 0x44)   # clusters per index record（0x45..0x47 保留）
    mft_record_size = cpm * cluster_size if cpm > 0 else 1 << (-cpm)
    index_record_size = cpi * cluster_size if cpi > 0 else 1 << (-cpi)
    if not (256 <= mft_record_size <= 65536):
        raise RuntimeError("NTFS MFT 记录大小非法")

    return {
        'bytes_per_sector': bps,
        'sectors_per_cluster': spc,
        'cluster_size': cluster_size,
        'total_sectors': total_sectors,
        'cluster_count': total_sectors // spc,
        'mft_lcn': _u64(bs, 0x30),
        'mftmirr_lcn': _u64(bs, 0x38),
        'mft_record_size': mft_record_size,
        'index_record_size': index_record_size,
        'serial': _u64(bs, 0x48),
    }


def _fixup(rec, sector_size):
    """还原 NTFS 记录的更新序列（fixup），否则跨扇区字段会被 USN 覆盖"""
    if rec[0:4] != b'FILE' or len(rec) < 0x30:
        return rec
    usa_off = _u16(rec, 4)
    usa_cnt = _u16(rec, 6)
    if usa_cnt == 0 or usa_off + usa_cnt * 2 > len(rec):
        return rec
    out = bytearray(rec)
    for i in range(1, usa_cnt):
        end = (i + 1) * sector_size
        if end > len(out):
            break
        out[end - 2:end] = rec[usa_off + 2 * i: usa_off + 2 * i + 2]
    return bytes(out)


def _parse_runs(buf, start_vcn=0):
    """解析 NTFS 数据运行列表，返回绝对 VCN 的 [(vcn, lcn|None, count), ...]"""
    runs = []
    i = 0
    vcn = start_vcn
    lcn = 0
    while i < len(buf):
        header = buf[i]
        i += 1
        if header == 0:
            break
        len_size = header & 0x0F
        off_size = (header >> 4) & 0x0F
        if len_size == 0 or i + len_size + off_size > len(buf):
            break
        length = int.from_bytes(buf[i:i + len_size], 'little')
        i += len_size
        if length == 0:
            break
        if off_size == 0:
            runs.append((vcn, None, length))          # 稀疏运行
        else:
            delta = int.from_bytes(buf[i:i + off_size], 'little', signed=True)
            i += off_size
            lcn += delta
            runs.append((vcn, lcn, length))
        vcn += length
    return runs


def _iter_attrs(rec):
    if rec[0:4] != b'FILE':
        return
    attr_off = _u16(rec, 0x14)
    used = _u32(rec, 0x18)
    end = min(len(rec), used) if used else len(rec)
    while attr_off + 8 <= end:
        atype = _u32(rec, attr_off)
        if atype == 0xFFFFFFFF:
            break
        alen = _u32(rec, attr_off + 4)
        if alen < 0x18 or attr_off + alen > len(rec):
            break
        nonres = rec[attr_off + 8]
        namelen = rec[attr_off + 9]
        nameoff = _u16(rec, attr_off + 0x0A)
        name = ''
        if namelen and attr_off + nameoff + namelen * 2 <= len(rec):
            name = rec[attr_off + nameoff:
                       attr_off + nameoff + namelen * 2].decode(
                           'utf-16-le', 'replace')
        a = {'type': atype, 'nonres': bool(nonres), 'name': name,
             'aid': _u16(rec, attr_off + 0x0E)}
        if nonres:
            a['start_vcn'] = _u64(rec, attr_off + 0x10)
            dro = _u16(rec, attr_off + 0x20)
            a['alloc_size'] = _u64(rec, attr_off + 0x28)
            a['data_size'] = _u64(rec, attr_off + 0x30)
            a['runs'] = _parse_runs(rec[attr_off + dro: attr_off + alen],
                                    a['start_vcn'])
        else:
            clen = _u32(rec, attr_off + 0x10)
            coff = _u16(rec, attr_off + 0x14)
            a['data'] = rec[attr_off + coff: attr_off + coff + clen]
            a['data_size'] = clen
            a['alloc_size'] = clen
            a['start_vcn'] = 0
            a['runs'] = []
        yield a
        attr_off += alen


def _find_attr(attrs, atype, name=None):
    for a in attrs:
        if a['type'] != atype:
            continue
        if name is not None and a['name'] != name:
            continue
        return a
    return None


def _find_run(runs, vcn):
    """二分查找包含 vcn 的数据运行（runs 按 VCN 升序）"""
    lo, hi = 0, len(runs) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        vcn_start, lcn, count = runs[mid]
        if vcn < vcn_start:
            hi = mid - 1
        elif vcn >= vcn_start + count:
            lo = mid + 1
        else:
            return runs[mid]
    return None


def _map_vcn(runs, vcn):
    run = _find_run(runs, vcn)
    if run is None or run[1] is None:
        return None
    return run[1] + (vcn - run[0])


def _read_by_runs(fh, boot, runs, start_vcn, size):
    """按运行表读取数据，同一物理连续段合并为一次 read（减少系统调用）"""
    cs = boot['cluster_size']
    out = bytearray()
    vcn = start_vcn
    remaining = size
    while remaining > 0:
        run = _find_run(runs, vcn)
        if run is None or run[1] is None:
            break
        vcn_start, lcn, count = run
        off = vcn - vcn_start
        nclusters = min(count - off, _ceil_div(remaining, cs))
        take = min(nclusters * cs, remaining)
        out += _read_at(fh, (lcn + off) * cs, take)
        vcn += nclusters
        remaining -= take
    return bytes(out)


def _read_mft_record(fh, boot, runs, rec_no):
    cs = boot['cluster_size']
    rec_size = boot['mft_record_size']
    off = rec_no * rec_size
    size = rec_size
    out = bytearray()
    while size > 0:
        vcn = off // cs
        skip = off % cs
        lcn = _map_vcn(runs, vcn)
        if lcn is None:
            return None
        take = min(cs - skip, size)
        out += _read_at(fh, lcn * cs + skip, take)
        off += take
        size -= take
    return _fixup(bytes(out), boot['bytes_per_sector'])


def _attr_data(fh, boot, attr):
    if not attr['nonres']:
        return attr['data']
    return _read_by_runs(fh, boot, attr['runs'], attr['start_vcn'],
                         attr['data_size'])


def _merge_runs(runs):
    return sorted(runs, key=lambda r: r[0])


def _parse_attr_list(data):
    entries = []
    i = 0
    while i + 0x1A <= len(data):
        atype = _u32(data, i)
        if atype in (0, 0xFFFFFFFF):
            break
        alen = _u16(data, i + 4)
        if alen < 0x1A or i + alen > len(data):
            break
        entries.append({
            'type': atype,
            'name_len': data[i + 6],
            'start_vcn': _u64(data, i + 8),
            'ref': _u64(data, i + 0x10),
            'aid': _u16(data, i + 0x18),
        })
        i += alen
    return entries


def _mft_map(fh, boot):
    """返回 ($MFT 数据运行表, MFT 已分配字节数)，必要时跟随属性列表"""
    cs = boot['cluster_size']
    rec_size = boot['mft_record_size']

    rec0 = _fixup(_read_at(fh, boot['mft_lcn'] * cs, rec_size),
                  boot['bytes_per_sector'])
    attrs = list(_iter_attrs(rec0))
    data = _find_attr(attrs, ATTR_DATA, '')
    if data is None:
        raise RuntimeError("NTFS $MFT 缺少 $DATA 属性")
    runs = list(data['runs'])
    alloc = data['alloc_size'] or data['data_size']
    if not runs and not data['nonres']:
        raise RuntimeError("NTFS $MFT 数据为常驻属性，结构异常")

    need_vcn = _ceil_div(alloc, cs)
    covered = max((v + c for v, _, c in runs), default=0)
    alist = _find_attr(attrs, ATTR_ATTRIBUTE_LIST, '')

    if alist is not None and covered < need_vcn:
        entries = _parse_attr_list(_attr_data(fh, boot, alist))
        for e in sorted(entries, key=lambda x: x['start_vcn']):
            if e['type'] != ATTR_DATA or e['name_len'] != 0:
                continue
            rec_no = e['ref'] & 0xFFFFFFFFFFFF
            ext = _read_mft_record(fh, boot, runs, rec_no)
            if ext is None:
                continue
            for a in _iter_attrs(ext):
                if (a['type'] == ATTR_DATA and not a['nonres']
                        and a['name'] == ''
                        and a['start_vcn'] == e['start_vcn']):
                    runs.extend(a['runs'])
                    break
        runs = _merge_runs(runs)

    return runs, alloc


@register_collector('ntfs', 'full')
def collect_ntfs(dev):
    try:
        fh = open(dev, 'rb', buffering=0)
    except OSError as e:
        raise RuntimeError(f"无法打开设备 {dev}: {e}")

    with fh:
        bs = _read_at(fh, 0, 512)
        boot = _parse_boot(bs)
        cluster_count = boot['cluster_count']

        runs, mft_alloc = _mft_map(fh, boot)

        bmp_rec = _read_mft_record(fh, boot, runs, MFT_BITMAP)
        if bmp_rec is None:
            raise RuntimeError("无法定位 $Bitmap（MFT 映射不完整）")
        bmp_attr = _find_attr(list(_iter_attrs(bmp_rec)), ATTR_DATA, '')
        if bmp_attr is None:
            raise RuntimeError("$Bitmap 缺少 unnamed $DATA 属性")
        bm = _attr_data(fh, boot, bmp_attr)
        if len(bm) < _ceil_div(cluster_count, 8):
            raise RuntimeError("$Bitmap 读取不完整")

        label = ''
        volume_version = ''
        vol_rec = _read_mft_record(fh, boot, runs, MFT_VOLUME)
        if vol_rec is not None:
            for a in _iter_attrs(vol_rec):
                if a['type'] == ATTR_VOLUME_NAME and not a['nonres']:
                    label = a['data'].decode('utf-16-le', 'replace').strip()
                elif a['type'] == ATTR_VOLUME_INFO and not a['nonres']:
                    if len(a['data']) >= 10:
                        volume_version = f"{a['data'][8]}.{a['data'][9]}"

        mft_records = mft_alloc // boot['mft_record_size']
        mft_in_use = None
        mft_bmp = _find_attr(list(_iter_attrs(_fixup(
            _read_at(fh, boot['mft_lcn'] * boot['cluster_size'],
                     boot['mft_record_size']),
            boot['bytes_per_sector']))), ATTR_BITMAP, None)
        if mft_bmp is not None:
            try:
                mb = _attr_data(fh, boot, mft_bmp)
                mft_in_use = int.from_bytes(mb, 'little').bit_count()
            except Exception:
                mft_in_use = None

    free_runs, free = _scan_bitmap_free(bm, cluster_count)

    used = cluster_count - free
    pct = free / cluster_count * 100 if cluster_count else 0
    rows, row_size = _chunk_rows(cluster_count, free_runs, base=0)

    fields = [
        ('文件系统', 'NTFS'),
        ('卷标', label or '-'),
        ('版本', volume_version or '-'),
        ('卷序列号', f"0x{boot['serial']:016X}"),
        ('每扇区字节', f"{boot['bytes_per_sector']}"),
        ('每簇扇区', f"{boot['sectors_per_cluster']}"),
        ('簇大小', human_size(boot['cluster_size'])),
        ('扇区总数', f"{boot['total_sectors']:,}"),
        ('设备容量', human_size(boot['total_sectors']
                            * boot['bytes_per_sector'])),
        ('簇总数', f"{cluster_count:,}"),
        ('空闲簇', f"{free:,}"),
        ('已用簇', f"{used:,}"),
        ('空闲比例', f"{pct:.2f}%"),
        ('MFT 起始簇', f"{boot['mft_lcn']:,}"),
        ('MFT 镜像起始簇', f"{boot['mftmirr_lcn']:,}"),
        ('MFT 记录大小', f"{boot['mft_record_size']} B"),
        ('MFT 记录数', f"{mft_records:,}"
                     + (f"（在用 {mft_in_use:,}）"
                        if mft_in_use is not None else "")),
        ('索引记录大小', f"{boot['index_record_size']} B"),
    ]

    info = (f"引导扇区 (Boot Sector)\n"
            f"{_hexdump(bs)}\n\n"
            f"$MFT 数据运行段数: {len(runs)}    "
            f"$MFT 已分配: {human_size(mft_alloc)}\n"
            f"$Bitmap 数据: {len(bm):,} 字节    "
            f"空闲区间数: {len(free_runs):,}\n")

    return {
        'dev': dev, 'fs': 'ntfs',
        'fields': fields,
        'info': info,
        'cluster_size': boot['cluster_size'],
        'cluster_count': cluster_count,
        'free_clusters': free,
        'rows': rows,
        'row_size': row_size,
    }


# =============================================================================
# Part 2 · 簇热力图（主视图：回答"全局分布"）
# =============================================================================
class _ClusterHeatmap(QWidget):
    """
    借鉴 ext4 的块组热力图：把等大小的"区"排成矩阵，一格 = 一个区，
    颜色浓淡 = 使用率（绿=空 → 黄=半 → 红=满）。
    左键点击某格 → cell_clicked(区号)，用于跳转到空闲可视化对应行。
    """
    cell_clicked = Signal(int)

    MARGIN = 10
    GAP = 3

    C_BG    = QColor('#ffffff')
    C_EMPTY = QColor('#22c55e')
    C_HALF  = QColor('#eab308')
    C_FULL  = QColor('#ef4444')
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
# Part 3 · Tab 集合
# =============================================================================
@register_tab
class NtfsOverviewTab(TabPlugin):
    TAB_FS = 'ntfs'
    TAB_ORDER = 10
    TAB_ID = 'overview'
    TAB_TITLE = '概览'
    NEEDS = ('full',)
    EMPTY_TEXT = '请在起始页选择一个 NTFS 设备'

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

    def _clear_grid(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def update_data(self, payloads):
        payload = payloads.get('full')
        if not payload:
            return
        self.title.setText(f"设备: {payload.get('dev')}")
        self._clear_grid()
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
        self._clear_grid()
        self.info_text.clear()


@register_tab
class NtfsClusterTableTab(TabPlugin):
    """簇分布：每一"区"（连续簇段）的空闲/使用统计，点击跳到可视化"""

    TAB_FS = 'ntfs'
    TAB_ORDER = 20
    TAB_ID = 'chunks'
    TAB_TITLE = '簇分布'
    NEEDS = ('full',)

    group_activated = Signal(int)

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
            vals = [f"区 {row_no}", f"{i * row_size:,}", f"{total:,}",
                    f"{free:,}", f"{used:,}", f"{pct:.2f}%", f"{len(recs)}"]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, c, item)

    def clear(self):
        self.table.setRowCount(0)


@register_tab
class NtfsHeatmapTab(TabPlugin):
    """主视图：簇热力图，回答"全局分布"（哪片区域空、哪片区域满）"""

    TAB_FS = 'ntfs'
    TAB_ORDER = 30
    TAB_ID = 'heatmap'
    TAB_TITLE = '簇热力图'
    NEEDS = ('full',)
    EMPTY_TEXT = '请在起始页选择一个 NTFS 设备'

    group_activated = Signal(int)

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
            rows.append({'row': row_no, 'start': i * row_size,
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


@register_tab
class NtfsPlotTab(TabPlugin):
    TAB_FS = 'ntfs'
    TAB_ORDER = 40
    TAB_ID = 'plot'
    TAB_TITLE = '空闲可视化'
    NEEDS = ('full',)
    EMPTY_TEXT = '请在起始页选择一个 NTFS 设备'
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
        hl.setContentsMargins(BlockRangeChart.ML, 0, 0, BlockRangeChart.MR)
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
        return self.chart.focus_row(row_index)

    def clear(self):
        self.chart.clear()


# =============================================================================
# Part 4 · 包描述
# =============================================================================
@register_pack
class NtfsPack(FSPack):
    FS_ID = 'ntfs'
    FS_NAME = 'NTFS'
    ORDER = 25

    @classmethod
    def matches_fstype(cls, fstype):
        return (fstype or '').lower() in ('ntfs', 'ntfs3')
