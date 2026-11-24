#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fs_packs.f2fs — F2FS 包

自包含一个文件系统所需的全部内容:
    collector : collect_f2fs()   （root 侧，直接读超级块 + 检查点 + SIT）
    tabs      : 概览 / 区分布 / 区热力图 / 空闲可视化

直接解析 F2FS 磁盘结构，不依赖 f2fs-tools：
    超级块(offset 1024) → 检查点(两份，取较新者) → SIT 区(含版本位图)
    → 叠加检查点里的 SIT 日志 → 每个段的 valid_map 位图(一块一位)

为什么必须叠加 SIT 日志：F2FS 的 SIT 区是懒写回的，格式化后 SIT 区全零，
初始的段占用只存在于检查点的 SIT 日志里；只有叠加日志才与 f2fs 挂载时一致。

带走这个包 = 复制本文件。
"""
import re
import struct

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QPlainTextEdit, QScrollArea,
                               QScrollBar, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .core import (BlockRangeChart, FSPack, TabPlugin, human_size,
                   register_collector, register_pack, register_tab)

MAX_CELLS = 1024          # 热力图最多格子数（超过则合并多个区为一行）
MAX_RUNS = 3000000        # 空闲区间数上限；超过则退回"分箱"粗略模式
BINS_PER_ROW = 256        # 分箱模式每行的箱子数

# ---- 超级块 / 检查点 / SIT 的磁盘常量（与 f2fs_fs.h 一致）----
F2FS_MAGIC = 0xF2F52010
SB_OFFSET = 1024                  # 超级块在块 0 内的字节偏移
SB_SIZE = 3072                    # sizeof(struct f2fs_super_block)
OFF_CP_PAYLOAD = 1664             # sb->cp_payload
SIT_ENTRY_SIZE = 74               # vblocks(2) + valid_map(64) + mtime(8)
SIT_VBLOCKS_MASK = 0x3FF          # [9:0] 有效块数，[15:10] 段类型
CKPT_SIT_BITMAP_OFF = 192         # ckpt->sit_nat_version_bitmap
SUM_ENTRIES_SIZE = 3584           # 512 条 summary * 7 字节
SUM_JOURNAL_SIZE = 507            # 4096 - 3584 - 5
SIT_JOURNAL_ENTRIES = 6
CP_UMOUNT_FLAG = 0x0001
CP_COMPACT_SUM_FLAG = 0x0004
CURSEG_COLD_DATA = 2              # SIT 日志挂在冷数据段的 summary 里
NR_CURSEG_TYPE = 6
NR_CURSEG_DATA_TYPE = 3


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
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hx = ' '.join(f"{b:02X}" for b in chunk)
        hx = hx.ljust(16 * 3 - 1)
        asc = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f"{base + off:08X}  {hx}  |{asc}|")
    return '\n'.join(lines)


def _bit_test_msb(buf, bit):
    """F2FS 的位图按 MSB-first 编号（与 f2fs_test_bit 一致）"""
    if bit >> 3 >= len(buf):
        return False
    return (buf[bit >> 3] & (0x80 >> (bit & 7))) != 0


def _free_byte_runs():
    """每个字节值 → 该字节内空闲位(0 位，MSB-first)的 (起始位, 长度) 列表"""
    table = []
    for v in range(256):
        lst = []
        k = 0
        while k < 8:
            if not ((v >> (7 - k)) & 1):
                s = k
                while k < 8 and not ((v >> (7 - k)) & 1):
                    k += 1
                lst.append((s, k - s))
            else:
                k += 1
        table.append(tuple(lst))
    return table


_FREE_BYTE_RUNS = _free_byte_runs()

# 成片的 0x00（整段空闲）/ 0xFF（整段占用）由 C 级正则整段跳过，
# 只有"混合"字节才逐位处理，避免 O(块数) 的 Python 循环。
_BITMAP_SPLIT = re.compile(rb'\x00+|\xff+|[^\x00\xff]+')


def _scan_free_runs(bm, total_bits, max_runs=MAX_RUNS):
    """
    扫描"已用位图"(1=已用，MSB-first)，返回 (空闲区间, 空闲位数)。
    区间以块为单位、从 0 开始（调用方再加 main_blkaddr）。
    区间数超过 max_runs 时返回 None，交由调用方退到分箱模式。
    """
    runs = []
    free = 0
    run_start = None
    run_end = 0

    for m in _BITMAP_SPLIT.finditer(bm):
        if len(runs) > max_runs:
            return None
        seg = m.group()
        b0 = m.start() * 8
        if b0 >= total_bits:
            break
        first = seg[0]
        if first == 0x00:                       # 整段空闲
            a = b0
            b = min(b0 + len(seg) * 8, total_bits)
            if run_start is not None and a == run_end:
                run_end = b
            else:
                if run_start is not None:
                    runs.append((run_start, run_end - run_start))
                run_start, run_end = a, b
            free += b - a
        elif first == 0xFF:                     # 整段占用
            if run_start is not None:
                runs.append((run_start, run_end - run_start))
                run_start = None
        else:                                   # 混合字节：查表逐位
            for j, byte in enumerate(seg):
                base = b0 + j * 8
                if base >= total_bits:
                    break
                for sb, ln in _FREE_BYTE_RUNS[byte]:
                    a = base + sb
                    if a >= total_bits:
                        break
                    b = min(a + ln, total_bits)
                    if run_start is not None and a == run_end:
                        run_end = b
                    else:
                        if run_start is not None:
                            runs.append((run_start, run_end - run_start))
                        run_start, run_end = a, b
                    free += b - a
            if len(runs) > max_runs:
                return None

    if run_start is not None:
        runs.append((run_start, run_end - run_start))
    return runs, free


def _free_bits_in(bm, start_bit, nbits):
    """统计 [start_bit, start_bit+nbits) 内的空闲(0)位数，MSB-first"""
    if nbits <= 0:
        return 0
    end = start_bit + nbits
    s_byte = start_bit >> 3
    e_byte = (end - 1) >> 3

    if s_byte == e_byte:                    # 落在同一个字节内
        byte = bm[s_byte]
        e_off = end & 7 or 8
        total = 0
        for k in range(start_bit & 7, e_off):
            if not (byte >> (7 - k)) & 1:
                total += 1
        return total

    total = 0
    byte = bm[s_byte]
    for k in range(start_bit & 7, 8):       # 头字节：逐位
        if not (byte >> (7 - k)) & 1:
            total += 1

    if end & 7:                             # 末字节只取前 end%8 位
        mid_end, tail_bits = e_byte - 1, end & 7
    else:                                   # 末字节整字节纳入
        mid_end, tail_bits = e_byte, 0

    full = mid_end - s_byte                 # 中间整字节：C 级 popcount
    if full > 0:
        chunk = bm[s_byte + 1:s_byte + 1 + full]
        total += len(chunk) * 8 - int.from_bytes(chunk, 'big').bit_count()

    if tail_bits:
        byte = bm[e_byte]
        for k in range(tail_bits):
            if not (byte >> (7 - k)) & 1:
                total += 1
    return total


def _binned_runs(bm, total_bits, row_blocks, nrows):
    """
    病态碎片时的降级表示：把每行再切成 BINS_PER_ROW 个箱，箱内只保留
    "空闲块数"，画在箱首。每行空闲块数仍然精确，只是箱内定位粗糙。
    """
    rows = [[i, []] for i in range(nrows)]
    free_total = 0
    bin_blocks = max(1, _ceil_div(row_blocks, BINS_PER_ROW))
    for i in range(nrows):
        base_bit = i * row_blocks
        row_bits = min(row_blocks, total_bits - base_bit)
        if row_bits <= 0:
            continue
        nbin = _ceil_div(row_bits, bin_blocks)
        for k in range(nbin):
            b0 = k * bin_blocks
            nb = min(bin_blocks, row_bits - b0)
            fb = _free_bits_in(bm, base_bit + b0, nb)
            if fb:
                rows[i][1].append((b0, fb))
                free_total += fb
    return rows, free_total


def _assemble_used(valid_maps, main_segs, bps):
    """
    把每段 64 字节的 valid_map 拼成一整条"已用位"流。
    返回 (字节串, 总位数, 每位的块数)：4K 块时每位就是一块（scale=1）。
    """
    if bps == 512:                      # 常见情况，整块即 512 位
        return bytes(valid_maps), main_segs * 512, 1

    if bps > 512:                       # 块小于 4K（f2fs 实际不支持）
        nbytes = 64
        out = bytearray()
        for s in range(main_segs):
            out += valid_maps[s * 64:(s + 1) * 64]
        return bytes(out), main_segs * 512, bps // 512

    nbytes = max(1, bps // 8)           # 块大于 4K：只用有效的那几位
    out = bytearray()
    for s in range(main_segs):
        out += valid_maps[s * 64:s * 64 + nbytes]
    return bytes(out), main_segs * nbytes * 8, 1


def _split_runs_into_rows(runs, row_blocks, total_blocks):
    """把空闲区间按 row_blocks 切到各行（行内偏移）"""
    nrows = max(1, _ceil_div(total_blocks, row_blocks))
    rows = [[i, []] for i in range(nrows)]
    for s, c in runs:
        e = s + c
        while s < e:
            r = s // row_blocks
            if r >= nrows:
                break
            r0 = r * row_blocks
            r1 = min(total_blocks, r0 + row_blocks)
            a, b = max(s, r0), min(e, r1)
            if b > a:
                rows[r][1].append((a - r0, b - a))
            s = b
    return rows


# =============================================================================
# Part 1 · 超级块
# =============================================================================
def _parse_sb(raw):
    if len(raw) < SB_SIZE:
        raise RuntimeError("读取超级块失败（设备过短）")
    if _u32(raw, 0) != F2FS_MAGIC:
        raise RuntimeError("不是有效的 F2FS 超级块（magic 不匹配）")

    sb = {
        'magic': _u32(raw, 0),
        'major_ver': _u16(raw, 4),
        'minor_ver': _u16(raw, 6),
        'log_sectorsize': _u32(raw, 8),
        'log_sectors_per_block': _u32(raw, 12),
        'log_blocksize': _u32(raw, 16),
        'log_blocks_per_seg': _u32(raw, 20),
        'segs_per_sec': _u32(raw, 24),
        'secs_per_zone': _u32(raw, 28),
        'checksum_offset': _u32(raw, 32),
        'block_count': _u64(raw, 36),
        'section_count': _u32(raw, 44),
        'segment_count': _u32(raw, 48),
        'segment_count_ckpt': _u32(raw, 52),
        'segment_count_sit': _u32(raw, 56),
        'segment_count_nat': _u32(raw, 60),
        'segment_count_ssa': _u32(raw, 64),
        'segment_count_main': _u32(raw, 68),
        'segment0_blkaddr': _u32(raw, 72),
        'cp_blkaddr': _u32(raw, 76),
        'sit_blkaddr': _u32(raw, 80),
        'nat_blkaddr': _u32(raw, 84),
        'ssa_blkaddr': _u32(raw, 88),
        'main_blkaddr': _u32(raw, 92),
        'root_ino': _u32(raw, 96),
        'node_ino': _u32(raw, 100),
        'meta_ino': _u32(raw, 104),
        'uuid': raw[108:124],
        'cp_payload': _u32(raw, OFF_CP_PAYLOAD),
    }
    name = raw[124:124 + 1024].decode('utf-16-le', 'replace')
    sb['volume_name'] = name.split('\x00', 1)[0].strip()

    log_bs = sb['log_blocksize']
    if not (9 <= log_bs <= 16):
        raise RuntimeError(f"F2FS 块大小非法（log_blocksize={log_bs}）")
    sb['block_size'] = 1 << log_bs
    sb['blocks_per_seg'] = 1 << sb['log_blocks_per_seg']
    if sb['blocks_per_seg'] <= 0 or sb['blocks_per_seg'] > 4096:
        raise RuntimeError("F2FS 每段块数非法")
    if sb['segs_per_sec'] <= 0:
        sb['segs_per_sec'] = 1
    if not sb['segment_count_main'] or not sb['main_blkaddr']:
        raise RuntimeError("F2FS 超级块缺少主区信息")
    return sb


def _fmt_uuid(b):
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# =============================================================================
# Part 2 · 检查点（两份副本，取 checkpoint_ver 较新者）
# =============================================================================
def _parse_ckpt(raw):
    if len(raw) < CKPT_SIT_BITMAP_OFF:
        raise RuntimeError("读取检查点失败")
    return {
        'checkpoint_ver': _u64(raw, 0),
        'user_block_count': _u64(raw, 8),
        'valid_block_count': _u64(raw, 16),
        'rsvd_segment_count': _u32(raw, 24),
        'overprov_segment_count': _u32(raw, 28),
        'free_segment_count': _u32(raw, 32),
        'cur_node_segno': [_u32(raw, 36 + 4 * i) for i in range(8)],
        'cur_data_segno': [_u32(raw, 84 + 4 * i) for i in range(8)],
        'ckpt_flags': _u32(raw, 132),
        'cp_pack_total_block_count': _u32(raw, 136),
        'cp_pack_start_sum': _u32(raw, 140),
        'valid_node_count': _u32(raw, 144),
        'valid_inode_count': _u32(raw, 148),
        'next_free_nid': _u32(raw, 152),
        'sit_ver_bitmap_bytesize': _u32(raw, 156),
        'nat_ver_bitmap_bytesize': _u32(raw, 160),
        'checksum_offset': _u32(raw, 164),
        'elapsed_time': _u64(raw, 168),
    }


def _ckpt_sane(ckpt, sb):
    """粗校验：块数、位图大小不合理时视为该副本损坏"""
    if not (2 <= ckpt['cp_pack_total_block_count'] <= 4 * sb['blocks_per_seg']):
        return False
    if ckpt['cp_pack_start_sum'] >= ckpt['cp_pack_total_block_count']:
        return False
    expect = ((sb['segment_count_sit'] >> 1) << sb['log_blocks_per_seg']) // 8
    if expect and ckpt['sit_ver_bitmap_bytesize'] != expect:
        return False
    if ckpt['free_segment_count'] > sb['segment_count_main']:
        return False
    return True


def _read_ckpt(fh, sb):
    """返回 (cp 块地址, 检查点 dict, 是否通过严格校验)"""
    bs = sb['block_size']
    parsed = []
    for addr in (sb['cp_blkaddr'], sb['cp_blkaddr'] + sb['blocks_per_seg']):
        try:
            ckpt = _parse_ckpt(_read_at(fh, addr * bs, CKPT_SIT_BITMAP_OFF))
        except Exception:
            continue
        parsed.append((addr, ckpt))
    if not parsed:
        raise RuntimeError("两份检查点都无法解析（可能不是 F2FS 或镜像损坏）")

    strict = [p for p in parsed if _ckpt_sane(p[1], sb)]
    addr, ckpt = max(strict or parsed,
                     key=lambda p: p[1]['checkpoint_ver'])
    return addr, ckpt, bool(strict)


# =============================================================================
# Part 3 · SIT：逐段有效块位图（含检查点里的 SIT 日志）
# =============================================================================
def _sit_journal(fh, sb, ckpt, cp_addr):
    """读出检查点中的 SIT 日志条目 [(segno, vblocks, valid_map), ...]"""
    bs = sb['block_size']
    flags = ckpt['ckpt_flags']
    if flags & CP_COMPACT_SUM_FLAG:
        # 压缩 summary：日志直接放在 cp_pack_start_sum 块内
        blk_no = cp_addr + ckpt['cp_pack_start_sum']
        joff = SUM_JOURNAL_SIZE
    else:
        # 普通 summary：SIT 日志在冷数据段 summary 块内部
        base = NR_CURSEG_TYPE if (flags & CP_UMOUNT_FLAG) else NR_CURSEG_DATA_TYPE
        blk_no = (cp_addr + ckpt['cp_pack_total_block_count']
                  - (base + 1) + CURSEG_COLD_DATA)
        joff = SUM_ENTRIES_SIZE
    if blk_no < 0:
        return []

    raw = _read_at(fh, blk_no * bs, bs)
    if len(raw) < joff + 2:
        return []
    n = _u16(raw, joff)
    if n == 0 or n > SIT_JOURNAL_ENTRIES:
        return []

    main_segs = sb['segment_count_main']
    bps = sb['blocks_per_seg']
    out = []
    p = joff + 2
    for _ in range(n):
        if p + 4 + SIT_ENTRY_SIZE > len(raw):
            return []
        segno = _u32(raw, p)
        vb = _u16(raw, p + 4) & SIT_VBLOCKS_MASK
        vmap = raw[p + 6:p + 6 + 64]
        if segno >= main_segs or vb > bps:
            return []          # 位置猜错了，宁可不叠加
        out.append((segno, vb, vmap))
        p += 4 + SIT_ENTRY_SIZE
    return out


def _read_sit(fh, sb, ckpt, cp_addr):
    """
    返回 (valid_blocks 列表, 每段 64 字节 valid_map 拼接, 日志条目数, 备注列表)
    """
    bs = sb['block_size']
    main_segs = sb['segment_count_main']
    notes = []

    sit_size = ckpt['sit_ver_bitmap_bytesize']
    if sb['cp_payload'] > 0:
        # cp_payload > 0：SIT 版本位图在 cp pack 的第二个块
        bit_addr = cp_addr * bs + bs
    else:
        bit_addr = cp_addr * bs + CKPT_SIT_BITMAP_OFF
    sit_bitmap = _read_at(fh, bit_addr, sit_size) if sit_size else b''
    if sit_size and len(sit_bitmap) < sit_size:
        notes.append("SIT 版本位图读取不完整，按第一副本处理")
        sit_bitmap = sit_bitmap.ljust(sit_size, b'\x00')

    # SIT 区被分成前后两半（各 segment_count_sit/2 段），版本位图决定用哪一半
    sit_blocks = (sb['segment_count_sit'] >> 1) << sb['log_blocks_per_seg']
    sents_per_block = max(1, bs // SIT_ENTRY_SIZE)
    n_sit_blk = _ceil_div(main_segs, sents_per_block)

    valid_blocks = [0] * main_segs
    valid_maps = bytearray(main_segs * 64)

    # 相邻 SIT 块通常落在同一半区（地址连续），批量读以减少随机 IO
    plan = [sb['sit_blkaddr'] + i
            + (sit_blocks if _bit_test_msb(sit_bitmap, i) else 0)
            for i in range(n_sit_blk)]

    idx = 0
    while idx < n_sit_blk:
        run = 1
        while (idx + run < n_sit_blk and run < 512
               and plan[idx + run] == plan[idx] + run):
            run += 1
        raw = _read_at(fh, plan[idx] * bs, run * bs)
        if len(raw) < run * bs:
            notes.append(f"SIT 块 {idx} 起读取不完整"
                         f"（设备小于超级块声明的容量？）")
            raw = raw.ljust(run * bs, b'\x00')
        for k in range(run):
            i = idx + k
            blk = raw[k * bs:(k + 1) * bs]
            for j in range(sents_per_block):
                segno = i * sents_per_block + j
                if segno >= main_segs:
                    break
                o = j * SIT_ENTRY_SIZE
                valid_blocks[segno] = _u16(blk, o) & SIT_VBLOCKS_MASK
                valid_maps[segno * 64:(segno + 1) * 64] = blk[o + 2:o + 66]
        idx += run

    # 叠加检查点里的 SIT 日志（懒写回的增量）
    journal = _sit_journal(fh, sb, ckpt, cp_addr)
    for segno, vb, vmap in journal:
        valid_blocks[segno] = vb
        valid_maps[segno * 64:(segno + 1) * 64] = vmap
    if not journal and ckpt['ckpt_flags'] & CP_COMPACT_SUM_FLAG:
        notes.append("未读到 SIT 日志（SIT 区可能未回写，段占用也许滞后）")

    return valid_blocks, valid_maps, len(journal), notes


# =============================================================================
# Part 4 · 采集
# =============================================================================
@register_collector('f2fs', 'full')
def collect_f2fs(dev):
    try:
        fh = open(dev, 'rb', buffering=0)
    except OSError as e:
        raise RuntimeError(f"无法打开设备 {dev}: {e}")

    with fh:
        raw_sb = _read_at(fh, SB_OFFSET, SB_SIZE)
        sb = _parse_sb(raw_sb)
        cp_addr, ckpt, ckpt_strict = _read_ckpt(fh, sb)
        valid_blocks, valid_maps, n_journal, notes = _read_sit(
            fh, sb, ckpt, cp_addr)

    bs = sb['block_size']
    bps = sb['blocks_per_seg']
    main_segs = sb['segment_count_main']
    main_blkaddr = sb['main_blkaddr']
    main_blocks = main_segs * bps
    segs_per_sec = sb['segs_per_sec']
    section_count = sb['section_count'] or _ceil_div(main_segs, segs_per_sec)

    # ---- 段统计 ----
    empty_segs = sum(1 for v in valid_blocks if v == 0)
    full_segs = sum(1 for v in valid_blocks if v >= bps)
    part_segs = main_segs - empty_segs - full_segs

    # ---- 行 = 若干个"区"(section)；区太多时合并，保证格子数可控 ----
    group = 1
    while _ceil_div(section_count, group) > MAX_CELLS:
        group *= 2
    row_blocks = group * segs_per_sec * bps
    nrows = max(1, _ceil_div(main_blocks, row_blocks))

    # ---- 空闲区间（一块一位，位图按 MSB-first）----
    valid_sum = sum(valid_blocks)
    used_bits, total_bits, scale = _assemble_used(valid_maps, main_segs, bps)
    if total_bits != main_blocks:
        notes.append(f"位图粒度 {total_bits:,} 位 != 主区块数 {main_blocks:,}，"
                     f"空闲区间按 {scale} 倍缩放")

    res = _scan_free_runs(used_bits, total_bits)
    binned = res is None
    if binned:
        rows, free_blocks = _binned_runs(used_bits, total_bits, row_blocks, nrows)
        notes.append(f"空闲区间过多（>{MAX_RUNS:,}），已按每行 {BINS_PER_ROW} 箱"
                     f"降级绘制；空闲块统计仍然精确")
    else:
        free_runs, free_blocks = res
        if scale != 1:
            free_runs = [(s * scale, c * scale) for s, c in free_runs]
        rows = _split_runs_into_rows(free_runs, row_blocks, main_blocks)
    if scale != 1 and binned:
        rows = [[n, [(s * scale, c * scale) for s, c in recs]] for n, recs in rows]
        free_blocks *= scale
    free_blocks = min(free_blocks, main_blocks)
    nrows = len(rows)
    n_runs = sum(len(recs) for _, recs in rows)

    rows_meta = []
    for i, (row_no, recs) in enumerate(rows):
        blocks = min(row_blocks, main_blocks - i * row_blocks)
        free = sum(c for _, c in recs)
        sec0 = i * group
        nsec = min(group, max(0, section_count - sec0))
        s0 = sec0 * segs_per_sec
        s1 = min(main_segs, s0 + nsec * segs_per_sec)
        segs = max(0, s1 - s0)
        vb = valid_blocks[s0:s1]
        rows_meta.append({
            'idx': row_no,
            'sec0': sec0,
            'nsec': nsec,
            'seg0': s0,
            'segs': segs,
            'blk0': main_blkaddr + i * row_blocks,
            'blocks': blocks,
            'free': free,
            'used': blocks - free,
            'seg_full': sum(1 for v in vb if v >= bps),
            'seg_empty': sum(1 for v in vb if v == 0),
            'seg_part': sum(1 for v in vb if 0 < v < bps),
        })

    used_pct = (main_blocks - free_blocks) / main_blocks * 100 if main_blocks else 0
    pct = free_blocks / main_blocks * 100 if main_blocks else 0
    seg_size = bps * bs

    if not ckpt_strict:
        notes.append("检查点未通过严格校验（字段可能来自旧版本或镜像有损）")

    flags = ckpt['ckpt_flags']
    unmounted = bool(flags & CP_UMOUNT_FLAG)
    verify = '一致' if valid_sum == ckpt['valid_block_count'] else (
        f"不一致（SIT 合计 {valid_sum:,} / 检查点 {ckpt['valid_block_count']:,}）")

    fields = [
        ('文件系统', 'F2FS'),
        ('卷标', sb['volume_name'] or '-'),
        ('版本', f"{sb['major_ver']}.{sb['minor_ver']}"),
        ('UUID', _fmt_uuid(sb['uuid'])),
        ('块大小', human_size(bs)),
        ('每段块数', f"{bps:,}"),
        ('每段大小', human_size(seg_size)),
        ('每区块数 (segs_per_sec)', f"{segs_per_sec}"),
        ('每区大小', human_size(row_blocks * bs) if group == 1 else
                   f"{human_size(row_blocks * bs)}（{group} 区合并）"),
        ('设备容量', human_size(sb['block_count'] * bs)),
        ('总段数', f"{sb['segment_count']:,}"),
        ('主区段数', f"{main_segs:,}"),
        ('区总数', f"{section_count:,}"),
        ('主区起始块', f"{main_blkaddr:,}"),
        ('主区块总数', f"{main_blocks:,}"),
        ('有效块 (SIT)', f"{valid_sum:,}  ({human_size(valid_sum * bs)})"),
        ('空闲块', f"{free_blocks:,}  ({human_size(free_blocks * bs)})"),
        ('空闲比例', f"{pct:.2f}%"),
        ('已用比例', f"{used_pct:.2f}%"),
        ('空段 / 部分段 / 满段', f"{empty_segs:,} / {part_segs:,} / {full_segs:,}"),
        ('检查点空闲段（不含当前段）', f"{ckpt['free_segment_count']:,}"),
        ('保留段 (GC)', f"{ckpt['rsvd_segment_count']:,}"),
        ('超额配置段', f"{ckpt['overprov_segment_count']:,}"),
        ('用户可用块', f"{ckpt['user_block_count']:,}"),
        ('有效节点 / inode', f"{ckpt['valid_node_count']:,} / "
                            f"{ckpt['valid_inode_count']:,}"),
        ('根 / 节点 / 元数据 inode', f"{sb['root_ino']} / {sb['node_ino']} / "
                                    f"{sb['meta_ino']}"),
        ('检查点版本', f"0x{ckpt['checkpoint_ver']:016X}"),
        ('卸载状态', '干净卸载（CP_UMOUNT）' if unmounted else
                    '未干净卸载（元数据可能滞后）'),
        ('SIT 日志条目', f"{n_journal}"),
        ('SIT 与检查点校验', verify),
    ]

    info_lines = [
        f"超级块 (Super Block @ 偏移 {SB_OFFSET})",
        _hexdump(raw_sb[:320]),
        "",
        f"检查点 (Checkpoint @ 块 {cp_addr})",
        f"  checkpoint_ver         0x{ckpt['checkpoint_ver']:016X}",
        f"  ckpt_flags             0x{flags:08X}",
        f"  cp_pack_total_block_count {ckpt['cp_pack_total_block_count']}",
        f"  cp_pack_start_sum      {ckpt['cp_pack_start_sum']}",
        f"  sit_ver_bitmap_bytesize {ckpt['sit_ver_bitmap_bytesize']}",
        f"  nat_ver_bitmap_bytesize {ckpt['nat_ver_bitmap_bytesize']}",
        f"  valid_block_count      {ckpt['valid_block_count']:,}",
        f"  user_block_count       {ckpt['user_block_count']:,}",
        f"  free_segment_count     {ckpt['free_segment_count']:,}",
        f"  cur_node_segno[]       "
        f"{[s for s in ckpt['cur_node_segno'] if s != 0xFFFFFFFF]}",
        f"  cur_data_segno[]       "
        f"{[s for s in ckpt['cur_data_segno'] if s != 0xFFFFFFFF]}",
        "",
        f"区域布局（块地址）",
        f"  CP    : {sb['cp_blkaddr']:>12,}   {sb['segment_count_ckpt']} 段",
        f"  SIT   : {sb['sit_blkaddr']:>12,}   {sb['segment_count_sit']} 段",
        f"  NAT   : {sb['nat_blkaddr']:>12,}   {sb['segment_count_nat']} 段",
        f"  SSA   : {sb['ssa_blkaddr']:>12,}   {sb['segment_count_ssa']} 段",
        f"  MAIN  : {main_blkaddr:>12,}   {main_segs} 段 "
        f"({human_size(main_blocks * bs)})",
        f"SIT 解析: 每块 {max(1, bs // SIT_ENTRY_SIZE)} 条目，"
        f"叠加日志 {n_journal} 条，空闲区间 {n_runs:,} 个，"
        f"行数 {nrows}（每行 {group} 区）",
    ]
    for n in notes:
        info_lines.append(f"注意: {n}")

    return {
        'dev': dev, 'fs': 'f2fs',
        'fields': fields,
        'info': '\n'.join(info_lines),
        'block_size': bs,
        'blocks_per_seg': bps,
        'main_blkaddr': main_blkaddr,
        'main_blocks': main_blocks,
        'section_count': section_count,
        'segment_count_main': main_segs,
        'segs_per_sec': segs_per_sec,
        'valid_blocks': valid_sum,
        'free_blocks': free_blocks,
        'rows': rows,
        'rows_meta': rows_meta,
        'row_size': row_blocks,
        'cells_per_row': group,
    }


# =============================================================================
# Part 5 · 区热力图（主视图：回答"全局分布"）
# =============================================================================
class _SectionHeatmap(QWidget):
    """
    一格 = 一个"区"(section，区太多时合并若干区)，
    颜色浓淡 = 使用率（绿=空 → 黄=半 → 红=满）。
    左键点击某格 → cell_clicked(行号)，用于跳转到空闲可视化对应行。
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
        self.block_size = 4096
        self._hover = None
        self._hover_pos = None

    def set_data(self, dev, rows, block_size=4096):
        self.dev = dev
        self.rows = list(rows or [])
        self.block_size = int(block_size or 4096)
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
        length = r.get('blocks') or 0
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
        blocks = r.get('blocks') or 0
        free = r.get('free') or 0
        pct = free / blocks * 100 if blocks else 0
        return {
            'title': f"区 {r.get('sec0')} 起   ·   空闲 {pct:.2f}%",
            'lines': [
                ('起始块', f"{r.get('blk0', 0):,}"),
                ('块数',   f"{blocks:,}   "
                          f"({human_size(blocks * self.block_size)})"),
                ('有效块', f"{r.get('used', 0):,}"),
                ('空闲块', f"{free:,}"),
                ('段数',   f"{r.get('segs', 0):,}"),
                ('满 / 部分 / 空段', f"{r.get('seg_full', 0)} / "
                                    f"{r.get('seg_part', 0)} / "
                                    f"{r.get('seg_empty', 0)}"),
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
# Part 6 · Tab 集合
# =============================================================================
@register_tab
class F2fsOverviewTab(TabPlugin):
    TAB_FS = 'f2fs'
    TAB_ORDER = 10
    TAB_ID = 'overview'
    TAB_TITLE = '概览'
    NEEDS = ('full',)
    EMPTY_TEXT = '请在起始页选择一个 F2FS 设备'

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

        gb_info = QGroupBox("超级块 / 检查点 / 布局明细")
        li = QVBoxLayout(gb_info)
        self.info_text = QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setFont(QFont("Monospace", 10))
        self.info_text.setMinimumHeight(320)
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
class F2fsSectionTableTab(TabPlugin):
    """区分布：每一"区"（一个或多个 section）的段/块统计，点击跳到可视化"""

    TAB_FS = 'f2fs'
    TAB_ORDER = 20
    TAB_ID = 'sections'
    TAB_TITLE = '区分布'
    NEEDS = ('full',)

    group_activated = Signal(int)

    COLS = ['区', '起始段', '段数', '起始块', '块数',
            '有效块', '空闲块', '空闲%', '满/部分/空段']

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
        meta = payload.get('rows_meta', [])
        self.table.setRowCount(len(meta))
        for i, m in enumerate(meta):
            blocks = int(m.get('blocks') or 0)
            free = int(m.get('free') or 0)
            pct = free / blocks * 100 if blocks else 0
            vals = [
                (f"区 {m.get('sec0')}" if int(m.get('nsec') or 1) == 1
                 else f"区 {m.get('sec0')}-"
                      f"{int(m.get('sec0')) + int(m.get('nsec')) - 1}"),
                f"{m.get('seg0', 0):,}",
                f"{m.get('segs', 0):,}",
                f"{m.get('blk0', 0):,}",
                f"{blocks:,}",
                f"{int(m.get('used') or 0):,}",
                f"{free:,}",
                f"{pct:.2f}%",
                f"{m.get('seg_full', 0)} / {m.get('seg_part', 0)} / "
                f"{m.get('seg_empty', 0)}",
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, c, item)

    def clear(self):
        self.table.setRowCount(0)


@register_tab
class F2fsHeatmapTab(TabPlugin):
    """主视图：区热力图，回答"全局分布"（哪片区域空、哪片区域满）"""

    TAB_FS = 'f2fs'
    TAB_ORDER = 30
    TAB_ID = 'heatmap'
    TAB_TITLE = '区热力图'
    NEEDS = ('full',)
    EMPTY_TEXT = '请在起始页选择一个 F2FS 设备'

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
        self.heat = _SectionHeatmap(self.EMPTY_TEXT)
        self.heat.cell_clicked.connect(self.group_activated)
        self.scroll.setWidget(self.heat)
        layout.addWidget(self.scroll)

    def update_data(self, payloads):
        payload = payloads.get('full')
        if not payload:
            return
        meta = payload.get('rows_meta', [])
        main_blocks = int(payload.get('main_blocks') or 0)
        free = int(payload.get('free_blocks') or 0)
        pct = free / main_blocks * 100 if main_blocks else 0
        self.title.setText(
            f"设备: {payload.get('dev')}　·　{len(meta)} 格　·　"
            f"总空闲 {pct:.2f}%　·　"
            + ("每格 = 1 个区　　" if int(payload.get('cells_per_row') or 1) == 1
               else f"每格 = {int(payload.get('cells_per_row'))} 个区　　")
            + self.LEGEND)
        self.heat.set_data(payload.get('dev'), meta,
                           int(payload.get('block_size') or 4096))

    def clear(self):
        self.title.setText(self.EMPTY_TEXT)
        self.heat.clear()


@register_tab
class F2fsPlotTab(TabPlugin):
    TAB_FS = 'f2fs'
    TAB_ORDER = 40
    TAB_ID = 'plot'
    TAB_TITLE = '空闲可视化'
    NEEDS = ('full',)
    EMPTY_TEXT = '请在起始页选择一个 F2FS 设备'
    ROW_PREFIX = '区'

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        grid = QGridLayout()
        grid.setSpacing(0)

        self.chart = BlockRangeChart(row_prefix=self.ROW_PREFIX,
                                     axis_label='块号 (block)',
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
                            int(payload.get('block_size') or 4096))

    def focus_group(self, row_index):
        return self.chart.focus_row(row_index)

    def clear(self):
        self.chart.clear()


# =============================================================================
# Part 7 · 包描述
# =============================================================================
@register_pack
class F2fsPack(FSPack):
    FS_ID = 'f2fs'
    FS_NAME = 'F2FS'
    ORDER = 26

    @classmethod
    def matches_fstype(cls, fstype):
        return (fstype or '').lower() == 'f2fs'