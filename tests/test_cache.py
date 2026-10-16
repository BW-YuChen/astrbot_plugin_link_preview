# -*- coding: utf-8 -*-
"""缓存治理测试：12 小时过期 + 体积上限 LRU 淘汰 + 不误伤当前任务。

  python tests/test_cache.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core.cache import CacheSweeper  # noqa: E402


def _mk(root: str, sub: str, name: str, size: int, age_hours: float = 0.0) -> str:
    d = os.path.join(root, sub)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as fh:
        fh.write(b"\0" * size)
    ts = time.time() - age_hours * 3600
    os.utime(p, (ts, ts))
    return p


def test_expiry():
    """超过 12 小时的卡片图删掉，未过期的保留；assets 不按 12 小时清。"""
    root = tempfile.mkdtemp(prefix="lp_cache_ttl_")
    try:
        old = _mk(root, "cards", "old.png", 1024, age_hours=13)
        fresh = _mk(root, "cards", "fresh.png", 1024, age_hours=1)
        asset_old = _mk(root, "assets", "x.img", 1024, age_hours=100)  # 素材不按 12h 清
        sw = CacheSweeper(root, card_ttl_hours=12)
        s = sw.sweep()
        assert not os.path.exists(old), "13 小时前的卡片应被清理"
        assert os.path.exists(fresh), "1 小时前的卡片应保留"
        assert os.path.exists(asset_old), "assets 素材不应按 12 小时过期"
        assert s["expired"] == 1, f"过期计数不对: {s}"
        print(f"  ✅ 12 小时过期：删 {s['expired']} 张，保留新卡片与素材")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_size_cap_lru():
    """超上限时按最久未使用淘汰到低水位（90%），最新的不删。"""
    root = tempfile.mkdtemp(prefix="lp_cache_size_")
    try:
        mb = 1024 * 1024
        # 10 个 20MB 文件 = 200MB，上限设 100MB -> 应淘汰到 <=90MB
        paths = []
        for i in range(10):
            paths.append(_mk(root, "cards", f"c{i}.png", 20 * mb, age_hours=20 - i * 2))
        sw = CacheSweeper(root, card_ttl_hours=999, max_bytes=100 * mb)
        s = sw.sweep()
        total = sum(
            os.path.getsize(os.path.join(root, "cards", f))
            for f in os.listdir(os.path.join(root, "cards"))
        )
        assert total <= 100 * mb * 0.9 + 1, f"未降到低水位: {total} 字节"
        assert os.path.exists(paths[-1]), "最新文件不该被淘汰"
        assert not os.path.exists(paths[0]), "最旧文件应被淘汰"
        assert s["evicted"] >= 5, f"淘汰数量异常: {s}"
        print(f"  ✅ 体积上限：淘汰 {s['evicted']} 个，现余 {s['human']}（≤ 90MB）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_protect_and_fresh():
    """使用中（protect）与 10 分钟内新写的文件，即使超限也不许删。"""
    root = tempfile.mkdtemp(prefix="lp_cache_prot_")
    try:
        mb = 1024 * 1024
        in_use = _mk(root, "cards", "in_use.png", 40 * mb, age_hours=30)
        just_written = _mk(root, "cards", "just_now.png", 40 * mb, age_hours=0.01)  # 36 秒前
        old1 = _mk(root, "cards", "o1.png", 40 * mb, age_hours=40)
        old2 = _mk(root, "cards", "o2.png", 40 * mb, age_hours=50)
        sw = CacheSweeper(root, card_ttl_hours=999, max_bytes=100 * mb)
        sw.protect(in_use)
        sw.sweep()
        assert os.path.exists(in_use), "被 protect 的文件不许删"
        assert os.path.exists(just_written), "10 分钟内的新文件不许删"
        assert not os.path.exists(old2), "最旧的文件应被淘汰"
        print("  ✅ 保护集与新鲜文件宽限生效（使用中/新写入的文件未被删）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_stats():
    root = tempfile.mkdtemp(prefix="lp_cache_stats_")
    try:
        _mk(root, "cards", "a.png", 5000, age_hours=2)
        _mk(root, "assets", "b.img", 5000, age_hours=6)
        st = CacheSweeper(root).stats()
        assert st["files"] == 2 and st["bytes"] == 10000, st
        assert 5 < st["oldest_age_h"] < 7, st  # 最旧的是 6 小时前的素材
        print(f"  ✅ 统计：{st['files']} 个文件 / {st['human']}，最旧 {st['oldest_age_h']:.1f}h")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    print("== 缓存治理 ==")
    test_expiry()
    test_size_cap_lru()
    test_protect_and_fresh()
    test_stats()
    print("全部通过 🎉")
