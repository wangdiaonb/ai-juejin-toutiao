# ============================================================
# 采集服务：抓 RSS、解析成统一结构、按分类规则筛选
# ------------------------------------------------------------
# 这个文件只做三件事：
#   1. 把 RSS 地址「下载」回来（网络 IO）
#   2. 把下载到的 XML「解析」成 Python 字典列表（数据加工）
#   3. 按关键词「筛选」，判断一条新闻属于哪个分类（规则判断）
#
# 它不碰数据库，也不调用大模型——那些是 news_fetcher.py 的事。
#
# 关于 RSS：它是一种标准 XML 格式，结构长这样
#   <rss><channel>
#       <item>
#           <title>标题</title>
#           <link>原文链接</link>
#           <pubDate>发布时间</pubDate>
#           <description><![CDATA[<p>带 HTML 的摘要</p>]]></description>
#       </item>
#   </channel></rss>
# 所以标准库的 ElementTree 就能解析，不需要额外装库。
# ============================================================

import asyncio                      # 把同步的 requests 丢进线程池，避免卡住事件循环
import html                         # html.unescape：把 &lt; 这类实体还原成 <
import re                           # 正则：去 HTML 标签、抽图片、匹配关键词
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime   # 解析 RFC822 时间（Mon, 05 Oct 2026 ...）
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree                # 标准库 XML 解析器

import requests

from config.fetcher_conf import (
    AI_KEYWORDS_STRONG, AI_KEYWORDS_TITLE_ONLY, BROWSER_HEADERS,
    DOMESTIC_KEYWORDS, ENTERTAINMENT_KEYWORDS, MIN_RAW_TEXT_LEN,
    REQUEST_TIMEOUT, SHARED_CLASSIFY_ORDER, SHARED_DEFAULT_CATEGORY,
    SOCIETY_KEYWORDS, TECH_KEYWORDS, WORLD_KEYWORDS,
)

# 每个源最多解析多少条。IT之家的 feed 有 200+ 条、体积 200KB+，
# 全量解析又慢又没必要（我们只要当天的），所以设个上限。
MAX_ITEMS_PER_SOURCE = 40

# 本地时区：东八区。用固定偏移而不是 zoneinfo，避免 Windows 上缺 tzdata 报错
CST = timezone(timedelta(hours=8))


# ------------------------------------------------------------
# 一、网络层：下载
# ------------------------------------------------------------
def _http_get_bytes(url: str) -> Optional[bytes]:
    """
    同步下载原始字节。失败返回 None（不抛异常，让上层跳过这个源）。

    【为什么返回 bytes 而不是 str？】
      RSS 的 XML 声明里写了编码（<?xml version="1.0" encoding="gb2312"?>），
      交给 ElementTree 解析时它会自己按声明解码，最准确。
      如果先转成 str，就得靠 requests 猜编码 —— 实测它会猜错：
      新浪返回的是 UTF-8，却被猜成 ISO-8859-1，正文全变乱码。
      所以「原始字节一路传到底」，不要在中间做解码。
    """
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
        # raise_for_status：把 4xx/5xx 变成异常，交给下面的 except 统一处理
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"  [采集] 抓取失败 {url} -> {type(e).__name__}: {e}")
        return None


def _http_get_text(url: str) -> Optional[str]:
    """
    同步下载文本（用于抓新闻网页，不是 RSS）。

    网页是 HTML、没有 XML 声明那种可靠的编码提示，所以这里用
    requests 的 apparent_encoding（基于内容猜测）。
    """
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding
        return resp.text
    except Exception as e:
        print(f"  [采集] 抓网页失败 {url} -> {type(e).__name__}: {e}")
        return None


async def fetch_feed(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    异步抓取并解析单个源。

    requests 是「同步阻塞」的库：直接 await 不了，如果在协程里直接调用，
    整个事件循环会被卡住（所有 HTTP 请求排队）。所以用 asyncio.to_thread
    把它丢到线程池 —— 这是在不引入 httpx/aiohttp 的前提下，
    让同步库安全跑在异步项目里的标准做法。
    """
    raw = await asyncio.to_thread(_http_get_bytes, source["url"])
    if not raw:
        return []
    try:
        return parse_feed(raw, source["name"])
    except ElementTree.ParseError as e:
        # 有些站会返回一段 HTML 错误页而不是 XML，这里会抛解析错误
        print(f"  [采集] XML 解析失败 {source['name']} -> {e}")
        return []


# 网页正文里的噪声段落特征：页脚版权、推广话术等，提取正文时直接排除
_NOISE_PATTERN = re.compile(
    r"(版权所有|ICP备|免责声明|扫码关注|点击关注|微信公号|微信公众号|商务合作|"
    r"未经授权|转载请注明|投稿|加入我们|关注我们|责任编辑)"
)


def extract_article_text(page_html: str, max_chars: int = 2000) -> str:
    """
    从新闻网页的 HTML 里提取正文纯文本。

    为什么需要它？
      有些站的 RSS 只给标题和一句导语（实测「量子位」每条只有十几个字），
      光靠那句话生成不出像样的摘要，所以正文太短的条目要去原文网页补全。

    提取思路（够用就好，不追求完美）：
      1. 去掉 <script>/<style> 整块
      2. 抓出所有 <p> 段落，丢掉太短的（多半是按钮、标签、导航）
      3. 丢掉版权/推广类噪声段
      4. 拼接后截断到 max_chars —— 我们只要写 100~150 字摘要，2000 字绰绰有余
    """
    if not page_html:
        return ""
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page_html, flags=re.S | re.I)
    parts: List[str] = []
    total = 0
    for raw_p in re.findall(r"<p[^>]*>(.*?)</p>", cleaned, flags=re.S):
        text = _clean_html(raw_p)
        if len(text) < 15:                    # 太短，多半不是正文
            continue
        if _NOISE_PATTERN.search(text):       # 版权、推广等噪声
            continue
        parts.append(text)
        total += len(text)
        if total >= max_chars:                # 攒够了就停，不必解析完整页
            break
    return " ".join(parts)[:max_chars]


async def fetch_full_text(url: str) -> str:
    """抓取原文网页并提取正文。任何失败都返回空字符串（调用方保持原样即可）。"""
    page = await asyncio.to_thread(_http_get_text, url)
    if not page:
        return ""
    return extract_body_text(page)


def extract_body_text(page_html: str, max_chars: int = 2000) -> str:
    """
    只从**正文容器**里取段落文本（比 extract_article_text 更准）。

    和 extract_article_text 的区别：
      extract_article_text 扫的是全页的 <p> —— 页面底部的「相关推荐」
      也是一堆 <p>，会被当成正文一起抓进来，摘录里就混进别的新闻的标题。
      本函数先把范围限制在正文容器内，再在「正文结束标志」处截断
      （和提图用的是同一套容器/结束标志，见 _CONTENT_EXACT、_BODY_END），
      所以拿到的段落真的是这篇文章的。

    用于：RSS 只给标题、必须去原文补正文的源（新浪滚动源就是这种）。
    """
    if not page_html:
        return ""
    frag = _content_slice(page_html, _CONTENT_EXACT, stop=_BODY_END)
    if not frag:
        frag = _content_slice(page_html, _CONTENT_LOOSE, stop=_BODY_END)
    if not frag:
        frag = page_html          # 容器都没匹配上，退回全页（总比没有强）
    return extract_article_text(frag, max_chars)


# ------------------------------------------------------------
# 二、解析层：XML → 结构化字典
# ------------------------------------------------------------
def _clean_html(raw: str) -> str:
    """把带 HTML 标签的摘要清理成纯文本。"""
    if not raw:
        return ""
    text = html.unescape(raw)                                          # 还原 &nbsp; &lt;
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>", " ", text, flags=re.I)     # 换行标签 → 空格
    text = re.sub(r"<[^>]+>", "", text)                                # 去掉剩余标签
    return re.sub(r"\s+", " ", text).strip()                           # 压缩连续空白


def _extract_image(raw: str) -> Optional[str]:
    """从 HTML 摘要里抽第一张图片的地址，抽不到返回 None。"""
    if not raw:
        return None
    # 有些站把图片写在 data-src 里（懒加载），也一并兜住
    m = re.search(r"<img[^>]+(?:src|data-src)=[\"']([^\"']+)[\"']", raw, flags=re.I)
    return m.group(1) if m else None


# ------------------------------------------------------------
# 图片 URL 清洗
# ------------------------------------------------------------
# 实测两个真实脏数据，不修好前端就是一坨破图：
#   1. 界面新闻  https:https://img.jiemian.com/101/...   —— 双协议头
#   2. 环球网    //img.huanqiucdn.cn/dp/api/...          —— 协议相对
# 这两类在浏览器里能不能加载要看运气，统一在入库前归一化成绝对 https 地址。
_DATA_URI = re.compile(r"^data:", re.I)


def normalize_image_url(url: Optional[str], base_url: str = "") -> Optional[str]:
    """
    把各种形态的图片地址归一化成「可直接放进 <img src> 的绝对 URL」。

    处理顺序（顺序不能乱，双协议头要在补 https 之前干掉）：
      "https:https://a/b.jpg"  → https://a/b.jpg     （去重复协议头）
      "//a/b.jpg"              → https://a/b.jpg     （协议相对 → 补 https）
      "/upload/a.jpg"          → 原文域名 + /upload/a.jpg  （站内相对路径）
      "a.jpg"                  → 原文所在目录 + a.jpg
      "http://a/b.jpg"         → 原样
      "data:image/png;base64,..." → None（base64 内联图体积太大，不入库）
    """
    if not url:
        return None
    u = url.strip().strip('"').strip("'")
    if not u or _DATA_URI.match(u):
        return None

    # 双/多协议头：https:https://xxx、http:http://xxx
    u = re.sub(r"^(https?:)+(?=//)", "", u, flags=re.I)
    # 现在如果是 "https:xxx"（协议后没跟 //）也统一
    if re.match(r"^https?://", u, flags=re.I):
        return u
    if u.startswith("//"):
        return "https:" + u

    # 相对路径：用原文链接补全
    if base_url:
        m = re.match(r"(https?://[^/]+)", base_url)
        origin = m.group(1) if m else ""
        if u.startswith("/"):
            return origin + u if origin else None
        # 站内相对路径（如 uploads/a.jpg）→ 拼到原文链接所在目录
        base_dir = base_url.rsplit("/", 1)[0]
        return f"{base_dir}/{u}"
    return None


# ------------------------------------------------------------
# 从原文网页里找配图
# ------------------------------------------------------------
# 为什么需要这一层？
#   实测 18 个源里有 6 个（中新网全家桶、量子位、少数派、开源中国）
#   RSS 里完全不带图，但它们的原文网页 100% 能提取到图。
#   用户要求「图片一定要来自原文」，所以这一步是必需的第二级图片源。
#
# 提取优先级（越是「这篇文章的封面」，排得越前）：
#   og:image → twitter:image → itemprop=image → 正文里第一张合格的 <img>
_OG_PATTERNS = [
    (r"<meta[^>]+property=[\"']og:image(?::url)?[\"'][^>]+content=[\"']([^\"']+)[\"']", "og:image"),
    (r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:image(?::url)?[\"']", "og:image"),
    (r"<meta[^>]+name=[\"']twitter:image(?::src)?[\"'][^>]+content=[\"']([^\"']+)[\"']", "twitter:image"),
    (r"<meta[^>]+itemprop=[\"']image[\"'][^>]+content=[\"']([^\"']+)[\"']", "itemprop"),
]

_IMG_TAG_RE = re.compile(
    r"<img[^>]+(?:data-src|data-original|data-lazy-src|data-echo|src)\s*=\s*[\"']([^\"']+)[\"']",
    re.I,
)

# 明显不是正文配图的（logo、头像、二维码、分享按钮、占位符…）
# 【fileftp 为什么必须挡掉】这是中新网放**站点模板资源**的目录：
#   导航图、栏目标题图、画中画广告位、公众号二维码……
#   它们 URL 里的日期是「模板制作日」，跟文章发布日期毫无关系，
#   比如 /fileftp/2022/04/2022-04-20/U719P4T47D50049F24533DT20220420152844.png
#   会出现在几乎每一篇文章的正文容器里 —— 正是它把配图去重统计搅乱的。
_BAD_IMAGE = re.compile(
    r"(logo|icon|avatar|qrcode|qr_|weixin|weibo|blank|spacer|placeholder|default|"
    r"share|btn|button|arrow|loading|pixel|1x1|sprite|banner_ad|ad_|/ad/|toparr|"
    r"fileftp)",
    re.I,
)

# 只认图片后缀，挡掉 <img src="track.gif?xxx"> 这类统计像素
_IMAGE_EXT = re.compile(r"\.(jpe?g|png|webp|gif|bmp)(\?|#|$)", re.I)

# ------------------------------------------------------------
# 正文容器的定位（提图准确率的关键）
# ------------------------------------------------------------
# 【为什么必须限定正文容器？】
#   踩过一个大坑：一开始在全页找 img，结果中新网提回来的全是
#   「侧栏推荐位」「导航箭头」「站点默认图」——
#     toparr.png（箭头）
#     fileftp/2022/05/...（侧栏推荐新闻的缩略图，每页都一样）
#     fileftp/2025/07/...U947P4T47D55580F24532DT...png（og:image 默认图）
#   而真正的文章配图在 <div class="left_zw"> 里，长这样：
#     //i2.chinanews.com.cn/simg/ypt/2026/261005/437a116c-...._zsite.jpg
#   注意路径里的日期就是发布当天 —— 一眼能看出是这篇文章自己的图。
#
# 【为什么要分「精确」和「宽泛」两级？—— 第二次踩坑的教训】
#   第一版把 main_content / content 这类也当成正文容器，结果中新网的
#   <div class="main_content w1280"> 是**整页主区**，里面除了正文还有
#   「侧栏推荐位」——而推荐位展示的正是当天别的新闻的配图。
#   于是 A 文章的配图会出现在 B 文章页面的侧栏里，
#   跨文章去重时被判成「通用图」一起干掉，国际/头条/娱乐三个分类
#   的图几乎被清空（只剩 2~4 条有图）。
#   所以：**先只在精确容器（left_zw 这种）里找图**，
#   这样侧栏图根本不会进入候选，也就不会污染去重统计。
_CONTENT_EXACT = re.compile(
    r"<div[^>]+(?:id|class)\s*=\s*[\"'][^\"']*"
    r"(?:left_zw|artibody|article[-_]?content|articleContent|content[-_]?body|"
    r"rich_media_content|post[-_]?content|TRS_Editor|zw[-_]?content|"
    r"detail[-_]?content|news[-_]?content|entry[-_]?content)"
    r"[^\"']*[\"'][^>]*>",
    re.I,
)

# 宽泛容器：可能包含侧栏，只在精确容器找不到图时才用
_CONTENT_LOOSE = re.compile(
    r"<div[^>]+(?:id|class)\s*=\s*[\"'][^\"']*"
    r"(?:main[-_]?content|article|content)"
    r"[^\"']*[\"'][^>]*>",
    re.I,
)


# ------------------------------------------------------------
# 「正文到底在哪里结束」—— 为什么必须知道这个
# ------------------------------------------------------------
# 【第三次踩坑】正文容器 left_zw 里不只有正文：它后面紧跟着「相关推荐位」，
#   推荐位展示的是**当天别的新闻**的配图。之前切片写死 15000 字符，
#   会一路切进推荐区，于是 A 文章页里混进了 B、C、D 文章的配图 →
#   跨条目去重时这些图被判成"多篇共用"→ 全部剔除 → 该条判定为"无原文图"被丢弃。
#   实测国际频道 30 条里只有 4 条能过，就是这么来的。
# 所以：切片必须在正文结束处停下。下面是几个可靠的"正文结束"标志。
_BODY_END = re.compile(
    r"<div[^>]+class\s*=\s*[\"'][^\"']*"
    r"(?:adEditor|article[-_]?editor|left_name|function_code_page|related|recommend|"
    r"article[-_]?share|share[-_]?box|statement)[^\"']*[\"']"
    r"|【?(?:编辑|责任编辑|责编)[:：]"
    r"|<!--\s*正文结束",
    re.I,
)


def _content_slice(page_html: str, anchor: re.Pattern, span: int = 15000,
                   stop: Optional[re.Pattern] = None) -> str:
    """
    截出「正文容器开头往后 span 个字符」的片段，方便在容器内找图。

    为什么用「截片段」而不是正则匹配整个 <div>...</div>？
      因为 HTML 的 div 是嵌套的，正则匹配成对的 </div> 需要递归计数，
      非贪婪匹配会在第一个 </div></div> 就停住，常常只截到半句话。
      正文图片基本都集中在正文开头 15000 字符内，取片段足够且简单可靠。
    找不到容器就返回空串。

    stop 参数：给出「正文结束标志」正则，命中就截断。
      这是为了把正文末尾的「相关推荐位」挡在切片之外 —— 详见 _BODY_END 注释。
    """
    m = anchor.search(page_html)
    if not m:
        return ""
    frag = page_html[m.end(): m.end() + span]
    if stop is not None:
        s = stop.search(frag)
        if s:
            frag = frag[: s.start()]
    return frag


def extract_page_images(page_html: Optional[str], base_url: str = "") -> List[str]:
    """
    从新闻详情页 HTML 里提取**候选配图列表**（按可信度从高到低排序）。

    为什么返回列表而不是一张图？
      因为同一页里混着「文章配图」和「站点通用图」，单看一页分不出来。
      返回全部候选，由上层（news_fetcher._fill_missing_images）
      跨条目统计：**在多篇文章里重复出现的图判为站点通用图并剔除**，
      这样每篇文章就能各自选到属于自己的那张。

    提取优先级（这个顺序是实测反复调整出来的）：
      1. 精确正文容器内的 <img>  ← 最可能是「这篇文章的配图」，且不含侧栏
      2. og:image / twitter:image / itemprop  ← 站方声明的分享封面，也可能是通用图
      3. 宽泛容器 / 全页 <img>     ← 兜底，只剩侧栏图时基本会被去重逻辑淘汰
    """
    if not page_html:
        return []

    out: List[str] = []
    seen = set()

    def _push(u: Optional[str]) -> None:
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    def _scan(fragment: str, limit: int = 12) -> None:
        for m in _IMG_TAG_RE.finditer(fragment):
            raw = m.group(1)
            if _BAD_IMAGE.search(raw):
                continue
            u = normalize_image_url(raw, base_url)
            if not u:
                continue
            if not _IMAGE_EXT.search(u.split("?")[0].split("#")[0]):
                continue
            _push(u)
            if len(out) >= limit:
                return

    # 1. 精确正文容器内 —— 首选，侧栏图进不来；并在正文结束标志处截断，
    #    避免把正文尾部的「相关推荐位」当成文章配图
    exact = _content_slice(page_html, _CONTENT_EXACT, stop=_BODY_END)
    if exact:
        _scan(exact)

    # 2. meta 标签（都在 <head>，只扫前 200KB 避免大页面拖慢正则）
    head = page_html[:200000]
    for pattern, _tag in _OG_PATTERNS:
        for m in re.finditer(pattern, head, flags=re.I):
            _push(normalize_image_url(m.group(1), base_url))

    # 3~4. 精确容器什么都没找到时，才退到宽泛容器 / 全页
    if not out:
        loose = _content_slice(page_html, _CONTENT_LOOSE)
        if loose:
            _scan(loose)
    if not out:
        _scan(page_html)

    return out


def extract_page_image(page_html: Optional[str], base_url: str = "") -> Optional[str]:
    """只要一张的便捷版本：取可信度最高的候选。"""
    candidates = extract_page_images(page_html, base_url)
    return candidates[0] if candidates else None


async def fetch_page_image_candidates(url: str) -> List[str]:
    """
    抓取原文网页，返回该页的候选配图列表。抓取失败返回空列表。

    注意这里**不做**「哪张才是这篇文章的图」的判断 ——
    那需要跨文章统计，只能在拿到所有文章的候选之后做（见 _fill_missing_images）。
    """
    page = await asyncio.to_thread(_http_get_text, url)
    if not page:
        return []
    return extract_page_images(page, url)


def _parse_pubdate(text: Optional[str]) -> Optional[datetime]:
    """
    解析发布时间。各站格式五花八门，实测有四类：

      1. RFC822 标准：  Mon, 05 Oct 2026 14:44:35 +0800     （雷锋网、界面、爱范儿）
      2. 自定义字符串： 2026-09-30 19:38:45 +0800           （36氪）
      3. 纯日期：       2025-06-05                          （人民网）
      4. **毫秒时间戳**：1791186962892                       （环球网，最特殊）

    第 4 种曾经坑过一次：环球网体育的 pubDate 是毫秒时间戳，
    parsedate 解析不了 → 返回 None → 被上层当成「没有时间」而放行，
    表面上看不出问题，但新鲜度过滤就完全失效了。
    所以这里单独处理纯数字的情况。

    统一转成「东八区的 naive datetime」，方便直接写进 MySQL 的 TIMESTAMP 列。
    """
    if not text:
        return None
    text = text.strip()

    # ---- 情况 4：纯数字时间戳 ----
    if text.isdigit():
        ts = int(text)
        if ts > 10_000_000_000:      # 13 位以上是毫秒，10 位是秒
            ts /= 1000
        try:
            return datetime.fromtimestamp(ts, CST).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None

    dt = None
    try:
        dt = parsedate_to_datetime(text)          # 情况 1
    except Exception:
        dt = None

    if dt is None:                                 # 情况 2、3
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if dt is None:
        return None
    # 没有时区信息就当作东八区；有的话统一换算到东八区
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST).replace(tzinfo=None)


def parse_feed(xml_bytes: bytes, source_name: str) -> List[Dict[str, Any]]:
    """把 RSS 的 XML 字节解析成统一的新闻字典列表。"""
    # 传 bytes：ElementTree 会读 XML 声明里的 encoding 自己解码
    root = ElementTree.fromstring(xml_bytes)

    # 兼容 RSS 2.0（<rss><channel><item>）和 Atom（<feed><entry>）两种格式
    if root.tag.endswith("feed"):
        raw_items = root.findall("{http://www.w3.org/2005/Atom}entry")
        is_atom = True
    else:
        channel = root.find("channel")
        # channel 下找不到 item，说明这不是一个正常的 RSS
        raw_items = channel.findall("item") if channel is not None else []
        is_atom = False

    items: List[Dict[str, Any]] = []
    for node in raw_items[:MAX_ITEMS_PER_SOURCE]:
        def _text(tag: str, ns: str = "") -> Optional[str]:
            """取子节点文本（含 CDATA）。找不到返回 None。"""
            child = node.find(f"{{{ns}}}{tag}" if ns else tag)
            return child.text if child is not None else None

        if is_atom:
            link_node = node.find("{http://www.w3.org/2005/Atom}link")
            link = link_node.get("href") if link_node is not None else ""
            title = _text("title", "http://www.w3.org/2005/Atom")
            desc = _text("summary", "http://www.w3.org/2005/Atom") or _text("content", "http://www.w3.org/2005/Atom")
            pub = _text("updated", "http://www.w3.org/2005/Atom") or _text("published", "http://www.w3.org/2005/Atom")
        else:
            link = _text("link") or ""
            title = _text("title")
            # description 是摘要；部分源（WordPress 系）正文在 content:encoded 里
            desc = _text("description") or ""
            pub = _text("pubDate")

        title = _clean_html(title or "")
        if not title or not link:
            continue          # 标题或链接都没有的条目直接丢弃

        # 正文文本：优先 content:encoded（更全），其次 <content>（环球网用这个），最后 description
        raw_body = (
            _text("encoded", "http://purl.org/rss/1.0/modules/content/")
            or _text("content")
            or desc
        )
        body_text = _clean_html(raw_body)

        # 作者：RSS 标准是 <author>，不少中文站用 dc:creator
        author = _text("creator", "http://purl.org/dc/elements/1.1/") or _text("author")
        author = _clean_html(author or "").strip() or None

        # 图片：正文里的 <img> → description 里的 <img> → 独立的 <cover> 字段（环球网）
        # 拿到后立刻 normalize_image_url：修掉「双协议头 https:https://」
        # 「协议相对 //img.xxx」这类脏数据（实测界面新闻、环球网都有）。
        raw_image = _extract_image(raw_body or "") or _extract_image(desc or "") or _text("cover")
        image = normalize_image_url(raw_image, link)

        items.append({
            "title": title.strip(),
            "url": link.strip(),
            "raw_text": body_text,
            "image": image,
            "author": author,
            "source": source_name,
            "publish_time": _parse_pubdate(pub),
        })
    return items


# ------------------------------------------------------------
# 三、筛选层：关键词匹配与分类判定
# ------------------------------------------------------------
def _build_keyword_pattern(keywords: List[str]) -> re.Pattern:
    """
    把一组关键词编译成一个正则（词与词之间是「或」）。

    对纯英文关键词加词边界保护：
      不加保护时 "AI" 会命中 "AIR"、"GPU" 会命中 "GPUs" 之外的词内片段。
      用 (?<![A-Za-z]) 和 (?![A-Za-z]) 断言前后不是英文字母即可精确匹配。
    中文关键词直接子串匹配（"人工智能"就是四个字连在一起）。
    """
    parts = []
    for kw in keywords:
        if kw.isascii():
            parts.append(rf"(?<![A-Za-z]){re.escape(kw)}(?![A-Za-z])")
        else:
            parts.append(re.escape(kw))
    return re.compile("|".join(parts), flags=re.I)


# 预编译成正则对象：只编译一次，之后每条新闻复用，比每次现编译快得多
STRONG_PATTERN = _build_keyword_pattern(AI_KEYWORDS_STRONG)
TITLE_PATTERN = _build_keyword_pattern(AI_KEYWORDS_TITLE_ONLY)
TECH_PATTERN = _build_keyword_pattern(TECH_KEYWORDS)

# 分类规则里 match 字段 → 对应正则。None 表示不过滤（照单全收）。
MATCH_PATTERNS = {
    "ai": STRONG_PATTERN,       # AI 强词（另有 TITLE_PATTERN 在 is_ai_related 里单独判定）
    "tech": TECH_PATTERN,
    "all": None,
}

# 综合源分发用的「分类 → 正则」映射。
# 顺序严格按 SHARED_CLASSIFY_ORDER，第一个命中的分类胜出。
CLASSIFY_PATTERNS: List[tuple] = [
    (name, _build_keyword_pattern({
        "娱乐": ENTERTAINMENT_KEYWORDS,
        "社会": SOCIETY_KEYWORDS,
        "国际": WORLD_KEYWORDS,
        "国内": DOMESTIC_KEYWORDS,
    }[name]))
    for name in SHARED_CLASSIFY_ORDER
]


def is_ai_related(item: Dict[str, Any]) -> bool:
    """
    判定一条新闻算不算「AI 新闻」，采用分级策略：

      1. 标题命中「强词」或「弱词」 → 算（标题提了就是主线）
      2. 正文命中「强词」          → 算（如正文讲的是大模型、英伟达、自动驾驶）
      3. 正文只命中「弱词」        → 不算（避免"正文顺带提了句算法/智能"的普通新闻混进来）

    这就是「泰坦军团显示器开售」被挡在门外的原因。
    """
    title = item.get("title") or ""
    body = item.get("raw_text") or ""
    if STRONG_PATTERN.search(title) or TITLE_PATTERN.search(title):
        return True
    return bool(STRONG_PATTERN.search(body))


def is_fresh(item: Dict[str, Any], hours: int) -> bool:
    """判断新闻是否在最近 hours 小时内发布。没有发布时间的当作新鲜（宁可多存不要漏）。"""
    pub = item.get("publish_time")
    if pub is None:
        return True
    return (datetime.now() - pub) <= timedelta(hours=hours)


def classify_shared_item(item: Dict[str, Any]) -> str:
    """
    给「综合源」的一条新闻判定唯一归属分类。

    【只匹配标题，不匹配正文】
      这是实测踩出来的：一开始用「标题 + 正文前 120 字」匹配，
      结果「华为与高通达成广泛专利许可协议」被归到了国际分类
      —— 因为它的正文里有一句「高通将收购华为部分美国专利」，
      命中了关键词「美国」，可这条新闻的主体根本不是国际。
      新闻的主体几乎都写在标题里，正文顺带提到的国家/领域不算数，
      所以这里严格只看标题。

    按 SHARED_CLASSIFY_ORDER 顺序找，第一个命中的胜出；
    一个都不命中就归到默认分类（头条）。
    """
    title = item.get("title") or ""
    for name, pattern in CLASSIFY_PATTERNS:
        if pattern.search(title):
            return name
    return SHARED_DEFAULT_CATEGORY


def pick_candidates(
    items: List[Dict[str, Any]],
    match: str,
    fresh_hours: int,
    limit: int,
    min_text_len: int = MIN_RAW_TEXT_LEN,
) -> List[Dict[str, Any]]:
    """
    按规则从一堆原始条目里挑出合格的候选。

    三道过滤依次是：
      1. 正文长度（太短的快讯没信息量）
      2. 新鲜度（只保留最近 fresh_hours 小时内发布的）
      3. match 规则（"ai" 走关键词，其余照单全收）

    返回按发布时间倒序、截断到 limit 条。
    """
    long_enough = [it for it in items if len(it.get("raw_text") or "") >= min_text_len]
    fresh = [it for it in long_enough if is_fresh(it, fresh_hours)]

    if match == "ai":
        picked = [it for it in fresh if is_ai_related(it)]
    elif match == "tech":
        # 科技分类：标题或正文开头命中科技名词即可。
        # 正文只取前 200 字 —— 短的科技快讯（如「XX 发布新机」）关键词往往在开头，
        # 而长文后段常会漂移到别的话题上。
        picked = [
            it for it in fresh
            if TECH_PATTERN.search(it.get("title") or "")
            or TECH_PATTERN.search((it.get("raw_text") or "")[:200])
        ]
    else:
        picked = fresh

    picked.sort(key=lambda x: x.get("publish_time") or datetime.min, reverse=True)
    return picked[:limit]


# ------------------------------------------------------------
# 四、对外入口：并发抓取所有源
# ------------------------------------------------------------
async def fetch_all_sources(sources: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    并发抓取所有源，返回 {源 id: [解析后的条目]}。

    为什么这里不直接做筛选？
      因为「筛选规则」是按分类定的，而同一个源可能被多个分类引用
      （比如 IT之家同时供「人工智能」和「科技」）。
      如果在这里筛，就得重复抓同一个源。所以这一层只负责「把数据拿回来」，
      筛选交给上层按分类规则做。
    """
    # asyncio.gather 让所有源同时抓，总耗时约等于最慢的那个源
    results = await asyncio.gather(*(fetch_feed(s) for s in sources), return_exceptions=True)

    data: Dict[str, List[Dict[str, Any]]] = {}
    for source, result in zip(sources, results):
        sid = source["id"]
        if isinstance(result, Exception):
            print(f"  [采集] {source['name']} 异常：{result}")
            data[sid] = []
            continue

        # RSS 只给标题或一句话的条目 → 去原文网页把正文补回来。
        # 只有 fetch_full_text=True 的源才做，因为多抓一次网页会明显变慢。
        if source.get("fetch_full_text"):
            need_full = [
                it for it in result
                if len(it.get("raw_text") or "") < MIN_RAW_TEXT_LEN
            ]
            if need_full:
                # 并发抓：10 条同时抓，总耗时约等于抓 1 条
                full_texts = await asyncio.gather(*(fetch_full_text(it["url"]) for it in need_full))
                for it, full in zip(need_full, full_texts):
                    if len(full) > len(it.get("raw_text") or ""):
                        it["raw_text"] = full
                print(f"  [采集] {source['name']}: 为 {len(need_full)} 条补抓了原文正文")

        data[sid] = result
        print(f"  [采集] {source['name']}: 解析出 {len(result)} 条")
    return data
