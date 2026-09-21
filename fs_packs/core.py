#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fs_packs.core — FS 包框架（主窗口与文件系统之间唯一的约定层）

设计目标:
    * 每个文件系统一个 "包"(FSPack)，包里自带 collector + Tab 集合；
    * 主窗口不认 FS，只认 "设备 + 包"；
    * 起始页列出所有已知 FS 的分区，选择后按磁盘格式自动匹配包，
      加载该包的 Tab 集合并采集。

一个 FS 包需要提供:
    FS_ID / FS_NAME / ORDER
    matches_fstype(fstype) -> bool     判断磁盘格式
    collectors                          {action: fn(dev)->dict}
    tabs                                TAB_FS == FS_ID 的 @register_tab 类
"""
import importlib
import json
import math
import os
import pkgutil
import shutil
import subprocess
import sys

from PySide6.QtCore import Qt, QPointF, QRectF, QThread, Signal
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QAction
from PySide6.QtWidgets import QWidget, QMenu, QScrollBar


# =============================================================================
# Part 1 · 通用工具
# =============================================================================
def kv_parse(text):
    """解析 "key = value" / "key: value" 形式的输出为 dict"""
    d = {}
    for line in text.splitlines():
        for sep in ('=', ':'):
            if sep in line:
                k, v = line.split(sep, 1)
                d[k.strip()] = v.strip()
                break
    return d


def _c_env():
    """强制 C locale，保证各 FS 工具输出可稳定解析（不随系统语言变化）"""
    env = dict(os.environ)
    env['LC_ALL'] = 'C'
    env['LANG'] = 'C'
    return env


def run_cmd(cmd, timeout=300):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=_c_env())
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, '', str(e))


def human_size(n):
    n = float(n)
    for u in ('B', 'K', 'M', 'G', 'T', 'P'):
        if abs(n) < 1024:
            return f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} E"


def nice_step(raw):
    if raw <= 0:
        return 1
    exp = math.floor(math.log10(raw))
    base = raw / (10 ** exp)
    if base < 1.5:
        nice = 1
    elif base < 3:
        nice = 2
    elif base < 7:
        nice = 5
    else:
        nice = 10
    return nice * (10 ** exp)


def fmt_tick(v):
    a = abs(v)
    if a >= 1e12: return f"{v/1e12:.1f}T"
    if a >= 1e9:  return f"{v/1e9:.1f}G"
    if a >= 1e6:  return f"{v/1e6:.1f}M"
    if a >= 1e3:  return f"{v/1e3:.0f}K"
    return f"{v:.0f}"


def read_diskstats(name):
    try:
        with open('/proc/diskstats') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 14 and parts[2] == name:
                    return {
                        'reads': int(parts[3]),
                        'sectors_read': int(parts[5]),
                        'ms_reading': int(parts[6]),
                        'writes': int(parts[7]),
                        'sectors_written': int(parts[9]),
                        'ms_writing': int(parts[10]),
                        'ios_in_progress': int(parts[11]),
                        'ms_io': int(parts[12]),
                    }
    except Exception:
        pass
    return None


def list_block_devices():
    """列出所有带文件系统的块设备（整盘 + 分区），与具体 FS 无关"""
    try:
        out = subprocess.run(
            ['lsblk', '-o', 'PATH,FSTYPE,UUID,LABEL,MOUNTPOINT,SIZE', '-J'],
            capture_output=True, text=True).stdout
        tree = json.loads(out)
    except Exception:
        return []

    devs = []

    def walk(node):
        if node.get('fstype') and node.get('path'):
            devs.append({
                'dev': node['path'],
                'fstype': node.get('fstype') or '',
                'uuid': node.get('uuid') or '-',
                'label': node.get('label') or '-',
                'mount': node.get('mountpoint') or '-',
                'size': node.get('size') or '-',
            })
        for c in node.get('children', []):
            walk(c)

    for d in tree.get('blockdevices', []):
        walk(d)
    return devs


# =============================================================================
# Part 2 · 注册表：包 / Tab / collector
# =============================================================================
_PACKS = []
_TAB_REGISTRY = []
_COLLECTOR_REGISTRY = {}      # fs_id -> {action: fn}
_COLLECTOR_ORDER = {}         # fs_id -> [action, ...]
_PACKS_LOADED = False


def register_pack(cls):
    """FS 包注册装饰器"""
    if cls not in _PACKS:
        _PACKS.append(cls)
    return cls


def register_tab(cls):
    """Tab 插件注册装饰器：TAB_FS 决定它属于哪个包"""
    if cls not in _TAB_REGISTRY:
        _TAB_REGISTRY.append(cls)
    return cls


def register_collector(fs_id, action):
    """采集动作注册装饰器：@register_collector('xfs', 'full')"""
    def deco(fn):
        _COLLECTOR_REGISTRY.setdefault(fs_id, {})[action] = fn
        order = _COLLECTOR_ORDER.setdefault(fs_id, [])
        if action not in order:
            order.append(action)
        return fn
    return deco


def collector_for(fs_id, action):
    return _COLLECTOR_REGISTRY.get(fs_id, {}).get(action)


def actions_for(fs_id):
    return list(_COLLECTOR_ORDER.get(fs_id, []))


def tabs_for(fs_id):
    return sorted([c for c in _TAB_REGISTRY
                   if getattr(c, 'TAB_FS', '') == fs_id],
                  key=lambda c: getattr(c, 'TAB_ORDER', 100))


def all_packs():
    return sorted(_PACKS, key=lambda c: getattr(c, 'ORDER', 100))


def find_pack_by_id(fs_id):
    for p in all_packs():
        if p.FS_ID == fs_id:
            return p
    return None


def find_pack_for_fstype(fstype):
    """按磁盘格式匹配包（磁盘格式自动识别的核心入口）"""
    if not fstype:
        return None
    for p in all_packs():
        try:
            if p.matches_fstype(fstype):
                return p
        except Exception:
            pass
    return None


def load_packs():
    """自动发现并导入 fs_packs/ 下的所有 FS 包模块"""
    global _PACKS_LOADED
    if _PACKS_LOADED:
        return
    pkg_name = __package__ or 'fs_packs'
    pkg = sys.modules.get(pkg_name)
    if pkg is None or not hasattr(pkg, '__path__'):
        _PACKS_LOADED = True
        return
    for m in pkgutil.iter_modules(pkg.__path__):
        if m.name in ('core', '__init__'):
            continue
        try:
            importlib.import_module(f"{pkg_name}.{m.name}")
        except Exception as e:
            print(f"加载 FS 包 {m.name} 失败: {e}", file=sys.stderr)
    _PACKS_LOADED = True


class TabPlugin(QWidget):
    """
    Tab 插件基类。子类需声明：
        TAB_FS    所属 FS 包（如 'xfs'）
        TAB_ID    唯一标识
        TAB_TITLE 显示名
        TAB_ORDER 排序权重（越小越靠前）
        NEEDS     需要的采集动作元组，例如 ('full',)
    并实现:
        update_data(payloads)   payloads = {动作名: 结果dict, ...}
        clear()
    """
    TAB_FS = ""
    TAB_ID = ""
    TAB_TITLE = ""
    TAB_ORDER = 100
    NEEDS = ('full',)

    def update_data(self, payloads):
        pass

    def clear(self):
        pass


class FSPack:
    """FS 包基类。主窗口只通过这个接口与文件系统交互。"""
    FS_ID = ""
    FS_NAME = ""
    ORDER = 100
    ICON = ""

    @classmethod
    def matches_fstype(cls, fstype):
        return bool(fstype) and fstype == cls.FS_ID

    @classmethod
    def collectors(cls):
        return {a: collector_for(cls.FS_ID, a) for a in actions_for(cls.FS_ID)}

    @classmethod
    def tabs(cls):
        return tabs_for(cls.FS_ID)


# =============================================================================
# Part 3 · 通用块范围图（AG / 块组 空闲可视化共用）
# =============================================================================
class BlockRangeChart(QWidget):
    ML, MR, MT, MB = 80, 110, 52, 48
    ROW_HEIGHT = 44

    C_BG      = QColor('#ffffff')
    C_ROW_ALT = QColor('#fafafa')
    C_USED    = QColor('#3b82f6')
    C_FREE    = QColor('#e3e3e3')
    C_FREE_BD = QColor('#cfcfcf')
    C_TEXT    = QColor('#1a1a1a')
    C_DIM     = QColor('#666666')
    C_EDGE    = QColor('#999999')

    def __init__(self, row_prefix='AG', axis_label='块号 (block)',
                 empty_text='请选择一个设备', parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(400, 300)
        self.setStyleSheet("background: white;")

        self.row_prefix = row_prefix
        self.axis_label = axis_label
        self.empty_text = empty_text

        self.dev = None
        self.rows = []
        self.row_size = 1
        self.block_size = 4096
        self.focus_index = None

        self.view_start = 0.0
        self.view_end = 1.0
        self.v_offset = 0.0

        self._drag_pos = None
        self._drag_view = None
        self._hover_pos = None
        self._hover_info = None

        self.vbar = None
        self.hbar = None

    def attach_scrollbars(self, vbar: QScrollBar, hbar: QScrollBar):
        self.vbar = vbar
        self.hbar = hbar
        vbar.valueChanged.connect(self._on_v_scroll)
        hbar.valueChanged.connect(self._on_h_scroll)
        self._sync_scrollbars()

    def set_data(self, dev, rows, row_size, block_size=4096):
        """rows: [(行号, [(起始块, 块数), ...]), ...]"""
        self.dev = dev
        self.rows = [(n, [tuple(r) for r in recs]) for n, recs in rows]
        self.row_size = max(1, int(row_size or 1))
        self.block_size = int(block_size or 4096)
        self.focus_index = None
        self.view_start = 0.0
        self.view_end = float(self.row_size)
        self.v_offset = 0.0
        self._hover_pos = None
        self._hover_info = None
        self._sync_scrollbars()
        self.update()

    def clear(self):
        self.dev = None
        self.rows = []
        self.focus_index = None
        self.v_offset = 0.0
        self._hover_pos = None
        self._hover_info = None
        self._sync_scrollbars()
        self.update()

    def focus_row(self, index):
        """滚动到指定行并整行复位缩放，同时高亮该行"""
        if not (0 <= index < len(self.rows)):
            return False
        self.focus_index = index
        dh = max(1, self.height() - self.MT - self.MB)
        self.v_offset = max(0.0, index * self.ROW_HEIGHT
                            - (dh - self.ROW_HEIGHT) / 2)
        self.view_start = 0.0
        self.view_end = float(self.row_size)
        self._sync_scrollbars()
        self.update()
        return True

    def _layout(self, W, H):
        return (self.ML, self.MT,
                max(1, W - self.ML - self.MR),
                max(1, H - self.MT - self.MB))

    def _x_to_px(self, x, W, H):
        dx, _, dw, _ = self._layout(W, H)
        span = self.view_end - self.view_start
        return dx + (x - self.view_start) / span * dw

    def _px_to_x(self, px, W, H):
        dx, _, dw, _ = self._layout(W, H)
        span = self.view_end - self.view_start
        return self.view_start + (px - dx) / dw * span

    def _sync_scrollbars(self):
        if self.vbar is None:
            return
        W, H = self.width(), self.height()
        dh = max(1, H - self.MT - self.MB)

        n = len(self.rows)
        total_h = n * self.ROW_HEIGHT
        vmax = max(0, total_h - dh)
        self.vbar.blockSignals(True)
        self.vbar.setRange(0, vmax)
        self.vbar.setPageStep(dh)
        self.vbar.setSingleStep(self.ROW_HEIGHT)
        if self.v_offset > vmax:
            self.v_offset = vmax
        self.vbar.setValue(int(round(self.v_offset)))
        self.vbar.blockSignals(False)
        self.vbar.setVisible(n > 0 and vmax > 0)

        span = self.view_end - self.view_start
        hmax = max(0, int(round(self.row_size - span)))
        self.hbar.blockSignals(True)
        self.hbar.setRange(0, hmax)
        self.hbar.setPageStep(int(round(span)))
        self.hbar.setSingleStep(max(1, int(span / 20)))
        self.hbar.setValue(int(round(self.view_start)))
        self.hbar.blockSignals(False)
        self.hbar.setVisible(n > 0)

    def _on_v_scroll(self, value):
        self.v_offset = float(value)
        self.update()

    def _on_h_scroll(self, value):
        span = self.view_end - self.view_start
        self.view_start = float(value)
        self.view_end = self.view_start + span
        if self.view_end > self.row_size:
            self.view_end = float(self.row_size)
            self.view_start = self.view_end - span
        self.update()

    def resizeEvent(self, event):
        self._sync_scrollbars()
        super().resizeEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, self.C_BG)

        if not self.rows:
            p.setPen(self.C_DIM)
            f = QFont(); f.setPointSizeF(12); p.setFont(f)
            p.drawText(QRectF(0, 0, W, H), Qt.AlignCenter, self.empty_text)
            return

        dx, dy, dw, dh = self._layout(W, H)
        n = len(self.rows)
        row_h = self.ROW_HEIGHT
        bar_h = row_h * 0.70
        bar_pad = (row_h - bar_h) / 2

        total_free = sum(sum(c for _, c in r) for _, r in self.rows)
        total = self.row_size * n
        ratio = total_free / total * 100 if total else 0

        f = QFont(); f.setPointSizeF(12); f.setBold(True)
        p.setFont(f); p.setPen(self.C_TEXT)
        p.drawText(QRectF(0, 8, W, 28), Qt.AlignCenter,
                   f"{self.dev}   总空闲 {ratio:.2f}%  "
                   f"({total_free:,} / {total:,} 块)")

        p.save()
        p.setClipRect(QRectF(dx, dy, dw, dh))

        first = max(0, int(self.v_offset // row_h))
        last  = min(n - 1, int((self.v_offset + dh) // row_h))

        for i in range(first, last + 1):
            row_no, recs = self.rows[i]
            row_y = dy + i * row_h - self.v_offset
            bar_y = row_y + bar_pad

            if i % 2 == 0:
                p.fillRect(QRectF(dx, row_y, dw, row_h), self.C_ROW_ALT)

            p.setPen(Qt.NoPen)
            p.fillRect(QRectF(dx, bar_y, dw, bar_h), self.C_USED)

            p.setBrush(self.C_FREE)
            for s, c in recs:
                sx = self._x_to_px(s, W, H)
                ex = self._x_to_px(s + c, W, H)
                if ex - sx < 0.75:
                    ex = sx + 0.75
                vx1 = max(sx, dx)
                vx2 = min(ex, dx + dw)
                if vx2 <= vx1:
                    continue
                p.drawRect(QRectF(vx1, bar_y, vx2 - vx1, bar_h))

        if (self.focus_index is not None
                and first <= self.focus_index <= last):
            row_y = dy + self.focus_index * row_h - self.v_offset
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor('#f59e0b'), 2))
            p.drawRect(QRectF(dx, row_y + 1, dw, row_h - 2))

        p.restore()

        for i in range(first, last + 1):
            row_no, recs = self.rows[i]
            row_y = dy + i * row_h - self.v_offset
            free = sum(c for _, c in recs)
            pct = free / self.row_size * 100 if self.row_size else 0

            f3 = QFont(); f3.setPointSizeF(10); f3.setBold(True)
            p.setFont(f3); p.setPen(self.C_TEXT)
            p.drawText(QRectF(0, row_y, dx - 8, row_h),
                       Qt.AlignVCenter | Qt.AlignRight,
                       f"{self.row_prefix} {row_no}")
            p.drawText(QRectF(dx + dw + 6, row_y, self.MR - 10, row_h),
                       Qt.AlignVCenter | Qt.AlignRight, f"{pct:.2f}%")

        span = self.view_end - self.view_start
        raw_step = span / 8
        step = nice_step(raw_step)
        tick = math.floor(self.view_start / step) * step
        axis_y = dy + dh
        p.setPen(QPen(self.C_EDGE, 1))
        p.drawLine(QPointF(dx, axis_y), QPointF(dx + dw, axis_y))

        f4 = QFont(); f4.setPointSizeF(9); p.setFont(f4)
        while tick <= self.view_end + step * 0.001:
            if tick >= self.view_start - step * 0.001:
                px = self._x_to_px(tick, W, H)
                if dx - 1 <= px <= dx + dw + 1:
                    p.setPen(QPen(self.C_EDGE, 1))
                    p.drawLine(QPointF(px, axis_y),
                               QPointF(px, axis_y + 4))
                    p.setPen(self.C_DIM)
                    p.drawText(QRectF(px - 50, axis_y + 6, 100, 20),
                               Qt.AlignCenter, fmt_tick(tick))
            tick += step

        p.setPen(self.C_DIM)
        p.drawText(QRectF(dx, axis_y + 24, dw, 20),
                   Qt.AlignCenter, f"{self.row_prefix} 内{self.axis_label}")

        self._draw_legend(p, W)
        self._draw_hover_box(p, W, H)

    def _draw_legend(self, p, W):
        x0 = W - self.MR + 6
        y0 = self.MT + 4
        box = 11
        f = QFont(); f.setPointSizeF(8.5); p.setFont(f)
        fm = p.fontMetrics()
        gap = 6

        items = [(self.C_USED, '已使用'), (self.C_FREE, '空闲')]
        x = x0
        for color, label in items:
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawRect(QRectF(x, y0, box, box))
            if color is self.C_FREE:
                p.setPen(QPen(self.C_FREE_BD, 1))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(x, y0, box, box))
            p.setPen(self.C_TEXT)
            tw = fm.horizontalAdvance(label)
            p.drawText(QRectF(x + box + 3, y0 - 2, tw + 2, box + 4),
                       Qt.AlignVCenter, label)
            x += box + 3 + tw + gap

    def wheelEvent(self, event):
        if not self.rows:
            return
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta == 0:
            return
        mods = event.modifiers()

        if mods & Qt.ControlModifier:
            self._zoom(event.position().x(), 1.25 if delta > 0 else 1 / 1.25)
        elif mods & Qt.ShiftModifier:
            self._pan_pixels(delta * 0.5)
        else:
            if self.vbar:
                v = self.vbar.value() - delta / 2
                self.vbar.setValue(
                    int(round(max(0, min(v, self.vbar.maximum())))))

    def _zoom(self, mouse_px, factor):
        W, H = self.width(), self.height()
        anchor = self._px_to_x(mouse_px, W, H)

        span = (self.view_end - self.view_start) / factor
        min_span = max(1.0, self.row_size / 1e6)
        span = max(min_span, min(span, float(self.row_size)))

        ratio = (anchor - self.view_start) / (self.view_end - self.view_start)
        self.view_start = anchor - ratio * span
        self.view_end = self.view_start + span

        if self.view_start < 0:
            self.view_start = 0
            self.view_end = span
        if self.view_end > self.row_size:
            self.view_end = float(self.row_size)
            self.view_start = self.row_size - span

        self._sync_scrollbars()
        self.update()

    def _pan_pixels(self, px):
        W, H = self.width(), self.height()
        dx, _, dw, _ = self._layout(W, H)
        span = self.view_end - self.view_start
        d_data = -px / dw * span
        self.view_start += d_data
        self.view_end += d_data
        if self.view_start < 0:
            self.view_end -= self.view_start
            self.view_start = 0
        if self.view_end > self.row_size:
            self.view_start -= (self.view_end - self.row_size)
            self.view_end = float(self.row_size)
        self._sync_scrollbars()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.rows:
            self._drag_pos = event.position()
            self._drag_view = (self.view_start, self.view_end)
            self.setCursor(Qt.ClosedHandCursor)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        self.setCursor(Qt.ArrowCursor)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None:
            W, H = self.width(), self.height()
            dx, _, dw, _ = self._layout(W, H)
            delta_px = event.position().x() - self._drag_pos.x()
            span = self._drag_view[1] - self._drag_view[0]
            d_data = -delta_px / dw * span
            self.view_start = self._drag_view[0] + d_data
            self.view_end = self._drag_view[1] + d_data
            if self.view_start < 0:
                self.view_end -= self.view_start
                self.view_start = 0
            if self.view_end > self.row_size:
                self.view_start -= (self.view_end - self.row_size)
                self.view_end = self.row_size
            self._sync_scrollbars()
            self.update()
            return

        self._hover_pos = event.position()
        self._hover_info = self._compute_hover(event.position())
        self.update()

    def leaveEvent(self, event):
        self._hover_pos = None
        self._hover_info = None
        self.update()

    def mouseDoubleClickEvent(self, event):
        if self.rows:
            self.view_start = 0.0
            self.view_end = float(self.row_size)
            self.v_offset = 0.0
            self._sync_scrollbars()
            self.update()

    def contextMenuEvent(self, event):
        menu = QMenu(self)

        def reset_all():
            self.view_start = 0.0
            self.view_end = float(self.row_size)
            self.v_offset = 0.0
            self._sync_scrollbars()
            self.update()

        act = QAction("重置缩放 / 回到顶部", self)
        act.triggered.connect(reset_all)
        menu.addAction(act)
        menu.exec(event.globalPos())

    def _compute_hover(self, pos):
        if not self.rows:
            return None
        W, H = self.width(), self.height()
        dx, dy, dw, dh = self._layout(W, H)
        x, y = pos.x(), pos.y()
        if not (dx <= x <= dx + dw and dy <= y <= dy + dh):
            return None

        rel_y = y - dy + self.v_offset
        row = int(rel_y // self.ROW_HEIGHT)
        n = len(self.rows)
        if not (0 <= row < n):
            return None

        row_no, recs = self.rows[row]
        block = self._px_to_x(x, W, H)

        free = sum(c for _, c in recs)
        cnt = len(recs)
        pct = free / self.row_size * 100 if self.row_size else 0

        lo, hi = 0, len(recs) - 1
        hit = None
        while lo <= hi:
            mid = (lo + hi) // 2
            s, c = recs[mid]
            if block < s:
                hi = mid - 1
            elif block >= s + c:
                lo = mid + 1
            else:
                hit = (s, c)
                break

        title = (f"{self.row_prefix} {row_no}   ·   空闲 {pct:.2f}%"
                 f"   ·   {cnt} 段")

        if hit:
            s, c = hit
            lines = [
                ("当前块号", f"{int(block):,}"),
                ("起始块号", f"{s:,}"),
                ("长度",     f"{c:,} 块   ({human_size(c * self.block_size)})"),
                ("结束块号", f"{s + c - 1:,}"),
            ]
            color = self.C_FREE_BD
        else:
            lines = [
                ("当前块号", f"{int(block):,}"),
                ("状态",     "已使用"),
            ]
            color = self.C_USED

        return {'title': title, 'lines': lines, 'color': color}

    def _draw_hover_box(self, p, W, H):
        hp = self._hover_pos
        info = self._hover_info
        if hp is None or info is None:
            return

        f_title = QFont(); f_title.setPointSizeF(9.5); f_title.setBold(True)
        f_body  = QFont(); f_body.setPointSizeF(9)

        p.setFont(f_body)
        fm_body = p.fontMetrics()
        label_w = max(fm_body.horizontalAdvance(k) for k, _ in info['lines'])
        value_w = max(fm_body.horizontalAdvance(v) for _, v in info['lines'])
        gap = 14
        content_w = label_w + gap + value_w

        p.setFont(f_title)
        fm_title = p.fontMetrics()
        title_w = fm_title.horizontalAdvance(info['title'])

        box_w = max(content_w, title_w) + 24
        title_h = fm_title.height() + 2
        line_h  = fm_body.height() + 3
        box_h = title_h + 8 + len(info['lines']) * line_h + 12

        x = hp.x() + 16
        y = hp.y() + 16
        if x + box_w > W - 6:
            x = hp.x() - box_w - 16
        if y + box_h > H - 6:
            y = hp.y() - box_h - 16
        x = max(6, x)
        y = max(6, y)

        box = QRectF(x, y, box_w, box_h)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 28))
        p.drawRoundedRect(box.translated(2, 2), 6, 6)

        p.setBrush(QColor(255, 255, 255, 245))
        p.setPen(QPen(QColor('#7a7a7a'), 1))
        p.drawRoundedRect(box, 6, 6)

        p.setBrush(info['color'])
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(x + 2, y + 2, 3, box_h - 4), 1.5, 1.5)

        p.setFont(f_title)
        p.setPen(QColor('#1a1a1a'))
        p.drawText(QRectF(x + 14, y + 6, box_w - 20, title_h),
                   Qt.AlignLeft | Qt.AlignVCenter, info['title'])

        sep_y = y + 6 + title_h + 4
        p.setPen(QPen(QColor('#e0e0e0'), 1))
        p.drawLine(QPointF(x + 14, sep_y), QPointF(x + box_w - 8, sep_y))

        p.setFont(f_body)
        for i, (k, v) in enumerate(info['lines']):
            ly = sep_y + 4 + i * line_h
            p.setPen(QColor('#777777'))
            p.drawText(QRectF(x + 14, ly, label_w, line_h),
                       Qt.AlignLeft | Qt.AlignVCenter, k)
            p.setPen(QColor('#1a1a1a'))
            p.drawText(QRectF(x + 14 + label_w + gap, ly,
                              value_w + 4, line_h),
                       Qt.AlignLeft | Qt.AlignVCenter, v)


# =============================================================================
# Part 4 · 采集线程（按包声明的 NEEDS 收集动作）
# =============================================================================
class CaptureThread(QThread):
    done = Signal(dict, str)  # {action: result}, stderr

    def __init__(self, dev, fs_id, actions, script, parent=None):
        super().__init__(parent)
        self.dev = dev
        self.fs_id = fs_id
        self.actions = actions
        self.script = script

    def run(self):
        try:
            py = sys.executable
            prefix = ['pkexec'] if shutil.which('pkexec') else ['sudo']

            results = {}
            for action in self.actions:
                r = subprocess.run(
                    prefix + [py, self.script, '--worker',
                              self.fs_id, action, self.dev],
                    capture_output=True, text=True, timeout=600)
                if r.returncode != 0:
                    self.done.emit(results, f"{action}: {r.stderr.strip()}")
                    return
                try:
                    results[action] = json.loads(r.stdout)
                except Exception as e:
                    self.done.emit(results, f"{action} 解析失败: {e}")
                    return
            self.done.emit(results, '')
        except Exception as e:
            self.done.emit({}, str(e))


# =============================================================================
# Part 5 · Worker 入口（root 侧只采集，JSON 到 stdout）
# =============================================================================
def worker_main(args):
    if len(args) < 3:
        print("usage: --worker <fs_id> <action> <dev>", file=sys.stderr)
        return 2
    fs_id, action, dev = args[0], args[1], args[2]
    load_packs()
    fn = collector_for(fs_id, action)
    if fn is None:
        print(f"unknown collector: {fs_id}/{action}", file=sys.stderr)
        return 2
    try:
        payload = fn(dev)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    return 0
