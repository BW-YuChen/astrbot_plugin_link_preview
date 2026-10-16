# -*- coding: utf-8 -*-
"""
链接预览 QQ 机器人 · AstrBot 插件主入口

功能
  群成员 @机器人 并附上一个链接，机器人把它「展开」成一张预览卡片图回在群里
  （类似部分客户端的链接预览，但由机器人出图，任何平台都能用）。

  当前支持的链接（解析逻辑在 core/fetch.py 的 parse_target）：
    * GitHub 仓库   github.com/{owner}/{repo}
    * B 站视频      bilibili.com/video/BVxxxx / avxxxx、m.bilibili.com、b23.tv 短链
    * 通用网页      其他任意 http(s) 链接（github.io Pages、博客、文档站……）
                    抓 og 标签出「标题+封面+描述+链接」通用卡片；
                    直链文件（.zip/.png/.pdf 等）不抓

指令格式
  @机器人 <链接>        （不需要任何斜杠指令，消息里带链接即可，多条链接只取第一条）

行为口径
  * 不是链接 / 不支持的平台  -> **完全不理**（不回复、不 stop_event，
    把消息让给 LLM 和其他插件 —— 链接预览插件绝不能抢话）
  * 是支持的链接但抓取失败   -> 默认在群里回一条简短原因（silent_on_error
    可改为静默只记日志），同时 stop_event，避免 LLM 再对同一个失效链接
    滔滔不绝
  * 出图成功               -> 自己 await event.send() 发图（与竞赛日报插件
    同一套兜底：发送阶段异常插件捕获不到，所以不走 yield，失败能回退文本）
  * /链接诊断              -> 逐项自检（配置 / 数据目录 / b23.tv / B 站接口 /
    GitHub 接口）；发它都没回音 = 插件没装上或没加载
  * 用户冷却 / 请求互斥     -> 与战绩查询同款限流：同一人默认 5 秒一次，
    第三方接口结果内存缓存（GitHub 10 分钟 / 贡献者数 6 小时 / B 站 10 分钟）

导入方式
  AstrBot 把插件当包加载，必须用相对导入 `from .core import fetch`，
  不要 sys.path 注入（详细原因见竞赛日报插件 main.py 的「导入方式」节）。
"""

from __future__ import annotations

import asyncio
import os
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from .core import fetch
from .core.cache import CacheSweeper

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


class LinkPreviewPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._cooldown: dict = {}       # {sender_id: 上次处理时间}
        self._inflight: set = set()     # 正在处理的链接（同链接并发去重）
        data_dir = self._data_dir()
        try:
            card_ttl_h = float(self._cfg("card_ttl_hours", 12))
        except (TypeError, ValueError):
            card_ttl_h = 12.0
        try:
            max_mb = int(self._cfg("cache_max_mb", 256))
        except (TypeError, ValueError):
            max_mb = 256
        try:
            cache_min = float(self._cfg("cache_minutes", 10))
        except (TypeError, ValueError):
            cache_min = 10.0
        fetch.set_cache_ttl(cache_min)   # 接口结果缓存时长（贡献者数按比例放大）
        self._cache = CacheSweeper(
            data_dir,
            log=self._card_log,
            card_ttl_hours=card_ttl_h,
            max_bytes=max_mb * 1024 * 1024,
        )
        self._cleanup_task = None
        self._ensure_cleanup_task()
        logger.info(
            "[link_preview] 插件已加载，数据目录: %s（GitHub=%s，B站=%s，通用网页=%s，"
            "卡片保留 %sh，缓存上限 %sMB，数据缓存 %s 分钟）"
            % (
                data_dir,
                bool(self._cfg("enable_github", True)),
                bool(self._cfg("enable_bilibili", True)),
                bool(self._cfg("enable_generic", True)),
                card_ttl_h,
                max_mb,
                cache_min,
            )
        )

    # ---------------------------------------------------------------- 基础设施

    def _cfg(self, key: str, default=None):
        try:
            if hasattr(self.config, "get"):
                v = self.config.get(key, default)
            else:
                v = getattr(self.config, key, default)
            return default if v is None else v
        except Exception:  # noqa: BLE001
            return default

    def _data_dir(self) -> str:
        """按 AstrBot 规范把数据放 data/ 目录；逐级回退（同竞赛日报插件）。"""
        name = "astrbot_plugin_link_preview"
        try:
            from astrbot.api.star import StarTools  # type: ignore

            # StarTools.get_data_dir() 已按插件名隔离（data/plugin_data/<插件名>）；
            # 个别老版本返回的是共享根，故仅在末级目录名不匹配时再拼一层，避免嵌套两层
            base = str(StarTools.get_data_dir())
            path = base if os.path.basename(os.path.normpath(base)) == name else os.path.join(base, name)
            os.makedirs(path, exist_ok=True)
            return path
        except Exception:  # noqa: BLE001
            pass
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path  # type: ignore

            path = os.path.join(str(get_astrbot_data_path()), "plugin_data", name)
            os.makedirs(path, exist_ok=True)
            return path
        except Exception:  # noqa: BLE001
            pass
        path = os.path.join(_PLUGIN_DIR, "data")
        os.makedirs(path, exist_ok=True)
        return path

    def _card_log(self, msg: str) -> None:
        logger.info(f"[link_preview] {msg}")

    # ---------------------------------------------------------------- 缓存治理

    def _ensure_cleanup_task(self) -> None:
        """启动后台清理循环（幂等）。AstrBot 在不支持的上下文里初始化插件时退化为
        「只在每次处理链接时顺手清一次」，不影响功能。"""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 没有事件循环（极少见），跳过后台任务
        self._cleanup_task = loop.create_task(self._cleanup_loop())
        # 启动时先清一轮（上次运行留下的过期卡片）
        loop.create_task(asyncio.to_thread(self._cache.sweep))

    async def _cleanup_loop(self) -> None:
        """周期性清理：过期卡片（默认 12 小时）+ 超 256MB 时按 LRU 淘汰到低水位。"""
        while True:
            try:
                try:
                    minutes = int(self._cfg("cleanup_minutes", 30))
                except (TypeError, ValueError):
                    minutes = 30
                await asyncio.sleep(max(300, minutes * 60))
                summary = await asyncio.to_thread(self._cache.sweep)
                if summary.get("expired") or summary.get("evicted"):
                    logger.info(
                        "[link_preview] 定时清理完成：过期 %s 张、淘汰 %s 个，余 %s"
                        % (summary.get("expired"), summary.get("evicted"), summary.get("human"))
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 后台任务不许炸
                logger.warning(f"[link_preview] 清理循环异常（继续运行）：{type(e).__name__}: {e}")

    async def terminate(self) -> None:
        """插件卸载时停掉后台任务。"""
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            self._cleanup_task = None
        logger.info("[link_preview] 插件已卸载，清理任务已停止")

    def _cooldown_left(self, sid: str) -> int:
        try:
            cd = int(self._cfg("cooldown_seconds", 5))
        except (TypeError, ValueError):
            cd = 5
        if cd <= 0:
            return 0
        elapsed = time.time() - self._cooldown.get(sid, 0.0)
        return 0 if elapsed >= cd else int(cd - elapsed) + 1

    # ---------------------------------------------------------------- 消息入口

    @filter.event_message_type(filter.EventMessageType.ALL, priority=40)
    async def on_message(self, event: AstrMessageEvent):
        """扫描 @机器人 的消息：带支持的链接就展开成卡片图。

        priority 取正值让它先于竞赛日报插件的兜底 handler（priority=-100）跑，
        且竞赛日报的兜底只处理「/」开头的指令形态，两边不会抢消息。
        """
        try:
            if event.is_stopped():
                return
            # 只响应 @机器人 / 唤醒的消息：群里不 @ 机器人的普通聊天不碰
            if not getattr(event, "is_at_or_wake_command", False):
                return
            text = str(event.get_message_str() or "").strip()
            if not text or text.startswith("/"):
                return  # 指令形态，让给指令类插件

            target = fetch.parse_target(text)
            if target is None:
                return  # 不是本插件的事，完全不理，交给 LLM / 其他插件

            # 到这里说明确实命中了链接 —— 打一行 INFO，方便在日志里确认
            # 「插件没被触发」还是「触发了但哪一步挂了」
            logger.info(f"[link_preview] 命中链接消息: {target[0]} <- {text[:80]!r}")

            # 平台开关
            if target[0].startswith("bili") and not bool(self._cfg("enable_bilibili", True)):
                logger.info("[link_preview] B 站预览已被配置关闭，忽略")
                return
            if target[0] == "github" and not bool(self._cfg("enable_github", True)):
                logger.info("[link_preview] GitHub 预览已被配置关闭，忽略")
                return
            if target[0] == "generic" and not bool(self._cfg("enable_generic", True)):
                return  # 通用卡片默认开着；关掉后未识别的链接回到「完全不理」

            await self._handle(event, target)
        except Exception as e:  # noqa: BLE001  入口兜底：任何异常都不炸消息流
            logger.error(f"[link_preview] 处理消息异常: {type(e).__name__}: {e}", exc_info=True)

    async def _handle(self, event: AstrMessageEvent, target) -> None:
        """展开一个链接：限流 -> 抓取 -> 渲染 -> 发图。"""
        # 同一条链接正在处理中：静默等待缓存结果，不重复打接口
        key = repr(target)
        if key in self._inflight:
            return

        # 用户冷却
        try:
            sid = str(event.get_sender_id() or "?")
        except Exception:  # noqa: BLE001
            sid = "?"
        left = self._cooldown_left(sid)
        if left > 0:
            await event.send(MessageChain().message(f"⏳ 链接解析太频繁啦，请 {left} 秒后再试"))
            return
        self._cooldown[sid] = time.time()
        if len(self._cooldown) > 500:
            cutoff = time.time() - 3600
            for k in [k for k, v in self._cooldown.items() if v < cutoff]:
                self._cooldown.pop(k, None)

        self._inflight.add(key)
        self._ensure_cleanup_task()  # 兜底：万一 __init__ 时没有事件循环，这里有
        try:
            # ① 抓数据（同步阻塞的 urllib，丢线程池）
            info, err = await asyncio.to_thread(fetch.fetch_card_info, target, self._card_log)
            if not info:
                logger.warning(f"[link_preview] 链接数据获取失败: {err}")
                if not bool(self._cfg("silent_on_error", True)):
                    try:
                        await event.send(MessageChain().message(f"❌ 链接解析失败：{err}"))
                    except Exception:  # noqa: BLE001
                        pass
                # 静默也要拦住后续 LLM：别让它在群里对一条失效链接滔滔不绝
                event.stop_event()
                return

            # ② 渲染卡片（渲染期间把产物登记为「使用中」，清理任务不会碰它）
            from .core import card

            path = None
            try:
                path = await asyncio.to_thread(
                    card.render_card, info, os.path.join(self._data_dir(), "cards"), self._card_log
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[link_preview] 卡片渲染异常: {type(e).__name__}: {e}")

            # ③ 发送：自己 send（发图失败可回退文本），成功后拦住 LLM
            caption = self._fallback_text(info)
            if path:
                self._cache.protect(path)  # 发送完成前不许被清理删掉
                try:
                    chain = MessageChain().file_image(path)
                    await event.send(chain)
                    logger.info(f"[link_preview] 卡片图已发送：{os.path.basename(path)}")
                    event.stop_event()
                    return
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[link_preview] 卡片图发送失败，回退文本：{type(e).__name__}: {e}"
                    )
                finally:
                    self._cache.unprotect(path)
            # 渲染失败 / 发图失败 -> 回一条兜底文本（保证「一定有回应」）
            if not bool(self._cfg("silent_on_error", True)) or caption:
                try:
                    await event.send(MessageChain().message(caption or f"❌ 卡片生成失败：{err or '渲染异常'}"))
                except Exception:  # noqa: BLE001
                    pass
            event.stop_event()
        finally:
            self._inflight.discard(key)
            # 顺手做一次轻量清理：距上轮超过清理间隔才真扫，避免每张图都遍历目录
            try:
                if time.time() - self._cache.last_sweep >= max(300, int(self._cfg("cleanup_minutes", 30)) * 60):
                    await asyncio.to_thread(self._cache.sweep)
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 诊断指令

    @filter.command("链接诊断")
    async def cmd_link_diag(self, event: AstrMessageEvent):
        """/链接诊断：逐项自检（插件加载 / 配置 / 数据目录 / 外网链路）。

        这条指令本身也是个试金石：**如果发它都没回音，说明插件根本没装上/没加载**，
        而不是链接解析的问题。
        """
        lines = ["🔗 链接预览 · 诊断"]

        # ① 配置开关
        lines.append(
            f"① 配置：GitHub={'开' if bool(self._cfg('enable_github', True)) else '关'}，"
            f"B站={'开' if bool(self._cfg('enable_bilibili', True)) else '关'}，"
            f"通用网页={'开' if bool(self._cfg('enable_generic', True)) else '关'}，"
            f"冷却={self._cfg('cooldown_seconds', 5)}s，"
            f"静默失败={'是' if bool(self._cfg('silent_on_error', True)) else '否（失败会回原因）'}"
        )

        # ② 缓存治理现状（12 小时过期 / 256MB 上限）
        try:
            st = await asyncio.to_thread(self._cache.stats)
            last = self._cache.last_summary or {}
            lines.append(
                f"② 缓存：{st['human']}/{st['limit_human']}，{st['files']} 个文件，"
                f"最旧 {st['oldest_age_h']:.1f} 小时，使用中 {st['protected']} 个"
                + (
                    f"；上轮清理：过期 {last.get('expired', 0)} 张、淘汰 {last.get('evicted', 0)} 个"
                    if last
                    else ""
                )
            )
        except Exception as e:  # noqa: BLE001
            lines.append(f"② 缓存统计异常：{type(e).__name__}: {e}")

        # ③ 数据目录可写
        try:
            d = self._data_dir()
            probe = os.path.join(d, ".diag")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            lines.append(f"③ 数据目录可写：{d}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"③ 数据目录异常：{type(e).__name__}: {e}")

        # ④⑤⑥⑦ 外网链路（b23.tv / B 站接口 / GitHub API / GitHub 网页）
        try:
            results = await asyncio.to_thread(fetch.probe_network)
            marks = "④⑤⑥⑦⑧⑨"
            for i, (name, ok, detail) in enumerate(results):
                mark = "✅" if ok else "❌"
                no = marks[i] if i < len(marks) else str(i + 1)
                lines.append(f"{no} {mark} {name}：{detail}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"④ 外网探测异常：{type(e).__name__}: {e}")

        lines.append("全部 ✅ 说明插件与链路正常；发链接没反应请把 AstrBot 日志里 [link_preview] 相关行发出来。")
        yield event.plain_result("\n".join(lines))

    # ---------------------------------------------------------------- 发送

    @staticmethod
    def _fallback_text(info: dict) -> str:
        """出不了图时的最小可读兜底（标题 + 原链接）。"""
        kind = info.get("kind")
        if kind == "github":
            return (
                f"📦 {info.get('owner')}/{info.get('repo')}\n"
                f"⭐ {info.get('stars')}   🍴 {info.get('forks')}\n"
                f"{info.get('url')}"
            )
        if kind == "bilibili":
            return f"📺 {info.get('title')}\nUP主：{info.get('up')}\n{info.get('url')}"
        if kind == "generic":
            parts = [f"🔗 {info.get('title')}"]
            if info.get("desc"):
                parts.append(str(info.get("desc"))[:80])
            parts.append(str(info.get("url") or ""))
            return "\n".join(parts)
        return str(info.get("url") or "")
