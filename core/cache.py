# -*- coding: utf-8 -*-
"""链接预览 · 缓存治理（12 小时过期 + 整目录 256MB 上限）

用户在群里看到的每张卡片图都是磁盘上的一个 png，属于「用完即弃」的中间产物，
所以这里的策略是：

  ① 过期清理：`cards/` 下的卡片图超过 card_ttl_hours（默认 12 小时）直接删。
     头像 / 封面 / GitHub 标志（`assets/`）不按 12 小时清 —— 它们是可复用素材，
     重复下载反而更费流量，只在触发体积上限时按 LRU 淘汰。

  ② 体积上限：整个插件缓存目录超过 max_bytes（默认 256MB）时，按「最久未使用」
     从旧到新删，直到降到低水位线（默认 90% 上限），避免每次调用都反复触发清理。

  ③ 绝不误伤当前任务：两重保护 ——
     * 显式保护集：主流程渲染出的卡片图在「发送完成」前一直登记在案；
     * 新鲜文件宽限：10 分钟内写入的文件一律不参与淘汰（正在生成/下载中）。

全模块无 raise：清理失败只是没清掉，绝不影响出图主流程。
"""

from __future__ import annotations

import os
import time

CARD_TTL_HOURS_DEFAULT = 12
MAX_BYTES_DEFAULT = 256 * 1024 * 1024
# 低于上限的这个比例就不再继续删，避免「每次都清到刚好卡线」
LOW_WATER_RATIO = 0.9
# 新鲜文件宽限期（秒）：刚写入的文件可能正被当前任务使用
FRESH_GRACE = 600


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


class CacheSweeper:
    """插件缓存目录的过期清理 + 体积治理。线程安全由调用方保证（外部丢线程池）。"""

    def __init__(
        self,
        root: str,
        log=None,
        card_ttl_hours: float = CARD_TTL_HOURS_DEFAULT,
        max_bytes: int = MAX_BYTES_DEFAULT,
    ):
        self.root = root
        self.log = log
        self.card_ttl = max(1, float(card_ttl_hours)) * 3600
        self.max_bytes = max(16 * 1024 * 1024, int(max_bytes))  # 下限 16MB，防手滑配成 0
        self._protected: set = set()
        self.last_sweep = 0.0
        self.last_summary: dict = {}

    # ------------------------------------------------------------ 保护集

    def protect(self, path: str) -> None:
        if path:
            try:
                self._protected.add(os.path.abspath(path))
            except Exception:  # noqa: BLE001
                pass

    def unprotect(self, path: str) -> None:
        try:
            self._protected.discard(os.path.abspath(path))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 扫描

    def _scan(self) -> list:
        """列出现存缓存文件：(路径, 大小, mtime, 是否卡片)。"""
        out = []
        cards_dir = os.path.abspath(os.path.join(self.root, "cards"))
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            for name in filenames:
                p = os.path.abspath(os.path.join(dirpath, name))
                if p in self._protected:
                    continue  # 当前任务正在用
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append((p, st.st_size, st.st_mtime, os.path.dirname(p) == cards_dir))
        return out

    def stats(self) -> dict:
        """给 /链接诊断 用：当前缓存体积、文件数、最旧文件时间。"""
        files = self._scan()
        total = sum(f[1] for f in files)
        oldest = min((f[2] for f in files), default=0.0)
        return {
            "files": len(files),
            "bytes": total,
            "human": _human(total),
            "limit_human": _human(self.max_bytes),
            "oldest_age_h": (time.time() - oldest) / 3600 if oldest else 0.0,
            "protected": len(self._protected),
        }

    # ------------------------------------------------------------ 清理

    def sweep(self, force: bool = False) -> dict:
        """执行一轮清理，返回摘要 dict（供日志与诊断展示）。"""
        summary = {"expired": 0, "evicted": 0, "freed": 0, "remaining": 0, "human": "0B"}
        try:
            now = time.time()
            files = self._scan()

            # ① 过期卡片：只清 cards/ 下超过 TTL 的卡片图
            keep = []
            for path, size, mtime, is_card in files:
                if is_card and (now - mtime) > self.card_ttl:
                    try:
                        os.remove(path)
                        summary["expired"] += 1
                        summary["freed"] += size
                        continue
                    except OSError:
                        pass  # 删不掉就留着，下轮再说
                keep.append((path, size, mtime, is_card))

            # ② 体积上限：按最久未使用淘汰，降到低水位线
            total = sum(f[1] for f in keep)
            if total > self.max_bytes:
                target = int(self.max_bytes * LOW_WATER_RATIO)
                for path, size, mtime, _is_card in sorted(keep, key=lambda f: f[2]):
                    if total <= target:
                        break
                    if now - mtime < FRESH_GRACE:
                        continue  # 刚写的文件不动（可能正被当前任务使用/下载中）
                    if path in self._protected:
                        continue
                    try:
                        os.remove(path)
                        total -= size
                        summary["evicted"] += 1
                        summary["freed"] += size
                    except OSError:
                        continue
                keep = [f for f in keep if os.path.exists(f[0])]

            summary["remaining"] = total
            summary["human"] = _human(total)
            self.last_sweep = now
            self.last_summary = dict(summary)
            if self.log and (summary["expired"] or summary["evicted"]):
                self.log(
                    f"缓存清理：过期 {summary['expired']} 张、超限淘汰 {summary['evicted']} 个，"
                    f"释放 {_human(summary['freed'])}，现余 {summary['human']}"
                )
        except Exception as e:  # noqa: BLE001  清理失败不影响出图
            if self.log:
                self.log(f"缓存清理异常（已忽略）：{type(e).__name__}: {e}")
        return summary
