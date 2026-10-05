# ============================================================
# 新浪滚动新闻源
# ------------------------------------------------------------
# 【为什么不用 RSS？】
#   新浪确实还挂着 RSS，但内容停在很多年前（实测首条 pubDate 是 8 年前），
#   是典型的「假活着」——HTTP 200、XML 格式合法，就是没有新内容。
#   所以这里直接用它网页上真实在用的滚动接口。
#
# 【这个源长什么样】
#   GET https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&num=50&page=1
#   返回 JSON：result.data 是一个数组，每条含
#     title   标题
#     url     文章链接
#     ctime   Unix 秒级时间戳（北京时间）
#     intro   站点写的导语
#     images  [{u: 图片地址}]  —— 实测就是文章正文的第一张图
#   一次最多给 50 条。
#
# 【它解决的问题】
#   中新网「国际/娱乐」两个频道以编译短讯为主，文章页本来就没有配图，
#   在 REQUIRE_ORIGINAL_IMAGE=True 下 30 条只过得了 7 条，
#   分类经常凑不满 10 条。新浪滚动流更新快（约 5 分钟一批）、不封 IP，
#   正好把缺的条数补上。
#
# 【和澎湃源的区别（为什么澎湃要限速、新浪不用）】
#   澎湃挂了腾讯云 WAF，请求稍密就 403 封 IP；
#   新浪这个接口是公开的滚动接口，实测并发抓几十次都正常，
#   所以这里可以放心用并发。
# ============================================================

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from config.fetcher_conf import SINA_DETAIL_CONCURRENCY, SINA_ROLL_SOURCES
from services.rss_source import extract_body_text, extract_page_images

ROLL_URL = ("https://feed.mix.sina.com.cn/api/roll/get"
            "?pageid=153&lid={lid}&k=&num={num}&page=1")

# 伪装成普通桌面浏览器：不带的裸请求会被新浪网关直接拒掉
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://news.sina.com.cn/",
    "Accept": "application/json, text/plain, */*",
}


def _get_text(url: str, timeout: int = 20) -> Optional[str]:
    """同步 GET，返回文本；任何异常都吞掉返回 None（单个源失败不影响整体）。"""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        if resp.status_code != 200:
            return None
        # 新浪这个接口有时不带 charset，手动兜一下，避免中文乱码
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except Exception:
        return None


def _parse_roll(payload: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    把滚动接口的 JSON 转成项目统一的 item 结构。

    image 先留空 —— 列表接口给的图虽然一般就是正文首图，
    但为了和其它源保持一致（统一由「提图 + 跨条目去重」这一步裁决），
    这里不预设，交给下面的 _enrich_one 从文章页正式提取。
    """
    try:
        data = json.loads(payload)
    except Exception:
        return []

    rows = (data.get("result") or {}).get("data") or []
    if not isinstance(rows, list):
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        title = (row.get("title") or "").strip()
        url = (row.get("url") or "").strip()
        if not title or not url.startswith("http"):
            continue

        pub: Optional[datetime] = None
        ts = row.get("ctime")
        if ts:
            try:
                pub = datetime.fromtimestamp(int(ts))
            except (TypeError, ValueError, OSError):
                pub = None

        out.append({
            "title": title,
            "url": url,
            "raw_text": "",                  # 下面抓文章页补
            "source": cfg["name"],
            "image": None,                   # 下面抓文章页提
            "publish_time": pub,
            "author": (row.get("author") or "").strip(),
        })
    return out


async def _enrich_one(item: Dict[str, Any], sem: asyncio.Semaphore) -> None:
    """
    抓文章页，就地补上 raw_text（正文）和 image（正文首图）。

    为什么正文和图片要一次抓完？
      两者都在同一个 HTML 里。分两次抓等于把同一个页面下载两遍，
      50 条候选就是 100 次请求，纯属浪费。
    """
    async with sem:
        html = await asyncio.to_thread(_get_text, item["url"])
    if not html:
        return
    # 只取正文容器内的段落（页脚的「相关推荐」也是 <p>，不能要）
    item["raw_text"] = extract_body_text(html)
    cands = extract_page_images(html, item["url"])
    if cands:
        # 第一张是正文首图；如果它是站点通用图，后面的跨条目去重会把它换掉
        item["image"] = cands[0]
        item["_page_cands"] = cands       # 留作候选，供去重时换图用


async def fetch_one_roll(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """抓一个 lid 的滚动列表，并并发补全每条的正文与配图。"""
    url = ROLL_URL.format(lid=cfg["lid"], num=cfg.get("num", 50))
    payload = await asyncio.to_thread(_get_text, url)
    if not payload:
        print(f"  [新浪] {cfg['id']} 列表抓取失败")
        return []

    items = _parse_roll(payload, cfg)
    if not items:
        print(f"  [新浪] {cfg['id']} 解析出 0 条")
        return []

    sem = asyncio.Semaphore(SINA_DETAIL_CONCURRENCY)
    await asyncio.gather(*(_enrich_one(it, sem) for it in items))

    got_text = sum(1 for it in items if len(it["raw_text"]) >= 60)
    got_img = sum(1 for it in items if it["image"])
    print(f"  [新浪] {cfg['id']}({cfg['name']}) 抓回 {len(items)} 条，"
          f"补到正文 {got_text} 条，提到图 {got_img} 条")
    return items


async def fetch_all_sina_rolls() -> Dict[str, List[Dict[str, Any]]]:
    """
    抓取全部新浪滚动源，返回 {source_id: [items]}，键名和 CATEGORY_RULES 里引用的一致。

    新浪源在 news_fetcher 里是**串行于 RSS 之后**调用的：
      - 它自己内部把 50 条详情页用并发跑满（8 并发），已经够快；
      - 和 RSS 的并发混在一起会让瞬时连接数过高，分开更稳。
    """
    result: Dict[str, List[Dict[str, Any]]] = {}
    for cfg in SINA_ROLL_SOURCES:
        try:
            result[cfg["id"]] = await fetch_one_roll(cfg)
        except Exception as e:
            # 一个源挂了不能拖垮整轮采集
            print(f"  [新浪] {cfg['id']} 异常，已跳过：{type(e).__name__}: {str(e)[:80]}")
            result[cfg["id"]] = []
    return result
