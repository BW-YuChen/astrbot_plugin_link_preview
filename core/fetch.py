# -*- coding: utf-8 -*-
"""
链接预览机器人 · 数据抓取层

支持两类链接（后续要扩平台就往 parse_target / _FETCH 里加）：

  * GitHub 仓库   github.com/{owner}/{repo}（仓库根；带多余路径时自动归到仓库）
  * B 站视频      www.bilibili.com/video/BVxxxx / avxxxx、m.bilibili.com 同理、
                  b23.tv 短链（跟随 302 解析出真实 BV 号）

接口口径：

  * GitHub    api.github.com/repos/{owner}/{repo}（匿名 60 次/小时/IP，
              所以缓存要给足，contributors 单独缓存 6 小时）
  * B 站       api.bilibili.com/x/web-interface/view（需要 UA + Referer，
              缺了会吃 -412 风控）

出参统一是 info dict，抓取失败返回 (None, 原因)。全模块无 raise —— 链接
预览是「锦上添花」型功能，炸了也不能把消息流带崩。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 10
MAX_BYTES = 8 * 1024 * 1024

# 直连 opener（不读系统代理），与竞赛日报插件保持一致
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---------------------------------------------------------------- 链接识别

RE_GITHUB = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
RE_BILI_BV = re.compile(r"BV[0-9A-Za-z]{10}")
RE_BILI_AV = re.compile(r"(?:https?://)?(?:www\.|m\.)?bilibili\.com/video/av(\d+)", re.IGNORECASE)
RE_BILI_SHORT = re.compile(r"(?:https?://)?b23\.tv/([0-9A-Za-z]+)", re.IGNORECASE)
# 通用网页兜底：任何 http(s) 链接（特殊平台先匹配，剩下的都算它）
RE_ANY_URL = re.compile(r"https?://[^\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+", re.IGNORECASE)

# 这些扩展名是文件不是网页，通用卡片不去抓（下载个 2GB 的 zip 就闹笑话了）
_GENERIC_SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp",
    ".pdf", ".zip", ".rar", ".7z", ".tar", ".gz", ".mp3", ".mp4", ".flv",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".exe", ".apk", ".dmg",
}

# github.com 的这些第二段不是仓库名（/orgs/xxx、/topics/xx、/settings/...），
# 不要当仓库去打 API，白吃配额
_GITHUB_NON_REPO = {
    "orgs", "topics", "settings", "features", "pricing", "sponsors",
    "collections", "trending", "marketplace", "explore", "notifications",
    "account", "apps", "searches", "readmes", "about", "site", "security",
}

# GitHub 匿名配额只有 60 次/小时， contributors 之外的数据一起缓存更久
CACHE_TTL_REPO = 600        # 仓库主数据 10 分钟
CACHE_TTL_CONTRIB = 6 * 3600  # 贡献者数 6 小时（变化慢，且是第二个请求）
CACHE_TTL_BILI = 600        # B 站视频 10 分钟（播放点赞这些短时间内会变，但没必要追实时）

_MEM_CACHE: dict = {}  # {key: (到期时间戳, info)}


def set_cache_ttl(minutes) -> None:
    """按配置项 `cache_minutes` 调整接口结果缓存时长（分钟）。

    默认 10 分钟；贡献者数变化慢，按同一比例放大（10 分钟 : 6 小时 = 1 : 36），
    保持「主数据短、附属数据长」的既有关系。传入非法值时静默保持默认。
    """
    global CACHE_TTL_REPO, CACHE_TTL_CONTRIB, CACHE_TTL_BILI
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return
    if m <= 0:
        return
    m = min(max(m, 1.0), 24 * 60)   # 1 分钟 ~ 1 天，避免误填成 0 或几万
    CACHE_TTL_REPO = m * 60
    CACHE_TTL_BILI = m * 60
    CACHE_TTL_CONTRIB = m * 60 * 36


def _mem_get(key: str):
    hit = _MEM_CACHE.get(key)
    if not hit:
        return None
    expires, data = hit
    if time.time() > expires:
        _MEM_CACHE.pop(key, None)
        return None
    return data


def _mem_put(key: str, data, ttl: float) -> None:
    # 缓存做个体量上限，防止长期运行里 key 无限膨胀
    if len(_MEM_CACHE) > 300:
        for k in list(_MEM_CACHE)[: 100]:
            _MEM_CACHE.pop(k, None)
    _MEM_CACHE[key] = (time.time() + ttl, data)


def _get_json(url: str, headers: dict | None = None):
    """GET 一个 JSON 接口。返回 (parsed|None, err_text|None, response_headers)。"""
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with OPENER.open(req, timeout=TIMEOUT) as r:
            raw = r.read(MAX_BYTES)
            return json.loads(raw.decode("utf-8", "replace")), None, dict(r.headers)
    except urllib.error.HTTPError as e:
        # 403 通常是配额用完 / 风控，404 是对象不存在 —— 都不重试
        detail = ""
        try:
            detail = e.read(500).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return None, f"HTTP {e.code} {url} {detail[:120]}", {}
    except Exception as e:  # noqa: BLE001  超时 / DNS / TLS 抖动
        return None, f"{type(e).__name__}: {e} ({url})", {}


def _resolve_short(url: str) -> str:
    """跟随 b23.tv 短链的 302，返回最终地址。失败返回空串。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with OPENER.open(req, timeout=TIMEOUT) as r:
            r.read(2048)  # 不读完也行，urllib 已跟着重定向到底
            return str(r.geturl() or "")
    except Exception:  # noqa: BLE001
        return ""


def parse_target(text: str):
    """从消息文本里找出第一个「本插件认得」的链接。

    返回 target 元组，无法识别返回 None：
      * ("github", (owner, repo))       —— github.com 仓库
      * ("bili_bv", bvid) / ("bili_av", aid) / ("bili_short", 短链)
      * ("generic", url)                —— 其他任意网页（og 标签出通用卡片）
    """
    text = str(text or "")
    # 短链优先：它最短，先解析避免被别的正则抢走（b23.tv 不会同时含 BV）
    m = RE_BILI_SHORT.search(text)
    if m:
        return ("bili_short", "https://b23.tv/" + m.group(1))
    m = RE_BILI_AV.search(text)
    if m:
        return ("bili_av", m.group(1))
    m = RE_BILI_BV.search(text)
    if m:
        return ("bili_bv", m.group(0))
    m = RE_GITHUB.search(text)
    if m:
        owner, repo = m.group(1), m.group(2)
        if owner.lower() in _GITHUB_NON_REPO:
            return None
        repo = re.sub(r"\.(git|md|html?)$", "", repo, flags=re.IGNORECASE)
        return ("github", (owner, repo))
    # 都不是 -> 通用网页（github.io / 博客 / 文档站……）
    m = RE_ANY_URL.search(text)
    if m:
        url = m.group(0).rstrip(".,;!)\"'")
        path = url.split("?")[0].split("#")[0]
        if os.path.splitext(path)[1].lower() in _GENERIC_SKIP_EXT:
            return None  # 直链文件，不抓
        return ("generic", url)
    return None


# ---------------------------------------------------------------- GitHub

def _github_scrape_page(owner: str, repo: str):
    """GitHub 接口不可用时的网页兜底。

    最常见触发场景：共享机房 IP 把匿名接口的 60 次/小时配额吃光（403），
    或 api.github.com 被 DNS 污染 / 连接重置。仓库页面的计数元素 id 多年
    稳定，直接按 id 抠 title 属性即可，不需要登录与配额。
    抠不到的字段置 None（卡片上显示 —）。
    """
    req = urllib.request.Request(
        f"https://github.com/{owner}/{repo}",
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with OPENER.open(req, timeout=TIMEOUT) as r:
            raw = r.read(3 * 1024 * 1024)
    except Exception as e:  # noqa: BLE001
        return None, f"网页兜底也失败: {type(e).__name__}: {e}"

    if raw[:2] == b"\x1f\x8b":
        import gzip

        try:
            raw = gzip.decompress(raw)
        except Exception:  # noqa: BLE001
            pass
    html = raw.decode("utf-8", "replace")
    if "Page not found" in html or '"status":"404"' in html:
        return None, "仓库不存在（404 页面）"

    def _meta(prop: str) -> str:
        m = re.search(
            rf'<meta[^>]+property="og:{prop}"[^>]+content="([^"]*)"', html
        ) or re.search(
            rf'<meta[^>]+content="([^"]*)"[^>]+property="og:{prop}"', html
        )
        return (m.group(1) if m else "").strip()

    def _counter(element_id: str, json_key: str):
        """按元素 id 抠计数（title 属性里是精确值，带千分位逗号）。

        GitHub 改版频繁，所以两条路都试：① 元素 id（多年稳定）
        ② 页面内嵌 JSON 的字段名（react-app.embeddedData）。都拿不到返回 None。
        """
        for pat in (
            rf'id="{element_id}"[^>]*?title="([\d,]+)"',
            rf'title="([\d,]+)"[^>]*?id="{element_id}"',
            rf'"{json_key}":\s*(\d+)',
        ):
            m = re.search(pat, html)
            if m:
                try:
                    return int(m.group(1).replace(",", ""))
                except ValueError:
                    continue
        return None

    # 标题：og:title 形如 "GitHub - owner/repo: 描述"
    otitle = _meta("title")
    m = re.search(r"GitHub\s*-\s*([^/\s]+)/([^:\s]+)", otitle)
    if not m:
        return None, "网页里抠不到仓库信息（可能被拦截）"
    real_owner, real_repo = m.group(1), m.group(2)

    m_av = re.search(r'(https://avatars\.githubusercontent\.com/u/\d+[^"\'\s]*)', html)
    info = {
        "kind": "github",
        "owner": real_owner,
        "repo": real_repo,
        "avatar": m_av.group(1) if m_av else "",
        # 字段名按 2026-10 实测确定，见下方常量（fork 那个**不是** forkCount）；
        # issues 在当前页面首屏 JSON 里没有（前端异步渲染），抠不到就显示 —
        "stars": _counter("repo-stars-counter-star", "stargazerCount"),
        "forks": _counter("repo-network-counter", "forksCount"),
        "issues": _counter("issues-repo-tab-count", "openIssuesCount"),
        "contributors": None,
        "url": f"https://github.com/{real_owner}/{real_repo}",
        "desc": _meta("description"),
    }
    return info, ""


def fetch_github_repo(owner: str, repo: str, log=None):
    """抓 GitHub 仓库卡片数据。返回 (info|None, err)。"""
    key = f"gh:{owner}/{repo}".lower()
    cached = _mem_get(key)
    if cached is not None:
        return dict(cached), ""

    data, err, _ = _get_json(f"https://api.github.com/repos/{owner}/{repo}")
    if err or not isinstance(data, dict):
        # ① 接口不通（限额 403 / DNS / 超时）-> 网页兜底
        if log:
            log(f"GitHub 接口不可用（{err}），改走网页兜底")
        info, serr = _github_scrape_page(owner, repo)
        if info:
            _mem_put(key, info, CACHE_TTL_REPO)
            return dict(info), ""
        return None, f"GitHub API: {err or '响应异常'}；{serr}"
    if data.get("message") and (
        "Not Found" in str(data.get("message")) or "rate limit" in str(data.get("message")).lower()
    ):
        # ② 接口能连但仓库不存在 / 配额用尽 -> 也试网页（配额耗尽时页面通常还能看）
        info, serr = _github_scrape_page(owner, repo)
        if info:
            _mem_put(key, info, CACHE_TTL_REPO)
            return dict(info), ""
        return None, f"GitHub API: {data.get('message')}；{serr}"

    info = {
        "kind": "github",
        "owner": data.get("owner", {}).get("login", owner) or owner,
        "repo": data.get("name", repo) or repo,
        "avatar": data.get("owner", {}).get("avatar_url", "") or "",
        "stars": int(data.get("stargazers_count") or 0),
        "forks": int(data.get("forks_count") or 0),
        "issues": int(data.get("open_issues_count") or 0),
        "url": data.get("html_url") or f"https://github.com/{owner}/{repo}",
        "desc": str(data.get("description") or ""),
        "contributors": None,
    }

    # 贡献者数：单独缓存。per_page=1 时 Link 响应头里 rel="last" 的页码就是总数，
    # 一次请求搞定大仓库（Star 数万的仓库也只回 1 条记录）
    ckey = f"gh-contrib:{owner}/{repo}".lower()
    ccached = _mem_get(ckey)
    if ccached is not None:
        info["contributors"] = ccached
    else:
        cdata, cerr, cheaders = _get_json(
            f"https://api.github.com/repos/{owner}/{repo}/contributors?per_page=1&anon=1"
        )
        count = None
        if not cerr and isinstance(cdata, list):
            link = str(cheaders.get("Link") or "")
            m = re.search(r'[?&]page=(\d+)>;\s*rel="last"', link)
            if m:
                count = int(m.group(1))
            else:
                count = len(cdata)  # 没有分页头 = 就这么多
        if count is not None:
            info["contributors"] = count
            _mem_put(ckey, count, CACHE_TTL_CONTRIB)
        else:
            if log:
                log(f"贡献者数获取失败（不影响出图，位置留空）: {cerr}")

    _mem_put(key, info, CACHE_TTL_REPO)
    return dict(info), ""


# ---------------------------------------------------------------- B 站

# B 站风控的 UA 画像（本插件自己的对照实验结论）：
#   裸 curl / UA=test-client / UA=curl/8.0  -> 200
#   Chrome 风格 UA（Mozilla/5.0 ... Chrome/xxx）-> **稳定 412**
# 即「浏览器型 UA + 非浏览器请求环境」会被判定为伪造，机房 IP 上尤其明显。
# 所以 B 站接口先试朴素 UA，Chrome UA 只作为最后兜底；并把成功的档位记下来，
# 之后直接用，不用每次试错。
#
# ⚠️ 本插件只取「元数据 + 封面图」：视频信息 + 1 张封面缩略图。
#    不碰播放地址 / CDN 分片，任何情况下都不会下载视频本体
#    （服务器磁盘与内存不允许，也违反使用条款）。
_BILI_UA_PROFILES = (
    "linkpreview/1.0 (astrbot plugin)",   # 自报家门，最朴素
    "curl/8.4.0",
    UA,                                    # Chrome 风格，最后兜底
)
_bili_ua_idx = 0  # 上次成功的 UA 档位
_BUVID_TTL = 24 * 3600
_BUVID: dict = {"v": None, "ts": 0.0}


def _bili_headers(cookie: str = "", ua: str = "") -> dict:
    h = {
        "User-Agent": ua or _BILI_UA_PROFILES[0],
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


def _get_buvid() -> str:
    if _BUVID["v"] and time.time() - _BUVID["ts"] < _BUVID_TTL:
        return _BUVID["v"]
    v = ""
    try:
        data, err, _ = _get_json(
            "https://api.bilibili.com/x/frontend/finger/spi", headers=_bili_headers()
        )
        if isinstance(data, dict) and data.get("code") == 0:
            d = data.get("data") or {}
            v = str(d.get("b_3") or "")
    except Exception:  # noqa: BLE001
        v = ""
    if not v:
        v = str(uuid.uuid4()).upper()
    _BUVID.update(v=v, ts=time.time())
    return v


def _bili_api_get(api: str, log=None):
    """带 cookie 调 B 站接口：按 UA 档位逐个试（朴素 UA 优先），成功的记下来。

    只在 412（风控）时换下一档；404 / 网络错这类换 UA 也没用，直接返回。
    """
    global _bili_ua_idx
    order = list(range(len(_BILI_UA_PROFILES)))
    if _bili_ua_idx in order:  # 上次成功的档位优先
        order.remove(_bili_ua_idx)
        order.insert(0, _bili_ua_idx)

    last_err = ""
    for i, idx in enumerate(order):
        ua = _BILI_UA_PROFILES[idx]
        cookie = f"buvid3={_get_buvid()}; buvid4={str(uuid.uuid4()).upper()}"
        data, err, _ = _get_json(api, headers=_bili_headers(cookie, ua))
        if not err and isinstance(data, dict):
            if idx != _bili_ua_idx and log:
                log(f"B 站接口通过（UA 档位 {idx}: {ua[:32]}）")
            _bili_ua_idx = idx
            return data, ""
        last_err = err or "响应异常"
        if "412" not in str(err):
            return None, last_err  # 不是风控，换 UA 无意义
        if log and i + 1 < len(order):
            log(f"B 站接口 412（UA 档位 {idx}），换下一个 UA 重试")
        _BUVID.update(v=None, ts=0.0)  # 连指纹一起换新
    return None, last_err


def _bili_scrape_page(bvid: str):
    """最后兜底：直接抓视频网页，从 og:* meta 与 __INITIAL_STATE__ 里抠字段。

    接口被风控但网页能开的情况下能救回 标题/封面/UP主；播放点赞这些
    抠不到就置 None（卡片上显示 —）。再失败返回 (None, err)。
    """
    req = urllib.request.Request(
        f"https://www.bilibili.com/video/{bvid}/",
        headers={
            # 网页同样先试朴素 UA（浏览器型 UA 在非浏览器环境易被风控 412）
            "User-Agent": _BILI_UA_PROFILES[0],
            "Referer": "https://www.bilibili.com/",
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            # 显式只要 gzip：不声明时 B 站可能回 br，urllib 不会自动解压
            "Accept-Encoding": "gzip",
            "Cookie": f"buvid3={_get_buvid()}",
        },
    )
    raw = b""
    last_err = ""
    for ua in (_BILI_UA_PROFILES[0], UA):  # 朴素 UA 失败再换 Chrome
        req.headers["User-Agent"] = ua
        try:
            with OPENER.open(req, timeout=TIMEOUT) as r:
                raw = r.read(3 * 1024 * 1024)
            break
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            raw = b""
    if not raw:
        return None, f"网页兜底也失败: {last_err}"

    # gzip 手动解压（响应头 Content-Encoding 也可能是漏标的，按魔数判断最稳）
    if raw[:2] == b"\x1f\x8b":
        import gzip

        try:
            raw = gzip.decompress(raw)
        except Exception:  # noqa: BLE001
            pass
    html = raw.decode("utf-8", "replace")

    def _og(prop: str) -> str:
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']og:{prop}["\'][^>]+content=["\']([^"\']+)',
            html,
        ) or re.search(
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:{prop}["\']',
            html,
        )
        return (m.group(1) if m else "").replace("&#39;", "'").replace("&amp;", "&")

    def _json_str(raw: str) -> str:
        """把页面里 JSON 字符串的字面量还原（\\uXXXX / \\" 等）。"""
        try:
            return json.loads('"' + raw + '"')
        except Exception:  # noqa: BLE001
            return raw.replace('\\"', '"').replace("\\/", "/")

    # 标题优先级：① 页面内嵌数据里的原始标题（最干净）
    #            ② og:title（带「_哔哩哔哩_bilibili」后缀，要削）
    #            ③ <title> 标签
    title = ""
    m = re.search(r'"videoData":\{.{0,400}?"title":"((?:[^"\\]|\\.){2,300})"', html, re.S)
    if not m:
        # 兜底：页面里第一个 title 字段（通常是本视频）
        m = re.search(r'"title":"((?:[^"\\]|\\.){2,300})"', html)
    if m:
        title = _json_str(m.group(1)).strip()
        if title.endswith("_哔哩哔哩_bilibili"):
            title = title[: -len("_哔哩哔哩_bilibili")].strip()
    if not title:
        title = _og("title")
        title = re.sub(r"[_\-](哔哩哔哩|bilibili).*$", "", title, flags=re.IGNORECASE).strip()
    if not title:
        m_t = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        if m_t:
            title = re.sub(r"\s+", " ", m_t.group(1)).strip()
            title = re.sub(r"[_\-](哔哩哔哩|bilibili).*$", "", title, flags=re.IGNORECASE).strip()
    if not title:
        return None, "网页里抠不到标题（可能被风控页拦截）"
    m = re.search(r'"owner":\{"mid":\d+,"name":"([^"]+)"', html)
    info = {
        "kind": "bilibili",
        "bvid": bvid,
        "title": title,
        "cover": _og("image"),
        "up": m.group(1) if m else "",
        "view": None,
        "like": None,
        "coin": None,
        "fav": None,
        "url": f"https://www.bilibili.com/video/{bvid}",
    }
    # 网页里也有播放数据：__INITIAL_STATE__ 的第一个 "stat":{...} 就是本视频的
    m_stat = re.search(r'"stat":\{[^{}]*\}', html)
    if m_stat:
        seg = m_stat.group(0)
        for key, field in (("view", "view"), ("like", "like"), ("coin", "coin"), ("favorite", "fav")):
            m_num = re.search(rf'"{key}":(\d+)', seg)
            if m_num:
                info[field] = int(m_num.group(1))
    if info["cover"].startswith("//"):
        info["cover"] = "https:" + info["cover"]
    return info, ""


def fetch_bili_video(key_kind: str, key_val: str, log=None):
    """抓 B 站视频卡片数据。key_kind: bili_bv / bili_av / bili_short。

    只取元数据（标题 / UP主 / 统计 / 封面地址），不碰任何视频流接口。
    """
    # ① 短链先落地成 BV 号（解析失败会走不了下面的接口）
    if key_kind == "bili_short":
        final = _resolve_short(key_val)
        m = RE_BILI_BV.search(final)
        if m:
            key_kind, key_val = "bili_bv", m.group(0)
        else:
            m = RE_BILI_AV.search(final)
            if m:
                key_kind, key_val = "bili_av", m.group(1)
            else:
                return None, f"短链解析失败：{key_val}"

    key = f"bili:{key_kind}:{key_val}"
    cached = _mem_get(key)
    if cached is not None:
        return dict(cached), ""

    if key_kind == "bili_bv":
        api = f"https://api.bilibili.com/x/web-interface/view?bvid={key_val}"
    else:
        api = f"https://api.bilibili.com/x/web-interface/view?aid={key_val}"
    data, err = _bili_api_get(api, log=log)
    if err or not isinstance(data, dict):
        # 接口被风控（412 等）-> 抓视频网页兜底，能救回标题/封面/UP主
        bvid_guess = key_val if key_kind == "bili_bv" else ""
        if bvid_guess:
            info, serr = _bili_scrape_page(bvid_guess)
            if info:
                _mem_put(key, info, CACHE_TTL_BILI)
                return dict(info), ""
            return None, f"B 站接口: {err or '响应异常'}；{serr}"
        return None, f"B 站接口: {err or '响应异常'}"
    if data.get("code") != 0:
        # 62002 稿件不可见 / 62004 审核中 / -404 不存在 / -412 风控
        return None, f"B 站接口返回 code={data.get('code')}：{data.get('message')}"
    d = data.get("data") or {}

    info = {
        "kind": "bilibili",
        "bvid": d.get("bvid") or (key_val if key_kind == "bili_bv" else ""),
        "title": str(d.get("title") or ""),
        "cover": str(d.get("pic") or ""),
        "up": str((d.get("owner") or {}).get("name") or ""),
        "view": int((d.get("stat") or {}).get("view") or 0),
        "like": int((d.get("stat") or {}).get("like") or 0),
        "coin": int((d.get("stat") or {}).get("coin") or 0),
        "fav": int((d.get("stat") or {}).get("favorite") or 0),
        "url": f"https://www.bilibili.com/video/{d.get('bvid') or key_val}",
    }
    # 封面协议相对地址兜底
    if info["cover"].startswith("//"):
        info["cover"] = "https:" + info["cover"]

    _mem_put(key, info, CACHE_TTL_BILI)
    return dict(info), ""


def fetch_card_info(target, log=None):
    """parse_target 的结果 -> 卡片数据。返回 (info|None, err)。"""
    kind, val = target
    try:
        if kind == "github":
            owner, repo = val
            return fetch_github_repo(owner, repo, log=log)
        if kind in ("bili_bv", "bili_av", "bili_short"):
            return fetch_bili_video(kind, val, log=log)
        if kind == "generic":
            return fetch_generic_page(val)
        return None, f"未支持的链接类型：{kind}"
    except Exception as e:  # noqa: BLE001 兜底，绝不让抓取层炸出去
        return None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 通用网页

def fetch_generic_page(url: str, log=None):
    """任意网页的 og 标签抓取（github.io / 博客 / 文档站……）。

    og:title / og:image / og:description / og:site_name，缺啥用 <title> 和
    meta description 顶上。没有 og 标签的页面（多数老网站）只能拿到标题，
    出一张「标题 + 链接」的极简卡，不算失败。
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with OPENER.open(req, timeout=TIMEOUT) as r:
            final_url = str(r.geturl() or url)
            raw = r.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}（页面打不开）"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"

    if raw[:2] == b"\x1f\x8b":
        import gzip

        try:
            raw = gzip.decompress(raw)
        except Exception:  # noqa: BLE001
            pass

    # 编码：先按 utf-8 解；乱码太多再按 meta 里声明的 gbk/gb2312 解
    html = raw.decode("utf-8", "replace")
    head = html[:3000].lower()
    if html.count("\ufffd") > len(html) / 50 and ("gb2312" in head or "gbk" in head):
        html = raw.decode("gbk", "replace")

    def _meta(*names: str) -> str:
        """按 og:xxx / name=xxx 两种写法找 meta，返回第一个非空值。"""
        for name in names:
            m = re.search(
                rf'<meta[^>]+(?:property|name)=["\']{name}["\'][^>]*content=["\']([^"\']*)',
                html,
            ) or re.search(
                rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{name}["\']',
                html,
            )
            if m and m.group(1).strip():
                return (
                    m.group(1)
                    .replace("&#39;", "'")
                    .replace("&quot;", '"')
                    .replace("&amp;", "&")
                    .strip()
                )
        return ""

    title = _meta("og:title") or _meta("twitter:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
    if not title:
        return None, "页面抠不到标题（可能不是网页或被反爬拦截）"

    cover = _meta("og:image") or _meta("twitter:image")
    if cover.startswith("//"):
        cover = "https:" + cover

    info = {
        "kind": "generic",
        "title": title,
        "desc": _meta("og:description") or _meta("description") or "",
        "cover": cover,
        "site": _meta("og:site_name") or urllib.parse.urlsplit(final_url).netloc,
        "url": final_url,
    }
    return info, ""


# ---------------------------------------------------------------- 诊断探针

def probe_network():
    """/链接诊断 用：逐项探测外网链路。返回 [(项目, 是否OK, 详情)]。

    故意每次都真实打网络（不走缓存判断的路径尽量绕开），这样才能反映
    「服务器此刻」的连通性，而不是本机开发环境。
    """
    results = []

    # ① b23.tv 短链解析
    final = _resolve_short("https://b23.tv/9Olx7s7")
    bv = RE_BILI_BV.search(final or "")
    results.append(("b23.tv 短链解析", bool(bv), final[:80] or "无响应"))

    # ② B 站接口（绕开缓存直接打，并报告最终可用的 UA 档位 —— 服务器 412 就靠这条定位）
    bv_id = bv.group(0) if bv else "BV1HKJy61ET8"
    try:
        data, err = _bili_api_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bv_id}")
        if isinstance(data, dict) and data.get("code") == 0:
            t = str((data.get("data") or {}).get("title") or "")[:18]
            ua_used = _BILI_UA_PROFILES[_bili_ua_idx][:26]
            results.append(("B 站接口", True, f"UA 档位 {_bili_ua_idx}「{ua_used}」｜《{t}》"))
        else:
            reason = err or (data or {}).get("message") or "code != 0"
            results.append(("B 站接口", False, str(reason)))
    except Exception as e:  # noqa: BLE001
        results.append(("B 站接口", False, f"{type(e).__name__}: {e}"))

    # ③ GitHub API（rate_limit 不占 core 配额，还能顺便看限额余量）
    data, err, _ = _get_json("https://api.github.com/rate_limit")
    if isinstance(data, dict):
        core = (data.get("resources") or {}).get("core") or {}
        results.append(
            ("GitHub API", True, f"限额余量 {core.get('remaining', '?')}/{core.get('limit', '?')}（每小时重置）")
        )
    else:
        results.append(("GitHub API", False, err or "无响应"))

    # ④ GitHub 网页（接口被限额时靠它兜底，所以单独探一次）
    try:
        req = urllib.request.Request(
            "https://github.com/Hei-MaoM/xcpcrating",
            headers={"User-Agent": UA, "Accept": "text/html", "Accept-Encoding": "gzip"},
        )
        with OPENER.open(req, timeout=8) as r:
            raw = r.read(200 * 1024)
        if raw[:2] == b"\x1f\x8b":
            import gzip

            raw = gzip.decompress(raw)
        html = raw.decode("utf-8", "replace")
        if 'id="repo-stars-counter-star"' in html or '"stargazerCount"' in html:
            results.append(("GitHub 网页兜底", True, "仓库页可访问，Stars/Forks 可读"))
        elif "stargazer" in html or "github" in html.lower():
            results.append(("GitHub 网页兜底", True, "页面可访问（计数没抠到，统计会显示 —）"))
        else:
            results.append(("GitHub 网页兜底", False, "页面内容异常"))
    except Exception as e:  # noqa: BLE001
        results.append(("GitHub 网页兜底", False, f"{type(e).__name__}: {e}"))

    return results
