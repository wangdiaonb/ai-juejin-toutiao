# ============================================================
# 澎湃新闻源：绕过 RSS，直接解析频道页的 __NEXT_DATA__
# ------------------------------------------------------------
# 【为什么需要这个文件？】
#   用户要求「每条新闻都必须带一张来自原文的图」。
#   实测 2026 年中文主流新闻站已经全面停用 RSS：
#     新华网 / 人民网 / 央视网 / 中青网 / 光明网 / 观察者网 / 接口新闻
#     的 RSS 全部 404 或早已下线；新浪的 RSS 内容停在 8 年前。
#   还活着的 RSS（中新网）里，只有约 40% 的文章正文带图，
#   导致「国际」「娱乐」两个分类每天都在 2~4 条徘徊。
#
#   澎湃新闻虽然也没有 RSS，但它是 Next.js 服务端渲染：
#   频道页 HTML 里嵌了一整块 <script id="__NEXT_DATA__"> 的 JSON，
#   里面的 contentList 每条都带 pic 字段 —— **实测带图率 100%**。
#   所以这里直接解析那块 JSON，等价于「把页面当 API 用」。
#
# 【最大的坑：澎湃有腾讯云 WAF】
#   短时间内高频请求会被判定为爬虫，返回 403 + WAF 拦截页。
#   实测并发 16 个请求打过去，几秒钟内整个 IP 就被封（持续十几分钟）。
#   所以本模块做了三层保护：
#     1. 全局节流：任意两次请求之间强制间隔 PP_INTERVAL 秒（串行化）
#     2. 403 退避重试：被拦后等待再试，连续失败则放弃
#     3. 熔断：一次采集里如果澎湃已经连续失败，后续直接跳过，
#              不再浪费时间去撞墙（宁可这个源本轮没数据）
# ============================================================

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from config.fetcher_conf import (
    BROWSER_HEADERS, PP_CHANNEL_SOURCES, PP_FETCH_DETAIL, PP_INTERVAL,
    PP_MAX_CONSECUTIVE_FAILS, REQUEST_TIMEOUT,
)

# 本地时区：东八区（固定偏移，避免 Windows 缺 tzdata）
CST = timezone(timedelta(hours=8))

# Next.js 把服务端数据塞在这个 script 标签里
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# ------------------------------------------------------------
# 全局节流 + 熔断状态
# ------------------------------------------------------------
# 【为什么要用模块级全局变量？】
#   节流必须对「整个进程内所有澎湃请求」生效，不能每个协程各管各的 ——
#   否则 8 个协程并发时每个都认为自己只隔了 2 秒，打过去还是 8 连击。
_throttle_lock: Optional[asyncio.Lock] = None
_last_request_ts = 0.0

# 连续失败计数。达到阈值就熔断本轮采集里的澎湃源。
_consecutive_fails = 0


def _lock() -> asyncio.Lock:
    """惰性创建锁：必须在事件循环里创建，不能在模块导入时创建。"""
    global _throttle_lock
    if _throttle_lock is None:
        _throttle_lock = asyncio.Lock()
    return _throttle_lock


def reset_state() -> None:
    """每轮采集开始时调用，清掉上一轮的熔断状态。"""
    global _consecutive_fails
    _consecutive_fails = 0


def is_circuit_open() -> bool:
    """熔断是否已触发（触发后本轮不再请求澎湃）。"""
    return _consecutive_fails >= PP_MAX_CONSECUTIVE_FAILS


async def _throttled_get(url: str) -> Optional[str]:
    """
    带全局节流和重试的 GET。失败返回 None（不抛异常，让上层跳过）。

    节流原理：进入临界区后，先看「距离上次请求过了多久」，
    不够 PP_INTERVAL 就补睡差额。这样无论多少协程并发调用，
    实际发出的请求都会严格排开 ≥PP_INTERVAL 秒。
    """
    global _last_request_ts, _consecutive_fails

    if is_circuit_open():
        return None

    async with _lock():
        gap = time.monotonic() - _last_request_ts
        if gap < PP_INTERVAL:
            await asyncio.sleep(PP_INTERVAL - gap)

        def _do() -> Optional[str]:
            try:
                resp = requests.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 403:
                    return "__WAF__"          # 特殊标记：被 WAF 拦了
                if resp.status_code != 200:
                    return None
                resp.encoding = resp.apparent_encoding or resp.encoding
                return resp.text
            except Exception as e:
                print(f"  [澎湃] 请求异常 {url[:60]} -> {type(e).__name__}")
                return None

        html = await asyncio.to_thread(_do)
        _last_request_ts = time.monotonic()

    if html == "__WAF__":
        _consecutive_fails += 1
        print(f"  [澎湃] 被 WAF 拦截（连续第 {_consecutive_fails} 次），"
              f"来源：{url[:70]}")
        if is_circuit_open():
            print(f"  [澎湃] 连续失败 {_consecutive_fails} 次，本轮放弃澎湃源")
        return None

    if html is None:
        _consecutive_fails += 1
        return None

    _consecutive_fails = 0        # 成功一次就把失败计数清零
    return html


# ------------------------------------------------------------
# 解析
# ------------------------------------------------------------
def _next_data(html: str) -> Optional[Dict[str, Any]]:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _dig_content(obj: Any, depth: int = 0) -> Optional[str]:
    """
    在 __NEXT_DATA__ 里递归找一个像「正文」的 content 字段。

    为什么要递归找而不是写死路径？
      澎湃详情页的 JSON 层级随页面类型会变（图文稿、视频稿、专题稿结构不同），
      写死 props.pageProps.data.content 很容易一改版就取不到。
      递归找 "content" 字段 + 长度 > 80 的字符串，容错性高得多。
    """
    if depth > 8:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("content", "contBody", "body", "text") \
                    and isinstance(v, str) and len(v) > 80:
                return v
            found = _dig_content(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _dig_content(v, depth + 1)
            if found:
                return found
    return None


def _html_to_text(raw: str) -> str:
    """把详情页正文的 HTML 片段转成纯文本（复用 rss_source 的思路）。"""
    if not raw:
        return ""
    import html as _html
    text = _html.unescape(raw)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    # <p> 段落之间补换行，避免「上一段末尾 + 下一段开头」粘成一个词
    text = re.sub(r"</p>|<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [ln.strip() for ln in text.split("\n")]
    return " ".join(ln for ln in lines if ln)


def _parse_channel(html: str, source_name: str, node_name: str = "") -> List[Dict[str, Any]]:
    """
    解析频道页，返回统一结构的条目列表。

    页面结构（实测）：
      props.pageProps.data.data.list = [ {contId, name, pic, pubTimeLong, ...}, ... ]
    注意有两层 data —— 外层是接口返回信封，内层才是真正的数据。
    """
    data = _next_data(html)
    if not data:
        return []

    page_props = (data.get("props") or {}).get("pageProps") or {}
    inner = page_props.get("data") or {}
    if isinstance(inner, dict) and isinstance(inner.get("data"), dict):
        inner = inner["data"]          # 频道页：拆掉信封
    lst = inner.get("list") or []
    if not isinstance(lst, list):
        return []

    items: List[Dict[str, Any]] = []
    for raw in lst:
        if not isinstance(raw, dict):
            continue
        cont_id = raw.get("contId")
        title = (raw.get("name") or "").strip()
        # pic 是主图；smallPic 是它的缩略版（同一张图），sharePic 是分享封面
        image = raw.get("pic") or raw.get("sharePic")
        if not cont_id or not title or not image:
            continue

        ts = raw.get("pubTimeLong")
        try:
            pub = datetime.fromtimestamp(int(ts) / 1000, CST).replace(tzinfo=None) if ts else None
        except (ValueError, OSError, OverflowError):
            pub = None

        node = raw.get("nodeInfo") or {}
        items.append({
            "title": title,
            "url": f"https://www.thepaper.cn/newsDetail_forward_{cont_id}",
            "raw_text": "",                        # 稍后抓详情页补
            "image": image,
            "author": node.get("name") or node_name or source_name,
            "source": source_name,
            "publish_time": pub,
        })
    return items


def _parse_detail(html: str) -> str:
    """从详情页 HTML 里取正文纯文本，取不到返回空串。"""
    data = _next_data(html)
    if not data:
        return ""
    return _html_to_text(_dig_content(data) or "")


# ------------------------------------------------------------
# 对外入口
# ------------------------------------------------------------
async def fetch_one_channel(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    抓一个澎湃频道（含可选详情页）。

    cfg 结构：{"id": "pp_guoji", "name": "澎湃新闻", "node_id": 25429, "node_name": "全球速报"}
    """
    if is_circuit_open():
        return []

    channel_url = f"https://www.thepaper.cn/channel_{cfg['node_id']}"
    html = await _throttled_get(channel_url)
    if not html:
        print(f"  [澎湃] 频道「{cfg.get('node_name') or cfg['node_id']}」抓取失败")
        return []

    items = _parse_channel(html, cfg["name"], cfg.get("node_name", ""))
    if not items:
        print(f"  [澎湃] 频道「{cfg.get('node_name') or cfg['node_id']}」没解析出条目"
              f"（可能是频道 ID 失效或页面结构变了）")
        return []
    print(f"  [澎湃] 频道「{cfg.get('node_name') or cfg['node_id']}」解析出 {len(items)} 条")

    # 补正文：逐条抓详情页。这里必须串行 —— 详情页也要走同一个节流器，
    # 并发提交只会把请求排在队里，反而增加代码复杂度。
    if PP_FETCH_DETAIL:
        got = 0
        for it in items:
            if is_circuit_open():
                break
            detail_html = await _throttled_get(it["url"])
            if detail_html:
                it["raw_text"] = _parse_detail(detail_html)
                if it["raw_text"]:
                    got += 1
        print(f"  [澎湃] 频道「{cfg.get('node_name') or cfg['node_id']}」"
              f"补到正文 {got}/{len(items)} 条")
    return items


async def fetch_all_pp_sources() -> Dict[str, List[Dict[str, Any]]]:
    """
    抓取全部配置好的澎湃频道，返回 {源 id: [条目]}。

    返回结构刻意和 rss_source.fetch_all_sources 保持一致，
    这样 news_fetcher 里可以把两边的结果直接合并。
    """
    reset_state()
    if not PP_CHANNEL_SOURCES:
        return {}

    print(f"[澎湃] 开始抓取 {len(PP_CHANNEL_SOURCES)} 个频道"
          f"（串行 + 每次间隔 {PP_INTERVAL}s，避免触发 WAF）")

    # 串行：澎湃 WAF 对频率极敏感，并发抓多个频道是最容易封 IP 的做法。
    # 每天只跑一次，慢一点（6 个频道约 1 分钟）完全无所谓。
    data: Dict[str, List[Dict[str, Any]]] = {}
    for cfg in PP_CHANNEL_SOURCES:
        data[cfg["id"]] = await fetch_one_channel(cfg)

    total = sum(len(v) for v in data.values())
    print(f"[澎湃] 抓取结束：共 {total} 条")
    return data
