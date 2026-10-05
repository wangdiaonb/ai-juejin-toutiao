# ============================================================
# 原文摘录：把抓到的原文，摘成新闻客户端要的三段字段
# ------------------------------------------------------------
# 替代 services/llm_client.py 的「大模型改写」。
#
# 【为什么不改写，要摘录？】
#   AI 改写的问题不是句子不通顺，而是「不可验证」：
#   模型偶尔会顺手补一句原文没有的话（臆造参数、编造引述），
#   而这条新闻会以「某某媒体」的名义发布出去 —— 这是新闻产品最不能接受的。
#   摘录则天然免疫：写进库的每个字都是原文里有的。
#
#   附带好处：一轮采集省掉约 90 次大模型调用，整轮耗时从 ~110 秒降到 ~20 秒。
#
# 【三段字段怎么来】
#   title       原文标题（只做 HTML 清洗和长度截断，不改措辞）
#   description 正文开头，切在句号处，约 80 字 —— 列表页展示用
#   content     正文开头，切在句号处，约 300 字 —— 详情页展示用
#
#   description 是 content 的前缀，这是有意的：
#   列表页看到的简介，点进去在详情页开头能原样对上，不会「两段话对不上」。
# ============================================================

import re
from typing import Any, Dict, Optional

from config.fetcher_conf import (
    EXCERPT_CONTENT_LEN, EXCERPT_DESC_LEN, MIN_EXCERPT_BODY_LEN,
)

# 句末标点：中英文都算，遇到就认为「这句话说完了，可以在这里收尾」
_SENTENCE_END = re.compile(r"[。！？；!?;]")

# 正文开头常见的、不属于新闻内容的前缀。
# 这些都是实测从各源正文里「捞」出来的真实样本：
#   36氪    「文｜李炤锋 编辑｜张雨忻 」
#   钛媒体  「（本文作者为 半导体产业纵横，钛媒体经授权发布） 文 | 半导体产业纵横 」
#   通用    「原标题：xxx」「【编者按】」「来源：xxx」「记者：xxx」
#   新浪财经「炒股就看金麒麟分析师研报，权威，专业，及时，全面，助您挖掘潜力主题机会！」
#            「下载新浪财经APP，了解全球实时汇率」
# 注意全角竖线 ｜ 和半角 | 都要覆盖 —— 中英文站用的不一样。
#
# 【最后两条推广话术为什么要管？】
#   它们是新浪财经每篇正文前固定挂的引流段，一字不改地摘下来之后，
#   列表页第一条就是「炒股就看金麒麟分析师研报」—— 读者以为是正文，
#   其实是广告。这跟「原文摘录」的初衷（写的每个字都是原文的新闻内容）相悖。
_LEAD_NOISE = re.compile(
    r"^(?:"
    r"（原标题[:：][^）]*）|\(原标题[:：][^)]*\)|原标题[:：][^\n]{0,40}"
    r"|【?编者按[:：][^】]*】?"
    r"|（本文作者为[^）]*）|\(本文作者为[^)]*\)"
    r"|文\s*[｜|/]\s*[^\s，,。｜|]{1,15}(?:\s+编辑\s*[｜|/]\s*[^\s，,。｜|]{1,15})?"
    r"|编辑\s*[｜|/]\s*[^\s，,。｜|]{1,15}"
    r"|本文来自[^。]{0,30}。"
    r"|(?:来源|作者|记者|编辑)[:：][^\s，,。]{1,12}"
    # 引流/推广段
    # 关键在于结尾用 (?=\s|$) 收口：推广段后面一定跟着空白（段落拼接处）
    # 或者就是正文开头。如果改用「碰到句号才停」，会一路吃掉正文第一句。
    r"|(?:下载|打开|安装)[^。！？]{0,12}(?:APP|app|客户端)[^。！？]{0,16}[。！？]?(?=\s|$)"
    r"|(?:扫码|扫描二维码|长按识别)[^。！？]{0,20}[。！？]?(?=\s|$)"
    r"|(?:点此|点击此处|点击进入|点击查看)[^。！？]{0,20}[。！？]?(?=\s|$)"
    r"|(?:海量资讯|更多精彩)[^。！？]{0,20}[。！？]?(?=\s|$)"
    r"|炒股就看[^。！？]{0,60}[。！？]?"
    r")\s*"
)

# 抓到正文里混进来的 URL / 邮箱（如「jianli@jiemian.com」）—— 列表页展示很丑
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BARE_URL = re.compile(r"https?://\S+")


def _clean_text(text: str) -> str:
    """去掉正文里的邮箱、裸链接和多余空白。"""
    if not text:
        return ""
    text = _EMAIL.sub("", text)
    text = _BARE_URL.sub("", text)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    return text.strip()


def _strip_lead_noise(text: str) -> str:
    """
    反复剥掉正文开头的前缀，直到剥不动为止。

    为什么要循环而不是只 sub 一次？
      因为前缀会连着出现：「（本文作者为 半导体产业纵横，钛媒体经授权发布） 文 | 半导体产业纵横 9月底…」
      一次替换只能去掉括号里那段，前面还留着「文 | 半导体产业纵横」。
    """
    prev = None
    while prev != text:
        prev = text
        text = _LEAD_NOISE.sub("", text).strip()
    return text


def _cut_at_sentence(text: str, limit: int) -> str:
    """
    在 limit 个字符内，尽量切在句末标点之后。

    为什么要「尽量」而不是直接 text[:limit]？
      硬截断会出现「……公司宣布将投资 3.5 亿」这种半句话，
      列表页上非常像被截断的乱码。
      所以退回到最近的一个句号；只有当这个句号出现得太早
      （用掉了不到一半额度，说明前一句太短、信息量不够）时才硬截。
    """
    text = text.strip()
    if len(text) <= limit:
        return text

    window = text[:limit]
    last_end = 0
    for m in _SENTENCE_END.finditer(window):
        last_end = m.end()

    # 句号落在后半段 → 用它收尾，读起来是完整的句子
    if last_end >= limit * 0.5:
        return window[:last_end].strip()
    # 否则硬截，并补个省略号表示「还有下文」
    return window.rstrip() + "…"


def build_excerpt(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    把一条采集到的原始条目，转成入库需要的三段字段。

    参数 item 需要包含：title / raw_text（正文）。
    返回：{"title": str, "description": str, "content": str}
        或 None —— 表示「正文不可用，这条不值得入库」，由调用方跳过。

    什么情况返回 None？
      剥掉署名前缀、剥掉与标题重复的部分之后，正文不足 MIN_EXCERPT_BODY_LEN 字。
      实测中新网的视频类新闻就是这种：RSS 里的正文就是标题本身，
      入库后详情页会变成「标题重复两遍」，还不如不要这条。
    """
    title = _clean_text(item.get("title") or "")[:255]
    body = _clean_text(item.get("raw_text") or "")

    # 剥掉「原标题：」「文｜张三 编辑｜李四」这类署名前缀
    body = _strip_lead_noise(body)
    # 再剥掉「正文开头把标题又抄了一遍」的情况，重复两遍说明没有真实正文
    if title and body.startswith(title):
        body = body[len(title):].lstrip("。！？；:：—- ")
        body = _strip_lead_noise(body)

    if len(body) < MIN_EXCERPT_BODY_LEN:
        return None

    content = _cut_at_sentence(body, EXCERPT_CONTENT_LEN)
    description = _cut_at_sentence(body, EXCERPT_DESC_LEN)

    # description 理论上不会是 content 的「后半段」，这里只是防御：
    # 万一规则改动导致 description 比 content 还长，就把它压回 content 的长度
    if len(description) > len(content):
        description = content

    return {"title": title, "description": description, "content": content}
