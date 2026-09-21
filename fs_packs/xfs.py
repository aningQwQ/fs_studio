#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fs_packs.xfs — XFS 包

自包含一个文件系统所需的全部内容:
    collector : collect_full()   （root 侧，xfs_db / xfs_info 采集）
    tabs      : 概览 / AG 结构 / 空闲可视化

带走这个包 = 复制本文件。
"""
import re
import shutil

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QFrame, QGroupBox, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QPlainTextEdit, QScrollArea,
                               QScrollBar, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .core import (BlockRangeChart, FSPack, TabPlugin, kv_parse,
                   register_collector, register_pack, register_tab, run_cmd)


# =============================================================================
# Part 1 · Collector（root 侧）
# =============================================================================
@register_collector('xfs', 'full')
def collect_full(dev):
    if shutil.which('xfs_db') is None:
        raise RuntimeError("未安装 xfs_db（请 dnf install xfsprogs）")

    info = run_cmd(['xfs_info', dev]).stdout
    sb = kv_parse(run_cmd(['xfs_db', '-r', '-c', 'sb 0', '-c', 'p', dev]).stdout)
    try:
        agcount = int(sb.get('agcount', '0'))
    except ValueError:
        agcount = 0

    ags = []
    for ag in range(agcount):
        agf = kv_parse(run_cmd(['xfs_db', '-r', '-c', f'agf {ag}',
                                '-c', 'p', dev]).stdout)
        agi = kv_parse(run_cmd(['xfs_db', '-r', '-c', f'agi {ag}',
                                '-c', 'p', dev]).stdout)
        r = run_cmd(['xfs_db', '-r', '-c', f'agf {ag}', '-c', 'addr bnoroot',
                     '-c', 'btdump', dev])
        recs = [[int(m.group(1)), int(m.group(2))]
                for m in re.finditer(r'\[\s*(\d+)\s*,\s*(\d+)\s*\]',
                                     r.stdout + r.stderr)]
        ags.append({'ag': ag, 'agf': agf, 'agi': agi, 'bnobt': recs})

    return {'dev': dev, 'fs': 'xfs', 'info': info, 'sb': sb, 'ags': ags}


# =============================================================================
# Part 2 · Tab 集合
# =============================================================================
# --------------------------- 概览 ---------------------------
@register_tab
class XfsOverviewTab(TabPlugin):
    TAB_FS = "xfs"
    TAB_ID = "overview"
    TAB_TITLE = "概览"
    TAB_ORDER = 10
    NEEDS = ('full',)

    SB_FIELDS = [
        ('magicnum',   'Magic'),
        ('versionnum', 'Version'),
        ('blocksize',  '块大小'),
        ('sectsize',   '扇区大小'),
        ('inodesize',  'inode 大小'),
        ('agcount',    'AG 数量'),
        ('agblocks',   '每 AG 块数'),
        ('dblocks',    '数据块总数'),
        ('logblocks',  '日志块数'),
        ('icount',     'inode 总数'),
        ('ifree',      '空闲 inode'),
        ('fdblocks',   '空闲数据块'),
        ('uuid',       'UUID'),
        ('fname',      '卷标'),
    ]

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)

        self.title = QLabel("请在起始页选择一个 XFS 设备")
        self.title.setStyleSheet(
            "font-size:16px; font-weight:bold; padding:6px;")
        layout.addWidget(self.title)

        gb_sb = QGroupBox("超级块 (Superblock)")
        g = QGridLayout(gb_sb)
        self.sb_labels = {}
        for i, (k, label) in enumerate(self.SB_FIELDS):
            g.addWidget(QLabel(f"{label}:"), i, 0)
            v = QLabel("-")
            v.setStyleSheet("font-family: monospace;")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setWordWrap(True)
            g.addWidget(v, i, 1)
            self.sb_labels[k] = v
        layout.addWidget(gb_sb)

        gb_info = QGroupBox("xfs_info 输出")
        li = QVBoxLayout(gb_info)
        self.info_text = QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setFont(QFont("Monospace", 10))
        self.info_text.setMinimumHeight(80)
        li.addWidget(self.info_text)
        layout.addWidget(gb_info)

        scroll.setWidget(content)
        outer.addWidget(scroll)

    def update_data(self, payloads):
        payload = payloads.get('full')
        if not payload:
            return
        sb = payload.get('sb', {})
        self.title.setText(f"设备: {payload.get('dev')}")
        for k, _ in self.SB_FIELDS:
            self.sb_labels[k].setText(sb.get(k, '-'))
        self.info_text.setPlainText(payload.get('info', ''))

    def clear(self):
        self.title.setText("请在起始页选择一个 XFS 设备")
        for v in self.sb_labels.values():
            v.setText('-')
        self.info_text.clear()


# --------------------------- AG 结构 ---------------------------
@register_tab
class XfsAgTab(TabPlugin):
    TAB_FS = "xfs"
    TAB_ID = "ag"
    TAB_TITLE = "AG 结构"
    TAB_ORDER = 20
    NEEDS = ('full',)

    COLS = ['AG', '长度(块)', '空闲块', '空闲%',
            'inode 总数', '空闲 inode', 'inode 使用率', 'bnobt 段数']

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
        ags = payload.get('ags', [])
        self.table.setRowCount(len(ags))
        for row, ag in enumerate(ags):
            agf = ag.get('agf', {})
            agi = ag.get('agi', {})
            bnobt = ag.get('bnobt', [])

            try:
                length = int(agf.get('length', '0'))
            except ValueError:
                length = 0
            free = sum(c for _, c in bnobt)
            freepct = free / length * 100 if length else 0

            try:
                icount = int(agi.get('count', '0'))
                ifree = int(agi.get('freecount', '0'))
            except ValueError:
                icount = ifree = 0
            ipct = (icount - ifree) / icount * 100 if icount else 0

            vals = [
                f"AG {ag['ag']}",
                f"{length:,}",
                f"{free:,}",
                f"{freepct:.2f}%",
                f"{icount:,}",
                f"{ifree:,}",
                f"{ipct:.2f}%",
                f"{len(bnobt)}",
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, c, item)

    def clear(self):
        self.table.setRowCount(0)


# --------------------------- 空闲可视化 ---------------------------
@register_tab
class XfsPlotTab(TabPlugin):
    TAB_FS = "xfs"
    TAB_ID = "plot"
    TAB_TITLE = "空闲可视化"
    TAB_ORDER = 30
    NEEDS = ('full',)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        grid = QGridLayout()
        grid.setSpacing(0)

        self.chart = BlockRangeChart(row_prefix='AG',
                                     axis_label='块号 (block)',
                                     empty_text="请在起始页选择一个 XFS 设备")
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
            row_size = int(sb.get('agblocks', '1'))
        except ValueError:
            row_size = 1
        try:
            block_size = int(sb.get('blocksize', '4096'))
        except ValueError:
            block_size = 4096
        rows = [(a['ag'], [tuple(r) for r in a.get('bnobt', [])])
                for a in payload.get('ags', [])]
        self.chart.set_data(payload.get('dev'), rows, row_size, block_size)

    def clear(self):
        self.chart.clear()


# =============================================================================
# Part 3 · 包描述
# =============================================================================
@register_pack
class XfsPack(FSPack):
    FS_ID = "xfs"
    FS_NAME = "XFS"
    ORDER = 10
