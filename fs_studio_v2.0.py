#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FS Studio — 多文件系统结构与性能分析器（FS 包插件化）

运行: python3 fs_studio_v2.0.py

依赖:
    pip install PySide6

架构:
    fs_packs/                     每个文件系统一个"包"，可独立带走
      core.py                     框架：注册表 + 通用图表 + 采集线程 + worker
      xfs.py                      XFS  的 collector + Tab 集合
      ext4.py                     ext2/3/4 的 collector + Tab 集合
    主窗口（本文件）              不认 FS，只认 "设备 + 包"

流程:
    lsblk 枚举所有带 FS 的块设备 → 按磁盘格式匹配 FS 包
    → 启动先进入"起始页"（不采集、不弹授权）
    → 用户选择设备：自动识别格式、加载该包的 Tab 集合、再采集（root 授权）
    → collector 以子进程 --worker <fs_id> <action> <dev> 运行，JSON 回传。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton,
                               QStatusBar, QTabWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from fs_packs import (CaptureThread, all_packs, collector_for,
                      find_pack_for_fstype, list_block_devices, load_packs,
                      worker_main)


class StartPage(QWidget):
    """起始页：启动时不采集、不弹授权；由用户挑选设备后再开始。"""
    device_chosen = Signal(object)   # 设备 dict（含 pack）
    refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(8)

        title = QLabel("FS Studio")
        title.setStyleSheet("font-size:26px; font-weight:bold;")
        layout.addWidget(title)

        subtitle = QLabel(
            "多文件系统结构与性能分析 · 选择一个分区开始")
        subtitle.setStyleSheet("color:#555; font-size:13px;")
        layout.addWidget(subtitle)

        self.packs_label = QLabel()
        self.packs_label.setStyleSheet("color:#777;")
        layout.addWidget(self.packs_label)

        hint = QLabel(
            "选中后将自动识别文件系统、加载对应 Tab，并请求一次授权来只读采集元数据。")
        hint.setStyleSheet("color:#999;")
        layout.addWidget(hint)

        layout.addSpacing(8)
        layout.addWidget(QLabel("检测到的设备"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['设备', '文件系统', '挂载点', '容量'])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemClicked.connect(self._on_click)
        layout.addWidget(self.tree, 1)

        row = QHBoxLayout()
        btn = QPushButton("🔄 刷新设备")
        btn.clicked.connect(self.refresh_requested)
        row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)

    def set_packs(self, packs):
        names = ', '.join(p.FS_NAME for p in packs) or '无'
        self.packs_label.setText(f"已加载 FS 包: {names}")

    def set_devices(self, devs):
        self.tree.clear()
        for d in devs:
            item = QTreeWidgetItem(
                [d['dev'], d['pack'].FS_NAME, d['mount'], d['size']])
            item.setData(0, Qt.UserRole, d)
            self.tree.addTopLevelItem(item)
        if not devs:
            self.tree.addTopLevelItem(
                QTreeWidgetItem(["未检测到已支持的 FS 分区", "", "", ""]))

    def _on_click(self, item, _col):
        d = item.data(0, Qt.UserRole)
        if d:
            self.device_chosen.emit(d)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FS Studio · 多文件系统结构与性能分析")
        self.resize(1500, 950)

        self.current_dev = None
        self.current_mount = None
        self.current_pack = None
        self.capture_thread = None
        self.tab_instances = []

        # Tabs 即主区域：起始页固定为第 0 页，包 Tab 动态加载在其后
        self.tabs = QTabWidget()
        self.start_page = StartPage()
        self.start_page.device_chosen.connect(self._select_device)
        self.start_page.refresh_requested.connect(self.refresh_devices)
        self.tabs.addTab(self.start_page, "起始页")
        self.setCentralWidget(self.tabs)

        # 状态栏
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(200)
        self.progress.setVisible(False)
        self.status.addPermanentWidget(self.progress)

        self.refresh_devices()

    # ---------------- 包 / Tab ----------------
    def _build_tabs(self, pack):
        """按包加载对应的 Tab 集合（保留第 0 页起始页）"""
        for i in range(self.tabs.count() - 1, 0, -1):
            w = self.tabs.widget(i)
            self.tabs.removeTab(i)
            w.deleteLater()
        self.tab_instances = []
        for cls in pack.tabs():
            try:
                w = cls()
                self.tabs.addTab(w, cls.TAB_TITLE)
                self.tab_instances.append(w)
            except Exception as e:
                print(f"加载 Tab {cls.__name__} 失败: {e}", file=sys.stderr)
        self._wire_cross_tab()

    def _wire_cross_tab(self):
        """包内跨 Tab 导航：某 Tab 发出 group_activated(组号)，
        任一实现 focus_group(组号) 的同包 Tab 接收并切换过去。
        （如 ext4 热力图点击 → 空闲区间定位到该块组）"""
        for src in self.tab_instances:
            sig = getattr(src, 'group_activated', None)
            if sig is None:
                continue
            for dst in self.tab_instances:
                if dst is src or not callable(getattr(dst, 'focus_group', None)):
                    continue
                sig.connect(lambda g, d=dst: self._focus_group(d, g))

    def _focus_group(self, target, group_no):
        try:
            target.focus_group(group_no)
        except Exception as e:
            print(f"{type(target).__name__}.focus_group 出错: {e}",
                  file=sys.stderr)
        idx = self.tabs.indexOf(target)
        if idx >= 0:
            self.tabs.setCurrentIndex(idx)
        self.status.showMessage(
            f"已跳转到「{getattr(target, 'TAB_TITLE', '')}」· 块组 {group_no}")

    def _collect_actions(self, pack):
        """收集该包所有 Tab 声明的采集动作（去重，保持顺序）"""
        seen = set()
        actions = []
        for cls in pack.tabs():
            for a in cls.NEEDS:
                if a not in seen and collector_for(pack.FS_ID, a):
                    seen.add(a)
                    actions.append(a)
        return actions

    # ---------------- 设备 ----------------
    def refresh_devices(self):
        if self.capture_thread and self.capture_thread.isRunning():
            self.status.showMessage("正在采集，请稍候再刷新设备")
            return
        load_packs()

        devs = []
        for d in list_block_devices():
            pack = find_pack_for_fstype(d['fstype'])
            if pack is None:
                continue
            devs.append(dict(d, pack=pack))

        packs = all_packs()
        self.start_page.set_packs(packs)
        self.start_page.set_devices(devs)
        self.tabs.setCurrentIndex(0)

        names = ', '.join(p.FS_NAME for p in packs) or '无'
        self.status.showMessage(
            f"已加载 FS 包: {names} · 检测到 {len(devs)} 个设备（请选择设备开始）")

    def _select_device(self, d):
        if self.capture_thread and self.capture_thread.isRunning():
            self.status.showMessage("正在采集，请稍候再切换设备")
            return
        pack = d['pack']
        self.current_dev = d['dev']
        self.current_mount = d['mount']
        self.status.showMessage(
            f"{d['dev']} 识别为 {pack.FS_NAME}（{d['fstype']}）")

        if self.current_pack is not pack:
            self.current_pack = pack
            self._build_tabs(pack)
        if self.tabs.count() > 1:
            self.tabs.setCurrentIndex(1)

        self._start_capture(d['dev'], pack)

    # ---------------- 采集 ----------------
    def _start_capture(self, dev, pack):
        if self.capture_thread and self.capture_thread.isRunning():
            self.status.showMessage("已有采集任务在运行，请稍候")
            return

        actions = self._collect_actions(pack)
        if not actions:
            self.status.showMessage(f"{pack.FS_NAME} 包没有声明采集动作")
            return

        self.status.showMessage(
            f"正在采集 {dev}（{pack.FS_ID}: {'/'.join(actions)}，可能需要授权）…")
        self.progress.setVisible(True)
        self.tabs.setEnabled(False)

        self.capture_thread = CaptureThread(
            dev, pack.FS_ID, actions, os.path.abspath(__file__), self)
        self.capture_thread.done.connect(self._on_capture_done)
        self.capture_thread.start()

    @Slot(dict, str)
    def _on_capture_done(self, payloads, stderr):
        self.progress.setVisible(False)
        self.tabs.setEnabled(True)

        if not payloads:
            self.status.showMessage("采集失败")
            QMessageBox.critical(self, "采集失败", stderr or "无数据返回")
            return

        for w in self.tab_instances:
            if hasattr(w, 'set_mount'):
                try:
                    w.set_mount(self.current_mount)
                except Exception:
                    pass

        for w in self.tab_instances:
            try:
                w.update_data(payloads)
            except Exception as e:
                print(f"{type(w).__name__}.update_data 出错: {e}",
                      file=sys.stderr)

        res = next(iter(payloads.values()), {})
        dev = res.get('dev', self.current_dev)
        extra = f" · {stderr.strip()}" if stderr else ""
        self.status.showMessage(f"采集完成: {dev}{extra}")


# =============================================================================
# 入口
# =============================================================================
def main():
    if len(sys.argv) >= 2 and sys.argv[1] == '--worker':
        sys.exit(worker_main(sys.argv[2:]))

    load_packs()

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    families = ['Noto Sans CJK SC', 'Noto Sans SC', 'Source Han Sans SC',
                'WenQuanYi Zen Hei', 'WenQuanYi Micro Hei']
    available = set(QFontDatabase.families())
    for fam in families:
        if fam in available:
            f = QFont(fam); f.setPointSize(10)
            app.setFont(f)
            break

    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
