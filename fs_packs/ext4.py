#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fs_packs.ext4 — ext2/3/4 包

自包含一个文件系统所需的全部内容:
    collector : collect_full()   （root 侧，dumpe2fs / e2freefrag 采集）
    tabs      : 概览 / 块组结构 / 块组热力图（主视图）/ 空闲区间

带走这个包 = 复制本文件。
"""
import re
import shutil

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QGroupBox, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QPlainTextEdit, QScrollArea,
                               QScrollBar, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .core import (BlockRangeChart, FSPack, TabPlugin, human_size, kv_parse,
                   register_collector, register_pack, register_tab, run_cmd)

GROUP_HDR = re.compile(r'^Group\s+(\d+):\s*\(Blocks\s+(\d+)-(\d+)\)')
FREE_COUNT = re.compile(r'(\d+)\s+free blocks,\s*(\d+)\s+free inodes')


def _parse_ranges(text, out):
    for m in re.finditer(r'(\d+)(?:\s*-\s*(\d+))?', text):
        a = int(m.group(1))
        if m.group(2) is None:
            out.append([a, 1])
        else:
            b = int(m.group(2))
            out.append([a, b - a + 1])
    return out


def _parse_groups(text):
    """从 dumpe2fs 完整输出解析每个块组的空闲信息与空闲区间"""
    groups = []
    cur = None
    cur_free = []
    collecting = False

    def flush():
        if cur is not None:
            cur['free_ranges'] = cur_free
            groups.append(cur)

    for raw in text.splitlines():
        line = raw.rstrip()
        m = GROUP_HDR.match(line)
        if m:
            flush()
            start, end = int(m.group(2)), int(m.group(3))
            cur = {'group': int(m.group(1)), 'start': start, 'end': end,
                   'length': end - start + 1,
                   'free_blocks': 0, 'free_inodes': 0}
            cur_free = []
            collecting = False
            continue
        if cur is None:
            continue

        s = line.strip()
        mc = FREE_COUNT.search(s)
        if mc:
            cur['free_blocks'] = int(mc.group(1))
            cur['free_inodes'] = int(mc.group(2))
            continue
        if s.startswith('Free blocks:'):
            cur_free = _parse_ranges(s[len('Free blocks:'):], cur_free)
            collecting = True
            continue
        if s.startswith('Free inodes:'):
            collecting = False
            continue
        if collecting:
            if re.match(r'^\d', s):
                cur_free = _parse_ranges(s, cur_free)
            else:
                collecting = False

    flush()
    return groups


# =============================================================================
# Part 1 · Collector（root 侧）
# =============================================================================
@register_collector('ext4', 'full')
def collect_full(dev):
    if shutil.which('dumpe2fs') is None:
        raise RuntimeError("未安装 dumpe2fs（请 dnf install e2fsprogs）")

    sb = kv_parse(run_cmd(['dumpe2fs', '-h', dev]).stdout)
    full = run_cmd(['dumpe2fs', dev])

    frag = ''
    if shutil.which('e2freefrag'):
        frag = run_cmd(['e2freefrag', dev]).stdout

    groups = _parse_groups(full.stdout + full.stderr)
    if not groups:
        raise RuntimeError("无法解析块组信息，请确认设备是有效的 ext2/3/4")

    return {'dev': dev, 'fs': 'ext4', 'sb': sb,
            'info': run_cmd(['dumpe2fs', '-h', dev]).stdout,
            'frag': frag, 'groups': groups}


# =============================================================================
# Part 1.5 · 块组热力图（主视图：回答"全局分布"）
# =============================================================================
class BlockGroupHeatmap(QWidget):
    """
    把等大小、连续编号的块组排成矩阵：一格 = 一个块组，颜色浓淡 = 使用率。
    固定格尺寸（8~14px），根据控件宽高自动算行列；一屏可容纳数千格。
    颜色：绿=空，黄=半，红=满（单色深浅亦可）。
    左键点击某格 → group_clicked(块组号)，用于跳转到该组的区间视图。
    """
    group_clicked = Signal(int)

    MARGIN = 10
    GAP = 3

    C_BG    = QColor('#ffffff')
    C_EMPTY = QColor('#22c55e')   # 全空
    C_HALF  = QColor('#eab308')   # 半满
    C_FULL  = QColor('#ef4444')   # 全满
    C_DIM   = QColor('#666666')
    C_HOVER = QColor('#111111')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setStyleSheet("background: white;")

        self.dev = None
        self.groups = []
        self.cell = 11
        self.block_size = 4096
        self._hover = None
        self._hover_pos = None

    # ---------------- 数据 / 尺寸 ----------------
    def set_data(self, dev, groups, block_size=4096):
        self.dev = dev
        self.groups = list(groups or [])
        self.block_size = int(block_size or 4096)
        self._hover = None
        self._hover_pos = None
        self._relayout()
        self.update()

    def clear(self):
        self.dev = None
        self.groups = []
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
        if not self.groups:
            return 0
        n = len(self.groups)
        return (n + self._cols() - 1) // self._cols()

    def _needed_height(self):
        if not self.groups:
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
    def _used_ratio(self, g):
        length = g.get('length') or 0
        free = g.get('free_blocks') or 0
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

        if not self.groups:
            p.setPen(self.C_DIM)
            f = QFont(); f.setPointSizeF(12); p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter,
                       "请在起始页选择一个 ext4 设备")
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
                if i >= len(self.groups):
                    break
                p.setBrush(self._color_for(self._used_ratio(self.groups[i])))
                p.drawRect(self.MARGIN + c * pitch, y, self.cell, self.cell)

        if self._hover is not None and 0 <= self._hover < len(self.groups):
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
        g = self.groups[self._hover]
        length = g.get('length') or 0
        free = g.get('free_blocks') or 0
        pct = free / length * 100 if length else 0
        start = g.get('start', 0)
        return {
            'title': f"块组 {g.get('group')}   ·   空闲 {pct:.2f}%",
            'lines': [
                ('起始块', f"{start:,}"),
                ('长度',   f"{length:,} 块   ({human_size(length * self.block_size)})"),
                ('空闲块', f"{free:,}"),
                ('空闲 inode', f"{g.get('free_inodes', 0):,}"),
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
        if not self.groups:
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
        return i if 0 <= i < len(self.groups) else None

    def mouseMoveEvent(self, event):
        self._hover_pos = event.position()
        self._hover = self._index_at(event.position())
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            i = self._index_at(event.position())
            if i is not None:
                self._hover = i
                self.group_clicked.emit(int(self.groups[i].get('group', i)))
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
# Part 2 · Tab 集合
# =============================================================================
SB_FIELDS = [
    ('Filesystem volume name', '卷标'),
    ('Filesystem UUID',        'UUID'),
    ('Filesystem magic number', 'Magic'),
    ('Filesystem features',    '特性'),
    ('Filesystem state',       '状态'),
    ('Inode count',            'inode 总数'),
    ('Block count',            '块总数'),
    ('Reserved block count',   '保留块'),
    ('Free blocks',            '空闲块'),
    ('Free inodes',            '空闲 inode'),
    ('Block size',             '块大小'),
    ('Inode size',             'inode 大小'),
    ('Blocks per group',       '每块组块数'),
    ('Inodes per group',       '每块组 inode'),
    ('Journal inode',          '日志 inode'),
    ('Filesystem created',     '创建时间'),
]


@register_tab
class Ext4OverviewTab(TabPlugin):
    TAB_FS = "ext4"
    TAB_ID = "overview"
    TAB_TITLE = "概览"
    TAB_ORDER = 10
    NEEDS = ('full',)

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)

        self.title = QLabel("请在起始页选择一个 ext4 设备")
        self.title.setStyleSheet(
            "font-size:16px; font-weight:bold; padding:6px;")
        layout.addWidget(self.title)

        gb_sb = QGroupBox("超级块 (Superblock)")
        g = QGridLayout(gb_sb)
        self.sb_labels = {}
        for i, (k, label) in enumerate(SB_FIELDS):
            g.addWidget(QLabel(f"{label}:"), i, 0)
            v = QLabel("-")
            v.setStyleSheet("font-family: monospace;")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setWordWrap(True)
            g.addWidget(v, i, 1)
            self.sb_labels[k] = v
        layout.addWidget(gb_sb)

        gb_info = QGroupBox("dumpe2fs -h 输出")
        li = QVBoxLayout(gb_info)
        self.info_text = QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setFont(QFont("Monospace", 10))
        self.info_text.setMinimumHeight(80)
        li.addWidget(self.info_text)
        layout.addWidget(gb_info)

        gb_frag = QGroupBox("e2freefrag 空闲碎片")
        lf = QVBoxLayout(gb_frag)
        self.frag_text = QPlainTextEdit()
        self.frag_text.setReadOnly(True)
        self.frag_text.setFont(QFont("Monospace", 10))
        self.frag_text.setMinimumHeight(80)
        lf.addWidget(self.frag_text)
        layout.addWidget(gb_frag)

        scroll.setWidget(content)
        outer.addWidget(scroll)

    def update_data(self, payloads):
        payload = payloads.get('full')
        if not payload:
            return
        sb = payload.get('sb', {})
        self.title.setText(f"设备: {payload.get('dev')}")
        for k, _ in SB_FIELDS:
            self.sb_labels[k].setText(sb.get(k, '-'))
        self.info_text.setPlainText(payload.get('info', ''))
        self.frag_text.setPlainText(payload.get('frag', ''))

    def clear(self):
        self.title.setText("请在起始页选择一个 ext4 设备")
        for v in self.sb_labels.values():
            v.setText('-')
        self.info_text.clear()
        self.frag_text.clear()


@register_tab
class Ext4GroupTab(TabPlugin):
    TAB_FS = "ext4"
    TAB_ID = "group"
    TAB_TITLE = "块组结构"
    TAB_ORDER = 20
    NEEDS = ('full',)

    COLS = ['块组', '起始块', '长度(块)', '空闲块', '空闲%',
            '空闲 inode', 'inode 使用率', '空闲段数']

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

    def update_data(self, payloads):
        payload = payloads.get('full')
        if not payload:
            return
        try:
            inodes_per_group = int(payload.get('sb', {})
                                   .get('Inodes per group', '0'))
        except ValueError:
            inodes_per_group = 0

        groups = payload.get('groups', [])
        self.table.setRowCount(len(groups))
        for row, g in enumerate(groups):
            length = g.get('length', 0)
            free = g.get('free_blocks', 0)
            fip = g.get('free_inodes', 0)
            freepct = free / length * 100 if length else 0
            ipct = ((inodes_per_group - fip) / inodes_per_group * 100
                    if inodes_per_group else 0)
            vals = [
                f"组 {g.get('group')}",
                f"{g.get('start', 0):,}",
                f"{length:,}",
                f"{free:,}",
                f"{freepct:.2f}%",
                f"{fip:,}",
                f"{ipct:.2f}%",
                f"{len(g.get('free_ranges', []))}",
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, c, item)

    def clear(self):
        self.table.setRowCount(0)


@register_tab
class Ext4HeatmapTab(TabPlugin):
    """主视图：块组热力图，回答"全局分布"（哪片区域空、哪片区域满）"""
    TAB_FS = "ext4"
    TAB_ID = "heatmap"
    TAB_TITLE = "块组热力图"
    TAB_ORDER = 30
    NEEDS = ('full',)

    group_activated = Signal(int)   # 点击某块组 → 跳转到区间视图

    LEGEND = ("<span style='color:#22c55e'>■</span> 空　"
              "<span style='color:#eab308'>■</span> 半　"
              "<span style='color:#ef4444'>■</span> 满　"
              "<span style='color:#999'>（Ctrl+滚轮 调格子大小）</span>")

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        self.title = QLabel("请在起始页选择一个 ext4 设备")
        self.title.setTextFormat(Qt.RichText)
        self.title.setStyleSheet(
            "font-size:14px; font-weight:bold; padding:6px;")
        layout.addWidget(self.title)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.heat = BlockGroupHeatmap()
        self.heat.group_clicked.connect(self.group_activated)
        self.scroll.setWidget(self.heat)
        layout.addWidget(self.scroll)

    def update_data(self, payloads):
        payload = payloads.get('full')
        if not payload:
            return
        sb = payload.get('sb', {})
        groups = payload.get('groups', [])
        try:
            block_size = int(sb.get('Block size', '4096'))
        except ValueError:
            block_size = 4096

        total_len = sum(g.get('length', 0) for g in groups)
        total_free = sum(g.get('free_blocks', 0) for g in groups)
        pct = total_free / total_len * 100 if total_len else 0

        self.title.setText(
            f"设备: {payload.get('dev')}　·　{len(groups)} 个块组　·　"
            f"总空闲 {pct:.2f}%　·　每格 = 1 个块组　　{self.LEGEND}")
        self.heat.set_data(payload.get('dev'), groups, block_size)

    def clear(self):
        self.title.setText("请在起始页选择一个 ext4 设备")
        self.heat.clear()


@register_tab
class Ext4PlotTab(TabPlugin):
    TAB_FS = "ext4"
    TAB_ID = "plot"
    TAB_TITLE = "空闲区间"
    TAB_ORDER = 40
    NEEDS = ('full',)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        grid = QGridLayout()
        grid.setSpacing(0)

        self.chart = BlockRangeChart(row_prefix='组',
                                     axis_label='块号 (block)',
                                     empty_text="请在起始页选择一个 ext4 设备")
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
        sb = payload.get('sb', {})
        try:
            row_size = int(sb.get('Blocks per group', '1'))
        except ValueError:
            row_size = 1
        try:
            block_size = int(sb.get('Block size', '4096'))
        except ValueError:
            block_size = 4096
        # dumpe2fs 给出的是绝对块号，换算成组内偏移供图表使用
        rows = [(g.get('group'),
                 [[r[0] - g.get('start', 0), r[1]]
                  for r in g.get('free_ranges', [])])
                for g in payload.get('groups', [])]
        self.chart.set_data(payload.get('dev'), rows, row_size, block_size)

    def focus_group(self, group_no):
        """定位到指定块组所在行（供热力图点击跳转）"""
        for i, (n, _) in enumerate(self.chart.rows):
            if n == group_no:
                return self.chart.focus_row(i)
        return False

    def clear(self):
        self.chart.clear()


# =============================================================================
# Part 3 · 包描述
# =============================================================================
@register_pack
class Ext4Pack(FSPack):
    FS_ID = "ext4"
    FS_NAME = "ext2/3/4"
    ORDER = 20

    @classmethod
    def matches_fstype(cls, fstype):
        return fstype in ('ext4', 'ext3', 'ext2')
