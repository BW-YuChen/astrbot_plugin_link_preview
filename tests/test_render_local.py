# -*- coding: utf-8 -*-
"""
链接预览插件 · 离线渲染冒烟测试（不启动 AstrBot 也能跑）

  python tests/test_render_local.py           # 离线：解析用例 + 占位图渲染
  python tests/test_render_local.py --online  # 追加：真实抓 GitHub/B站 各一条再渲染

产物输出到 tests/_output/，跑完自己打开看看版式。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)  # 测试脚本专用；插件本体在 AstrBot 里是按包相对导入的

OUT = os.path.join(HERE, "_output")
os.makedirs(OUT, exist_ok=True)


def test_parse():
    from core import fetch

    cases = [
        ("https://github.com/Hei-MaoM/xcpcrating", ("github", ("Hei-MaoM", "xcpcrating"))),
        ("github.com/Hei-MaoM/xcpcrating/tree/main/docs", ("github", ("Hei-MaoM", "xcpcrating"))),
        ("看这个 https://github.com/python/cpython.git 很强", ("github", ("python", "cpython"))),
        ("https://github.com/orgs/community/discussions", None),  # /orgs/ 不是仓库
        ("https://www.bilibili.com/video/BV1okeB6oEUx", ("bili_bv", "BV1okeB6oEUx")),
        ("https://m.bilibili.com/video/BV1okeB6oEUx", ("bili_bv", "BV1okeB6oEUx")),
        ("https://www.bilibili.com/video/av170001", ("bili_av", "170001")),
        ("https://b23.tv/AbCd123", ("bili_short", "https://b23.tv/AbCd123")),
        ("今天天气不错", None),
        ("随便说说的文字 github.com 是个域名但没有仓库路径", None),
    ]
    bad = 0
    for text, expect in cases:
        got = fetch.parse_target(text)
        # 归一：github 的元组比较即可
        ok = got == expect
        if not ok:
            bad += 1
            print(f"  ❌ parse({text!r}) = {got}，期望 {expect}")
        else:
            print(f"  ✅ {text[:52]!r} -> {got}")
    assert bad == 0, f"{bad} 个解析用例失败"
    print("解析用例全部通过\n")


def test_render_offline():
    """占位渲染：把图片下载换成 None（走占位块路径），验证绘图代码不炸。"""
    from core import card

    orig_load = card._load_cached_image
    card._load_cached_image = lambda *a, **k: None  # type: ignore

    gh = {
        "kind": "github",
        "owner": "Hei-MaoM",
        "repo": "一个长得不得了的仓库名字用来测试标题截断逻辑abc",
        "avatar": "",
        "stars": 19,
        "forks": 1,
        "issues": 0,
        "contributors": 1,
        "url": "https://github.com/Hei-MaoM/xcpcrating",
    }
    p1 = card.render_github_card(gh, OUT, print)
    assert p1 and os.path.exists(p1), "GitHub 卡渲染失败"
    print(f"  ✅ GitHub 卡（占位图）: {os.path.basename(p1)}")

    # contributors 为 None（接口失败场景）也不许炸
    gh2 = dict(gh, contributors=None, repo="xcpcrating")
    p2 = card.render_github_card(gh2, OUT, print)
    assert p2 and os.path.exists(p2), "GitHub 卡（无贡献者数）渲染失败"
    print(f"  ✅ GitHub 卡（contributors=None）: {os.path.basename(p2)}")

    bili = {
        "kind": "bilibili",
        "bvid": "BV1okeB6oEUx",
        "title": "琵琶曲 川大vs电科川唯九之战",
        "cover": "",
        "up": "丹霞qwq",
        "view": 2400,
        "like": 89,
        "coin": 10,
        "fav": 18,
        "url": "https://www.bilibili.com/video/BV1okeB6oEUx",
    }
    p3 = card.render_bili_card(bili, OUT, print)
    assert p3 and os.path.exists(p3), "B 站卡渲染失败"
    print(f"  ✅ B 站卡（占位封面）: {os.path.basename(p3)}")

    bili2 = dict(
        bili,
        title="一个特别特别特别长的标题专门用来验证两行折行加省略号的处理效果到底行不行啊",
        view=120000,
        like=9876,
    )
    p4 = card.render_bili_card(bili2, OUT, print)
    assert p4 and os.path.exists(p4), "B 站卡（长标题）渲染失败"
    print(f"  ✅ B 站卡（长标题折行）: {os.path.basename(p4)}")
    card._load_cached_image = orig_load  # 还原，别污染同进程的在线测试
    print()


def test_render_online():
    """真实抓一条 GitHub + 一条 B 站，验证接口字段与出图。"""
    from core import card, fetch

    for target, label in (
        (("github", ("Hei-MaoM", "xcpcrating")), "GitHub"),
        (("bili_bv", "BV1okeB6oEUx"), "B 站"),
    ):
        info, err = fetch.fetch_card_info(target, print)
        if not info:
            print(f"  ⚠️ {label} 抓取失败（可能是网络/限额）：{err}")
            continue
        path = card.render_card(info, OUT, print)
        assert path and os.path.exists(path), f"{label} 在线渲染失败"
        print(f"  ✅ {label} 在线卡片: {os.path.basename(path)}")


def test_wrap_continuity():
    """折行不许丢字：两行内容拼起来（去掉省略号）必须是原文的前缀。

    这是 1.0.4 修的真 bug——第二行曾被第三行开头的内容覆盖，
    标题中间整段丢失、行首字符重复（群友看到的「数数/祀的画」）。
    """
    from PIL import Image, ImageDraw

    from core import card

    d = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    long_text = "【终末地一条龙全收集】祀的画/宝箱/醚质/调查点/机器人/采录桩/工业点数/祀的画/塔晶/武陵/明日方舟终末地1.6"
    for w in (200, 300, 400, 600, 780, 1200):
        lines, _ = card._wrap2(d, long_text, w, 48, True)
        joined = "".join(lines).replace("…", "")
        assert long_text.startswith(joined), f"宽 {w} 折行丢字: {lines}"
        assert len(lines) <= 2, f"宽 {w} 超过两行: {lines}"
    print("  ✅ 折行连续性（6 档宽度不丢字）")


if __name__ == "__main__":
    print("== 链接解析 ==")
    test_parse()
    print("== 折行回归 ==")
    test_wrap_continuity()
    print("== 离线渲染（占位图路径）==")
    test_render_offline()
    if "--online" in sys.argv:
        print("== 在线抓取 + 渲染 ==")
        test_render_online()
    print("全部通过 🎉 产物在 tests/_output/")
