#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fs_packs — FS 包集合（自动发现）

把某个文件系统"打包带走"时，只需带走对应的一个模块：
    fs_packs/xfs.py   → XFS  的 collector + Tab 集合
    fs_packs/ext4.py  → ext4 的 collector + Tab 集合
    fs_packs/fat.py   → FAT12/16/32 与 exFAT 的 collector + Tab 集合
主窗口通过 core 的注册表按磁盘格式自动加载对应包。
"""
from .core import (  # noqa: F401
    FSPack,
    TabPlugin,
    BlockRangeChart,
    CaptureThread,
    register_pack,
    register_tab,
    register_collector,
    collector_for,
    actions_for,
    tabs_for,
    all_packs,
    find_pack_by_id,
    find_pack_for_fstype,
    load_packs,
    worker_main,
    list_block_devices,
    kv_parse,
    run_cmd,
    human_size,
    fmt_tick,
    nice_step,
    read_diskstats,
)

__all__ = [
    'FSPack', 'TabPlugin', 'BlockRangeChart', 'CaptureThread',
    'register_pack', 'register_tab', 'register_collector',
    'collector_for', 'actions_for', 'tabs_for',
    'all_packs', 'find_pack_by_id', 'find_pack_for_fstype',
    'load_packs', 'worker_main', 'list_block_devices',
    'kv_parse', 'run_cmd', 'human_size', 'fmt_tick', 'nice_step',
    'read_diskstats',
]
