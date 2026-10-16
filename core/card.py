# -*- coding: utf-8 -*-
"""
链接预览机器人 · 卡片图渲染层（Pillow）

参照群里常见的「链接展开卡片」版式：

  GitHub 卡：左上 owner/repo 大标题、右上仓库头像、下方一行
             贡献者 / Issues / Stars / Fork 统计 + GitHub 标志，
             底边一条按头像取色的渐变色条
  B 站卡  ：标题 + 封面大图 + UP主 / 点赞 / 投币 / 收藏 / 观看 + 原链接

设计要点（与竞赛日报插件的 card.py 同一口径）：

  * 依赖 Pillow，本模块属于锦上添花 —— 全函数无 raise，任何渲染失败
    返回 None，调用方回退文本 / 静默
  * 头像 / 封面 / GitHub 标志下载失败时画占位块，**绝不让出图失败**
  * 输出按 平台+标识 固定命名（同名覆盖），不会无限堆积
  * 图片下载带磁盘缓存（md5 命名 + 失败标记），同一链接反复发不反复下载
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
import urllib.error
import urllib.request

try:
    from astrbot.api import logger as _log      # 统一走 AstrBot 日志系统
except ImportError:                             # 脱离 AstrBot 的离线环境（只跑渲染测试）
    class _NullLogger:                          # 退化为静默占位，不引入 Python 内置 logging
        def __getattr__(self, _name):
            return lambda *a, **kw: None
    _log = _NullLogger()

# ------------------------------------------------------------------ 基础设施

CARD_TTL = 30 * 86400      # 下载图片（头像/封面/标志）磁盘缓存 30 天
CARD_FAIL_TTL = 1800       # 下载失败的标记 30 分钟内不再重试
DL_TIMEOUT = 8
DL_MAX_BYTES = 8 * 1024 * 1024
DL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

INK = "#24292f"      # 主文字
MUTED = "#57606a"    # 次文字
LINK_BLUE = "#0969da"
PLACEHOLDER = "#eaeef2"


def _hex(c: str) -> tuple:
    c = c.lstrip("#")
    return tuple(int(c[i : i + 2], 16) for i in (0, 2, 4))


def _mix(c1, c2: str, t: float) -> tuple:
    r1, g1, b1 = c1 if isinstance(c1, tuple) else _hex(c1)
    r2, g2, b2 = _hex(c2)
    return (
        int(r1 + (r2 - r1) * t),
        int(g1 + (g2 - g1) * t),
        int(b1 + (b2 - b1) * t),
    )


# ------------------------------------------------------------------ 字体

_FONT_CACHE: dict = {}
_FONT_CACHE_MAX = 24
# 字体目录：Linux 服务器（AstrBot 常跑在 Docker）与 Windows / macOS 都覆盖
_FONT_DIRS = [
    # Linux / Docker
    "/usr/share/fonts",
    "/usr/share/fonts/opentype",
    "/usr/share/fonts/truetype",
    "/usr/local/share/fonts",
    os.path.expanduser("~/.fonts"),
    os.path.expanduser("~/.local/share/fonts"),
    # Windows / macOS
    r"C:\Windows\Fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
]

# 字体优先级（越靠前字形越标准）。
# 为什么要挑：Linux 上常见的 DroidSansFallback / WQY ZenHei 是十几年前的老字体，
# 部分汉字字形不规范（用户反馈「字形不对」多半就是落到了它们身上）。
# 标准中文黑体顺序：思源黑体(Source Han Sans) = Noto Sans CJK SC > 微软雅黑 > 苹方 > 文泉驿/Droid（兜底）
_FONT_CANDIDATES = {
    True: (
        # ① 思源黑体 / Noto Sans CJK SC（开源标准黑体，字形最规范）
        "NotoSansCJKsc-Bold.otf",
        "NotoSansCJK-Bold.ttc",
        "NotoSansSC-Bold.otf",
        "SourceHanSansSC-Bold.otf",
        "SourceHanSansCN-Bold.otf",
        "SourceHanSans-Bold.ttc",
        # ② Windows
        "msyhbd.ttc",
        "msyhbd.ttf",
        "Dengb.ttf",
        "simhei.ttf",
        # ③ macOS
        "PingFang.ttc",
        "STHeiti Medium.ttc",
        # ④ 兜底（字形一般，只在上面都没有时用）
        "wqy-microhei.ttc",
        "wqy-zenhei.ttc",
        "DroidSansFallbackFull.ttf",
    ),
    False: (
        "NotoSansCJKsc-Regular.otf",
        "NotoSansCJK-Regular.ttc",
        "NotoSansSC-Regular.otf",
        "SourceHanSansSC-Regular.otf",
        "SourceHanSansCN-Regular.otf",
        "SourceHanSans-Regular.ttc",
        "msyh.ttc",
        "msyhl.ttc",
        "Deng.ttf",
        "simhei.ttf",
        "PingFang.ttc",
        "STHeiti Light.ttc",
        "wqy-microhei.ttc",
        "wqy-zenhei.ttc",
        "DroidSansFallbackFull.ttf",
    ),
}

# 兜底扫描时按「字形优先级」给文件名打分：思源/Noto 优先，Droid 这些老字体最后
_FALLBACK_ORDER = ("sourcehan", "notosanscjk", "notosanssc", "noto", "msyh", "pingfang",
                   "wqy-microhei", "wqy", "droid", "deng", "hei", "song")

_CJK_OK_CACHE: dict = {}
_FONT_PICKED_LOGGED = False


def _has_cjk_glyphs(path: str) -> bool:
    """验证字体真的含中文字形。

    办法：把「终」和私用区码位（\\ue000，任何字体都不会有）各自渲染成位图，
    两者不一样说明「终」是真字形；一样说明都落到 .notdef（豆腐块）-> 判否。
    Linux Docker 里字体装得不全时，这一步能挡住「图上一堆方框」的情况。
    """
    if path in _CJK_OK_CACHE:
        return _CJK_OK_CACHE[path]
    ok = False
    try:
        from PIL import Image, ImageFont

        f = ImageFont.truetype(path, 32)
        m_real = Image.Image()._new(f.getmask("终")).tobytes()  # noqa: SLF001
        m_tofu = Image.Image()._new(f.getmask("\ue000")).tobytes()  # noqa: SLF001
        ok = bool(m_real) and m_real != m_tofu
    except Exception:  # noqa: BLE001
        ok = False
    _CJK_OK_CACHE[path] = ok
    return ok


def _find_font_file(bold: bool) -> str | None:
    """按「字形标准度」挑一个含中文的字体文件；找不到返回 None。"""
    from glob import glob

    # ① 指名道姓的候选（优先标准黑体），逐个验证含中文
    for name in _FONT_CANDIDATES[bold]:
        for d in _FONT_DIRS:
            try:
                hits = sorted(glob(os.path.join(d, "**", name), recursive=True))
            except Exception:  # noqa: BLE001
                hits = []
            for hit in hits:
                if _has_cjk_glyphs(hit):
                    return hit
    # ② 兜底：扫所有字体文件，按文件名打分排序后取第一个含中文的
    try:
        found = []
        for d in _FONT_DIRS:
            for pat in ("*.tt[cf]", "*.otf", "*.ttc"):
                for hit in glob(os.path.join(d, "**", pat), recursive=True):
                    low = os.path.basename(hit).lower()
                    if not re.search(r"(cjk|han|hei|song|wqy|noto|pingfang|deng|msyh|droid|kai|ming)", low):
                        continue
                    rank = len(_FALLBACK_ORDER)
                    for i, key in enumerate(_FALLBACK_ORDER):
                        if key in low:
                            rank = i
                            break
                    found.append((rank, low, hit))
        for _rank, _low, hit in sorted(found):
            if _has_cjk_glyphs(hit):
                return hit
    except Exception:  # noqa: BLE001
        pass
    return None


def _font(size: int, bold: bool = False):
    """取字体实例。找不到含中文的字体时退到 AstrBot 自带的默认字体（可能画不出中文，
    所以 render_card 会在出图前先做一次字体体检，见 fonts_ready()）。"""
    global _FONT_PICKED_LOGGED
    from PIL import ImageFont

    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    path = _find_font_file(bold)
    if path is None and bold:
        path = _find_font_file(False)  # 没有粗体就用常规体顶（至少字形对）
    f = None
    if path:
        try:
            f = ImageFont.truetype(path, size)
            if not _FONT_PICKED_LOGGED:
                _FONT_PICKED_LOGGED = True
                _log.info(f"[link_preview] 卡片字体：{path}")
        except Exception:  # noqa: BLE001
            f = None
    if f is None:
        try:
            f = ImageFont.load_default()
        except Exception:  # noqa: BLE001
            f = None
    if len(_FONT_CACHE) >= _FONT_CACHE_MAX:
        _FONT_CACHE.pop(next(iter(_FONT_CACHE)), None)
    _FONT_CACHE[key] = f
    return f


def fonts_ready() -> bool:
    """出图前的字体体检：没有任何含中文的字体就不要出图（否则满屏豆腐块），
    让调用方回退纯文本，比给用户看一张乱码图好。"""
    return _find_font_file(False) is not None or _find_font_file(True) is not None


def _text_w(draw, text: str, font) -> float:
    try:
        return draw.textlength(text, font=font)
    except Exception:  # noqa: BLE001
        return float(font.getsize(text)[0])


def _fit_font(draw, text: str, max_w: float, size: int, bold: bool, min_size: int):
    """字号从 size 往下试，让文本塞进 max_w；低于 min_size 后改为截断加 …。"""
    f = _font(size, bold)
    while size > min_size and _text_w(draw, text, f) > max_w:
        size -= 2
        f = _font(size, bold)
    if _text_w(draw, text, f) <= max_w:
        return text, f
    # 截断
    s = text
    while s and _text_w(draw, s + "…", f) > max_w:
        s = s[:-1]
    return s + "…", f


def _wrap2(draw, text: str, max_w: float, size: int, bold: bool):
    """按宽度折行（中英混排按字符折，最多 2 行，超出加 …）。

    ⚠️ 这里曾有 bug：第二行放满后用「第三行开头的内容」把第二行覆盖了，
    导致标题中间整段丢失 + 行首字符重复（群友看到的「数数/祀的画」就是它）。
    正确做法：第二行已装满就直接加 … 返回，动都不许动已装入的内容。
    """
    f = _font(size, bold)
    lines: list[str] = []
    cur = ""
    for ch in text:
        if cur and _text_w(draw, cur + ch, f) > max_w:
            lines.append(cur)
            if len(lines) == 2:
                # 两行已装满，ch 起还有剩余 -> 第二行尾加省略号
                lines[-1] += "…"
                return lines, f
            cur = ch
        else:
            cur += ch
    else:
        lines.append(cur)
    return lines, f


# ------------------------------------------------------------------ 图片下载

def _download(url: str, referer: str = "") -> bytes:
    for attempt in (0, 1):
        try:
            h = {"User-Agent": DL_UA}
            if referer:
                h["Referer"] = referer
            req = urllib.request.Request(url, headers=h)
            with OPENER.open(req, timeout=DL_TIMEOUT) as r:
                return r.read(DL_MAX_BYTES)
        except urllib.error.HTTPError:
            return b""
        except Exception:  # noqa: BLE001
            if attempt:
                return b""
            time.sleep(0.5)
    return b""


def _load_cached_image(url: str, cache_dir: str, referer: str = ""):
    """下载并返回 PIL Image；失败返回 None。带磁盘缓存 + 失败标记。"""
    if not url:
        return None
    try:
        from PIL import Image

        os.makedirs(cache_dir, exist_ok=True)
        key = hashlib.md5(url.encode()).hexdigest()
        good = os.path.join(cache_dir, key + ".img")
        miss = os.path.join(cache_dir, key + ".miss")
        data = b""
        if os.path.exists(good) and time.time() - os.path.getmtime(good) < CARD_TTL:
            with open(good, "rb") as fh:
                data = fh.read()
        elif os.path.exists(miss) and time.time() - os.path.getmtime(miss) < CARD_FAIL_TTL:
            return None
        else:
            data = _download(url, referer)
            if data:
                with open(good, "wb") as fh:
                    fh.write(data)
                try:
                    os.remove(miss)
                except OSError:
                    pass
            else:
                with open(miss, "wb") as fh:
                    fh.write(b"")
                return None
        # 统一转 RGBA：GitHub 标志是调色板 PNG，转 RGB 会把透明底压成黑块
        return Image.open(__import__("io").BytesIO(data)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None


def _rounded(img, radius: int):
    """把图片裁成圆角（返回带 alpha 的 RGBA）。"""
    from PIL import Image, ImageDraw

    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1], radius, fill=255)
    img.putalpha(mask)
    return img


def _fit_cover(img, w: int, h: int):
    """居中裁剪到目标尺寸（不变形）。"""
    from PIL import ImageOps

    try:
        return ImageOps.fit(img, (w, h))
    except Exception:  # noqa: BLE001
        return img.resize((w, h))


# ------------------------------------------------------------------ 小图标

def _icon_person(size: int, color: str):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = _hex(color) + (255,)
    r = size * 0.20
    cx = size / 2
    # 头
    d.ellipse([cx - r, size * 0.06, cx + r, size * 0.06 + 2 * r], fill=c)
    # 肩（半圆）
    d.pieslice([size * 0.08, size * 0.52, size * 0.92, size * 1.25], 180, 360, fill=c)
    return img


def _icon_issue(size: int, color: str):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = _hex(color) + (255,)
    lw = max(2, int(size * 0.11))
    pad = size * 0.10
    d.ellipse([pad, pad + size * 0.05, size - pad, size - pad], outline=c, width=lw)
    # 指针（秒表表盘感）
    d.line([size / 2, size * 0.42, size / 2, size * 0.62], fill=c, width=max(2, lw - 2))
    d.line([size / 2, size * 0.42, size * 0.66, size * 0.32], fill=c, width=max(2, lw - 2))
    return img


def _icon_star(size: int, color: str):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = _hex(color) + (255,)
    cx, cy = size / 2, size * 0.55
    R, r = size * 0.45, size * 0.18
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = R if i % 2 == 0 else r
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    d.polygon(pts, fill=c)
    return img


def _icon_fork(size: int, color: str):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = _hex(color) + (255,)
    lw = max(2, int(size * 0.10))
    cr = size * 0.14  # 端点圆半径
    lt, rt, bm = (size * 0.22, size * 0.14), (size * 0.78, size * 0.14), (size * 0.5, size * 0.86)
    # 两条竖线 + 汇合线
    d.line([lt[0], lt[1] + cr, lt[0], size * 0.45], fill=c, width=lw)
    d.line([rt[0], rt[1] + cr, rt[0], size * 0.45], fill=c, width=lw)
    d.line([lt[0], size * 0.45, bm[0], size * 0.62], fill=c, width=lw)
    d.line([rt[0], size * 0.45, bm[0], size * 0.62], fill=c, width=lw)
    d.line([bm[0], size * 0.62, bm[0], bm[1] - cr], fill=c, width=lw)
    for p in (lt, rt, bm):
        d.ellipse([p[0] - cr, p[1] - cr, p[0] + cr, p[1] + cr], outline=c, width=lw)
    return img


def _github_mark(size: int, cache_dir: str):
    """GitHub 章鱼猫标志：先试官方 PNG，失败画一个简化圆底「GH」。"""
    img = _load_cached_image(
        "https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png",
        cache_dir,
    )
    if img is not None:
        try:
            img = img.resize((size, size))
            return img.convert("RGBA")
        except Exception:  # noqa: BLE001
            pass
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, size - 1, size - 1], fill=_hex(INK) + (255,))
    f = _font(int(size * 0.34), bold=True)
    d.text((size / 2, size / 2), "GH", font=f, fill=(255, 255, 255, 255), anchor="mm")
    return img


# ------------------------------------------------------------------ 出口

def _out_path(out_dir: str, name: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, name + ".png")


def render_github_card(info: dict, out_dir: str, log=None) -> str | None:
    """GitHub 仓库卡片。成功返回文件路径，失败返回 None。"""
    try:
        from PIL import Image, ImageDraw

        W, H = 920, 470
        PAD = 64
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, W - 1, H - 1], 36, fill=(255, 255, 255, 255))

        cache_dir = os.path.join(out_dir, "assets")

        # ---- 标题区：owner/ 一行 + repo 一行（各自独立截断）
        title_max_w = W - PAD * 2 - 180 - 40
        owner, repo = str(info.get("owner") or ""), str(info.get("repo") or "")
        owner_s, f_owner = _fit_font(d, owner + "/", title_max_w, 46, False, 26)
        repo_s, f_repo = _fit_font(d, repo, title_max_w, 74, True, 34)
        d.text((PAD, 66), owner_s, font=f_owner, fill=INK)
        d.text((PAD, 124), repo_s, font=f_repo, fill=INK)

        # ---- 头像（右上，圆角方块）
        avatar = _load_cached_image(info.get("avatar") or "", cache_dir)
        if avatar is not None:
            av = _fit_cover(avatar, 180, 180)
            img.paste(_rounded(av, 28), (W - PAD - 180, 66), _rounded(av, 28))
        else:
            d.rounded_rectangle([W - PAD - 180, 66, W - PAD, 246], 28, fill=_hex(PLACEHOLDER) + (255,))

        # ---- 统计行：图标 + 数字，标签在数字下面
        stats = [
            (_icon_person, info.get("contributors"), "Contributor"),
            (_icon_issue, info.get("issues"), "Issues"),
            (_icon_star, info.get("stars"), "Stars"),
            (_icon_fork, info.get("forks"), "Fork"),
        ]
        icon_size = 44
        y = 330
        x = PAD
        f_num = _font(40, True)
        f_label = _font(30, False)
        for icon_fn, value, label in stats:
            shown = "—" if value is None else _fmt_count(value)
            icon = icon_fn(icon_size, INK)
            img.paste(icon, (x, y), icon)
            d.text((x + icon_size + 12, y + 2), shown, font=f_num, fill=INK)
            num_w = icon_size + 12 + _text_w(d, shown, f_num)
            d.text((x, y + icon_size + 10), label, font=f_label, fill=MUTED)
            # 块宽取「数字行 / 标签行」里更宽的那个，防止标签互相压字
            block_w = max(num_w, _text_w(d, label, f_label))
            x += max(int(block_w) + 52, 150)

        # ---- GitHub 标志（右下）
        mark = _github_mark(64, cache_dir)
        img.paste(mark, (W - PAD - 64, y - 8), mark)

        # ---- 底部渐变色条：按头像平均取色（取不到用 GitHub 蓝）
        bar_color = (9, 105, 218)
        if avatar is not None:
            try:
                avg = avatar.resize((1, 1)).getpixel((0, 0))[:3]
                # 提亮一档，避免取到太暗的颜色导致渐变看不清
                bar_color = _mix(avg, "#ffffff", 0.25)
            except Exception:  # noqa: BLE001
                pass
        bar_h = 16
        for bx in range(0, W, 4):
            c = _mix(bar_color, "#8b5cf6", bx / max(1, W - 1))
            d.rectangle([bx, H - bar_h, min(bx + 3, W - 1), H - 1], fill=c + (255,))

        # ---- 圆角裁切输出
        img = _rounded(img, 36)
        path = _out_path(out_dir, f"github_{info.get('owner')}_{info.get('repo')}")
        img.save(path)
        return path
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"GitHub 卡片渲染失败: {type(e).__name__}: {e}")
        return None


def render_bili_card(info: dict, out_dir: str, log=None) -> str | None:
    """B 站视频卡片。成功返回文件路径，失败返回 None。"""
    try:
        from PIL import Image, ImageDraw

        W = 920
        PAD = 64
        inner = W - PAD * 2
        cache_dir = os.path.join(out_dir, "assets")

        # 先离屏量一下标题占几行，才知道画布多高
        probe = Image.new("RGBA", (10, 10))
        pd = ImageDraw.Draw(probe)
        title = str(info.get("title") or "(无标题)")
        lines, f_title = _wrap2(pd, title, inner, 48, True)
        title_h = len(lines) * 64 + (18 if len(lines) > 1 else 0)

        cover_w, cover_h = inner, int(inner * 9 / 16)
        f_body = _font(42, False)
        f_link = _font(38, False)

        body_lines = [
            "UP主：" + (str(info.get("up") or "未知")),
            f"点赞 {_fmt_count(info.get('like'))}   投币 {_fmt_count(info.get('coin'))}",
            f"收藏 {_fmt_count(info.get('fav'))}   观看 {_fmt_count(info.get('view'))}",
        ]
        link = str(info.get("url") or "")
        link_s, f_link = _fit_font(pd, link, inner, 38, False, 24)

        H = 52 + title_h + 26 + cover_h + 40 + 3 * 62 + 26 + 58 + 48
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, W - 1, H - 1], 36, fill=(255, 255, 255, 255))

        # ---- 标题
        y = 52
        for line in lines:
            d.text((PAD, y), line, font=f_title, fill=INK)
            y += 64

        # ---- 封面
        y += 26 if len(lines) > 1 else 0
        cover = _load_cached_image(info.get("cover") or "", cache_dir, referer="https://www.bilibili.com/")
        cover_y = y
        if cover is not None:
            cv = _fit_cover(cover, cover_w, cover_h)
            cv_r = _rounded(cv, 24)
            img.paste(cv_r, (PAD, cover_y), cv_r)
        else:
            d.rounded_rectangle(
                [PAD, cover_y, PAD + cover_w, cover_y + cover_h], 24, fill=_hex(PLACEHOLDER) + (255,)
            )
            f_ph = _font(36, False)
            d.text((W / 2, cover_y + cover_h / 2), "封面获取失败", font=f_ph, fill=MUTED, anchor="mm")

        # ---- 统计文字
        y = cover_y + cover_h + 40
        for line in body_lines:
            d.text((PAD, y), line, font=f_body, fill=INK)
            y += 62

        # ---- 原链接（蓝色 + 下划线）
        y += 26
        d.text((PAD, y), link_s, font=f_link, fill=LINK_BLUE)
        lw = _text_w(d, link_s, f_link)
        d.line([PAD, y + 48, PAD + lw, y + 48], fill=LINK_BLUE, width=3)

        img = _rounded(img, 36)
        path = _out_path(out_dir, f"bili_{info.get('bvid') or 'video'}")
        img.save(path)
        return path
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"B 站卡片渲染失败: {type(e).__name__}: {e}")
        return None


def _fmt_count(v) -> str:
    """数字缩写：899 -> 899，2400 -> 2.4k，120000 -> 12w。"""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return "—"
    if v < 0:
        return "—"
    if v < 1000:
        return str(v)
    if v < 10000:
        s = f"{v / 1000:.1f}k"
        return s.replace(".0k", "k")
    s = f"{v / 10000:.1f}w"
    return s.replace(".0w", "w")


def render_generic_card(info: dict, out_dir: str, log=None) -> str | None:
    """通用网页卡片（og 标签）：标题 + 封面(可选) + 描述(可选) + 站名/链接。"""
    try:
        from PIL import Image, ImageDraw

        W = 920
        PAD = 64
        inner = W - PAD * 2
        cache_dir = os.path.join(out_dir, "assets")

        title = str(info.get("title") or "(无标题)")
        desc = str(info.get("desc") or "")
        site = str(info.get("site") or "")
        link = str(info.get("url") or "")

        # 离屏量行数
        probe = Image.new("RGBA", (10, 10))
        pd = ImageDraw.Draw(probe)
        lines, f_title = _wrap2(pd, title, inner, 48, True)
        title_h = len(lines) * 64 + (18 if len(lines) > 1 else 0)
        desc_lines: list = []
        f_desc = _font(38, False)
        desc_h = 0
        if desc:
            desc_lines, f_desc = _wrap2(pd, desc, inner, 38, False)
            desc_h = len(desc_lines) * 54 + 26

        cover_w, cover_h = inner, int(inner * 9 / 16)
        has_cover = bool(info.get("cover"))
        link_s, f_link = _fit_font(pd, link, inner, 38, False, 24)

        H = 52 + title_h + (26 + cover_h if has_cover else 0) + desc_h + 26 + 58 + 48
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, W - 1, H - 1], 36, fill=(255, 255, 255, 255))

        # 标题
        y = 52
        for line in lines:
            d.text((PAD, y), line, font=f_title, fill=INK)
            y += 64
        y += 18 if len(lines) > 1 else 0

        # 封面（og:image，可选）
        if has_cover:
            y += 26
            cover = _load_cached_image(info.get("cover") or "", cache_dir)
            if cover is not None:
                cv = _fit_cover(cover, cover_w, cover_h)
                cv_r = _rounded(cv, 24)
                img.paste(cv_r, (PAD, y), cv_r)
            else:
                d.rounded_rectangle(
                    [PAD, y, PAD + cover_w, y + cover_h], 24, fill=_hex(PLACEHOLDER) + (255,)
                )
            y += cover_h

        # 描述（可选）
        if desc_lines:
            y += 26
            for line in desc_lines:
                d.text((PAD, y), line, font=f_desc, fill=MUTED)
                y += 54

        # 底部：站名 + 链接
        y += 26
        f_site = _font(34, False)
        if site:
            d.text((PAD, y), site, font=f_site, fill=MUTED)
        d.text((PAD, y + 52), link_s, font=f_link, fill=LINK_BLUE)
        lw = _text_w(d, link_s, f_link)
        d.line([PAD, y + 100, PAD + lw, y + 100], fill=LINK_BLUE, width=3)

        img = _rounded(img, 36)
        key = hashlib.md5(link.encode()).hexdigest()[:10]
        path = _out_path(out_dir, f"web_{key}")
        img.save(path)
        return path
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"通用网页卡片渲染失败: {type(e).__name__}: {e}")
        return None


def render_card(info: dict, out_dir: str, log=None) -> str | None:
    """按 info['kind'] 分发。"""
    if not fonts_ready():
        if log:
            log("系统里找不到含中文字形的字体，跳过出图（回退文本）")
        return None
    if info.get("kind") == "github":
        return render_github_card(info, out_dir, log)
    if info.get("kind") == "bilibili":
        return render_bili_card(info, out_dir, log)
    if info.get("kind") == "generic":
        return render_generic_card(info, out_dir, log)
    return None
