# ============================================================
# 采集流水线：把 9 个分类的新闻都采回来、摘录、入库、替换旧数据
# ------------------------------------------------------------
# 完整流程（每一步失败都不会让整轮任务崩掉）：
#
#   1. 并发抓取所有源     services/rss_source.py  —— 每个源只抓一次
#   2. 按规则分发给分类   本文件的 _build_category_plan()
#   3. 补齐原文配图       services/rss_source.py  —— 无图的去原文网页找
#   4. 生成原文摘录       services/excerpt.py     —— 不改写，只摘录
#   5. 清空该分类旧新闻    crud/news.py            —— 「每天替换」的关键（先清）
#   6. 写入合格条目        crud/news.py            —— 无原文图的条目直接丢弃（后插）
#   7. 清理缓存           cache/news_cache.py     —— 让前端立刻看到新新闻
#
# 【第 5、6 步为什么是「先清后插」】
#   用「先插 → 再删白名单外的」会导致旧记录永远赖着不走：
#   旧记录 url 和今天抓到的相同 → 被跳过 → 图片不更新又被记进白名单。
#   详见本文件步骤 4a 前的说明。
#
# 【分层设计的一个要点】
#   第 1 步先把所有源抓完，再做分发。
#   因为同一个源可能被多个分类引用（IT之家同时供「人工智能」和「科技」），
#   如果按分类去抓，同一个地址会被重复抓 3 遍，既慢又容易被对方限流。
#
# 【第 3 步为什么单独抽出来做】
#   补图要发网络请求（每个缺图条目一次）。如果放在「逐条入库」的循环里，
#   就变成「抓一张图 → 写一条 → 再抓一张图」，串行慢得离谱。
#   抽到前面用 asyncio.gather 并发做，90 次请求约 10 秒就完了。
# ============================================================

import asyncio
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List

from cache.news_cache import clear_news_list_cache
from config.db_conf import AsyncSessionLocal
from config.fetcher_conf import (
    CANDIDATE_POOL_FACTOR, CATEGORY_RULES, FALLBACK_IMAGE, FETCH_PAGE_IMAGE,
    FRESH_HOURS_LENIENT, MIN_RAW_TEXT_LEN, PER_CATEGORY_LIMIT,
    PP_CHANNEL_SOURCES, REPLACE_OLD_NEWS, REQUIRE_ORIGINAL_IMAGE,
    SHARED_CLASSIFY_ORDER, SHARED_DEFAULT_CATEGORY, SHARED_SOURCE_IDS,
    SINA_ROLL_SOURCES, SOURCES, USE_LLM_REWRITE,
)
from crud import news as news_crud
from services.excerpt import build_excerpt
from services.llm_client import rewrite_news
from services.pp_source import fetch_all_pp_sources
from services.rss_source import (
    classify_shared_item, fetch_all_sources, fetch_page_image_candidates,
    is_fresh, pick_candidates,
)
from services.sina_source import fetch_all_sina_rolls

# 补图时的并发上限。一次放 8 个请求出去：够快，又不至于被对方站点当爬虫限流。
IMAGE_FETCH_CONCURRENCY = 8


def _fallback_rewrite(item: Dict[str, Any]) -> Dict[str, str]:
    """
    降级方案（仅在 USE_LLM_REWRITE=True 且大模型不可用时用到）：
    把 RSS 给的原文直接截成三段。

    现在的默认路径已经不走这里了 —— 默认 USE_LLM_REWRITE=False，
    直接用 services/excerpt.py 的原文摘录，天然没有「模型不可用」的问题。
    保留它只是为了让开关能随时切回大模型模式。
    """
    raw = (item.get("raw_text") or "").strip()
    return {
        "title": (item.get("title") or "")[:255],
        "description": raw[:60],
        "content": raw[:300],
    }


# 标题归一化时要去掉的字符：标点、空格、括号等一切「非文字」符号
_NON_WORD = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]")


def _title_key(title: str) -> str:
    """
    把标题归一化成去重用的 key。

    为什么按前 18 个字而不是整句？
      同一条新闻被不同媒体转载时，标题常常只有细微差别：
        「交通运输部：10 月 4 日全社会跨区域人员流动量 30539 万人」
        「交通运输部：10月4日，全社会跨区域人员流动量30539万人次」
      标点、空格、全半角都不一样，但前 18 个汉字完全一致。
      所以先剔掉所有非文字符号，再取前缀做 key。

    18 字是个经验值：够区分不同新闻，又不至于因为尾部措辞差异而漏判。
    """
    return _NON_WORD.sub("", title or "")[:18]


def _build_category_plan(
    source_data: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, List[Dict[str, Any]]]:
    """
    把抓回来的原始数据，按规则分配到 9 个分类。

    返回 {分类名: [候选新闻]}。

    分两轮处理，顺序很重要：
      第一轮 —— 有专属源的分类（人工智能/科技/财经/体育）
                这些源内容垂直，按关键词或照单全收即可。
      第二轮 —— 综合源分发（界面新闻、中新网）
                一份数据按关键词分给 娱乐/社会/国际/国内/头条。

    用一个全局 used_urls 记录「已经被某个分类认领的链接」，
    后面的分类不再重复使用，避免同一条新闻出现在多个分类里。
    """
    plan: Dict[str, List[Dict[str, Any]]] = {}
    used_urls: set = set()
    seen_titles: set = set()

    def _dedup(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        跨源标题去重：同一件事被多个媒体报道时只保留一条。

        为什么 url 去重不够？
          因为不同媒体的链接当然不一样，同一个事件在库里的 url 也各不相同。
          实测同时出现了两条「交通运输部：10月4日全社会跨区域人员流动量」，
          一条来自 IT 之家、一条来自界面新闻 —— 链接不同，内容却是一回事。

        保留策略：先按发布时间倒序，让最新的那条胜出。
        """
        ordered = sorted(
            items, key=lambda x: x.get("publish_time") or datetime.min, reverse=True
        )
        out: List[Dict[str, Any]] = []
        for it in ordered:
            key = _title_key(it.get("title"))
            if not key or key in seen_titles:
                continue
            seen_titles.add(key)
            out.append(it)
        return out

    # ---------- 第一轮：有专属源的分类 ----------
    for rule in CATEGORY_RULES:
        pool: List[Dict[str, Any]] = []
        for sid in rule["sources"]:
            pool.extend(source_data.get(sid, []))

        # 剔除已被前面分类用掉的链接，再去掉跨源重复的标题
        pool = [it for it in pool if it.get("url") not in used_urls]
        pool = _dedup(pool)

        # 候选池按 1.5 倍取（15 条），留给「补图后丢弃无图条目」的余量。
        # 后面 _fill_missing_images + 入库循环会把最终数量筛回 10 条。
        #
        # fresh_hours 支持分类级覆盖（rule 里可选的一个键）：
        # 娱乐这类源天然更新慢的分类，按全局 48 小时卡会永远凑不满条数。
        chosen = pick_candidates(
            pool, rule["match"], rule.get("fresh_hours", FRESH_HOURS_LENIENT),
            int(PER_CATEGORY_LIMIT * CANDIDATE_POOL_FACTOR),
        )
        plan[rule["category"]] = chosen
        used_urls.update(it["url"] for it in chosen)

    # ---------- 第二轮：综合源分发 ----------
    # 一份数据要供多个分类的源（新浪滚动流 sina_all）在这里按关键词拆桶。
    # 只对 plan 里已有的分类做「追加」，不覆盖 —— 专属源的结果必须保住。
    if SHARED_SOURCE_IDS:
        shared: List[Dict[str, Any]] = []
        for sid in SHARED_SOURCE_IDS:
            shared.extend(source_data.get(sid, []))

        shared = [
            it for it in shared
            if it.get("url") not in used_urls
            and len(it.get("raw_text") or "") >= MIN_RAW_TEXT_LEN
            and is_fresh(it, FRESH_HOURS_LENIENT)
        ]
        shared = _dedup(shared)

        # 按归属分类装桶。classify_shared_item 保证每条只进一个桶。
        buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for it in shared:
            buckets[classify_shared_item(it)].append(it)

        # 顺序遍历（娱乐 → 社会 → 国际 → 国内 → 头条），保证结果里分类顺序稳定
        for name in SHARED_CLASSIFY_ORDER + [SHARED_DEFAULT_CATEGORY]:
            items = buckets.get(name, [])
            # 按发布时间倒序：当日最新的排前面。
            # 用的是 48 小时池，倒序取前 10 条天然等价于
            # 「优先取 24 小时内的，不够才用 24~48 小时的补」。
            items.sort(key=lambda x: x.get("publish_time") or datetime.min, reverse=True)
            chosen = items[:int(PER_CATEGORY_LIMIT * CANDIDATE_POOL_FACTOR)]

            # 【必须是「追加」而不是赋值】
            #   专属源在第一轮已经往 plan[name] 里写过候选了
            #   （比如「头条」来自中新网要闻、「国际」来自中新网国际）。
            #   如果这里直接 plan[name] = chosen，第一轮的结果会被整个覆盖掉，
            #   综合源就从「补充」变成了「取代」—— 那还不如不加这个源。
            #   追加在后面还有另一个好处：入库循环是按顺序取条目的，
            #   专属源的条目排前面会优先入选，综合源只在不够时顶上。
            existing = plan.get(name) or []
            merged = existing + [it for it in chosen if it["url"] not in used_urls]
            plan[name] = merged
            used_urls.update(it["url"] for it in chosen)

    return plan


async def _fill_missing_images(plan: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    """
    确保每条候选都有一张「专属的、来自原文」的图（就地写进 item["image"]）。

    为什么必须做这一步？
      用户要求「配图一定要来自原文」。实测 18 个源里有 6 个
      （中新网 7 个频道、量子位、少数派、开源中国）RSS 完全不带图，
      只能靠这一步从原文网页提 og:image 或正文首图 —— 实测成功率约 96%。

    【为什么要跨条目统计重复图 —— 这是本函数最关键的逻辑】
      踩过两次坑，都是「一张图铺满整个分类」：
        1. 中新网：每篇文章的 og:image 都指向同一张站点默认图，
           只看单篇以为找到了图，80 篇放一起才发现是同一张。
        2. 界面新闻：RSS 里**自带**的图是栏目通用图（URL 里还是 2022 年的日期），
           同一个分类下 6 篇文章共用一张，等于没配图。
      所以不能只处理「没图的」，必须对**所有**条目做统一裁决：
        a. 先按 RSS 自带图统计，凡是「重复出现」的图都不可信，
           这些条目也要去原文网页重新找图；
        b. 把每条的候选图合并成列表（RSS 图在前、网页候选在后），
           重新统计每张图被几篇文章引用；
        c. 每条挑「第一张只在本篇出现」的图。挑不到（候选全是通用图）
           就置空，交由后面的「无图不入库」规则淘汰。

    并发是另一个关键：100 条缺图条目串行抓按每条 0.5 秒算要 50 秒；
    用 asyncio.gather + 信号量并发 8 路，只要约 15 秒。
    """
    stats = {"need": 0, "filled": 0, "failed": 0, "dropped_shared": 0, "replaced": 0}
    if not FETCH_PAGE_IMAGE:
        return stats

    all_items: List[Dict[str, Any]] = [it for items in plan.values() for it in items]

    # ---------- a. 找出「必须去原文网页重新找图」的条目 ----------
    #   - 完全没图的
    #   - 有图但这张图被多篇文章共用的（RSS 里的栏目通用图）
    rss_usage: Dict[str, int] = {}
    for it in all_items:
        u = it.get("image")
        if u:
            rss_usage[u] = rss_usage.get(u, 0) + 1

    targets = [
        it for it in all_items
        if it.get("url") and (not it.get("image") or rss_usage.get(it["image"], 0) > 1)
    ]
    stats["need"] = len(targets)
    if not targets:
        return stats

    sem = asyncio.Semaphore(IMAGE_FETCH_CONCURRENCY)

    async def _one(it: Dict[str, Any]) -> List[str]:
        async with sem:
            try:
                return await fetch_page_image_candidates(it["url"])
            except Exception as e:
                # 单条失败不能影响其他条目；它后面会被「无图不入库」规则淘汰
                print(f"  [补图] 失败 {it['url'][:60]} -> {type(e).__name__}: {e}")
                return []

    cand_lists = await asyncio.gather(*(_one(it) for it in targets))

    # ---------- b. 合并候选并重新统计引用次数 ----------
    def _candidates_of(it: Dict[str, Any]) -> List[str]:
        """RSS 自带的图排在最前（它最可能和这篇文章相关），网页候选接在后面。"""
        out: List[str] = []
        own = it.get("image")
        if own:
            out.append(own)
        for u in it.get("_page_cands") or []:
            if u not in out:
                out.append(u)
        return out

    for it, cands in zip(targets, cand_lists):
        it["_page_cands"] = cands

    usage: Dict[str, int] = {}
    for it in all_items:
        for u in set(_candidates_of(it)):
            usage[u] = usage.get(u, 0) + 1

    # ---------- c. 每条挑第一张「只在本篇出现」的图 ----------
    for it in all_items:
        chosen = None
        for u in _candidates_of(it):
            if usage.get(u, 0) <= 1:      # 只在本篇出现 → 是这篇文章自己的图
                chosen = u
                break

        had = it.get("image")
        if chosen:
            it["image"] = chosen
            stats["filled"] += 1
            if had and had != chosen:
                stats["replaced"] += 1
        else:
            if _candidates_of(it):
                stats["dropped_shared"] += 1
                print(f"  [补图] 候选图全为站点通用图，弃用：{it['title'][:26]}")
            it["image"] = None

        # 清掉临时字段，别让它跟着 item 到处跑
        it.pop("_page_cands", None)

    stats["failed"] = stats["need"] - sum(1 for it in targets if it.get("image"))
    return stats


async def fetch_and_save() -> Dict[str, Any]:
    """
    执行一轮完整的采集入库（覆盖全部 9 个分类），返回统计信息。

    返回值示例：
        {"candidates": 78, "saved": 76, "skipped": 2, "deleted": 403,
         "llm_used": 74, "fallback_used": 2, "elapsed": 231.5,
         "per_category": {"人工智能": {"saved": 10, "deleted": 10}, ...}}
    """
    started = datetime.now()
    print(f"[采集] ====== 开始 {started:%Y-%m-%d %H:%M:%S} ======")

    stats: Dict[str, Any] = {
        "started_at": started.isoformat(timespec="seconds"),
        "candidates": 0,     # 全部候选条数
        "saved": 0,          # 实际入库条数
        "skipped": 0,        # 因为已存在而跳过的条数
        "failed": 0,         # 入库失败的条数
        "no_image": 0,       # 因为找不到原文配图而被丢弃的条数
        "no_text": 0,        # 因为没有可用正文（正文=标题 / 太短）而被丢弃的条数
        "deleted": 0,        # 清掉的旧新闻条数
        "img_need": 0,       # 需要去原文补图的条数
        "img_filled": 0,     # 成功补到图的条数
        "img_failed": 0,     # 补图失败的条数
        "excerpt_used": 0,   # 走原文摘录的条数
        "llm_used": 0,       # 走大模型改写的条数（仅 USE_LLM_REWRITE=True 时）
        "fallback_used": 0,  # 走降级截取的条数（仅 USE_LLM_REWRITE=True 时）
        "cleared_cache": 0,  # 清理掉的缓存 key 数量
        "per_category": {},  # 各分类明细
        "titles": [],
    }

    # ---------- 步骤 1：抓取所有源 ----------
    # 1a. RSS 源：并发抓，总耗时约等于最慢的那个源
    print(f"[采集] 开始抓取 {len(SOURCES)} 个 RSS 源")
    source_data = await fetch_all_sources(SOURCES)

    # 1b. 澎湃源：绕过 RSS 直接解析频道页 JSON，带图率 100%。
    #     必须放在 RSS 之后串行执行 —— 它内部有全局限速（防腾讯云 WAF），
    #     和 RSS 的并发抓取混在一起会互相干扰。
    #     整体包一层 try：澎湃挂了（WAF/改版）不能让整个采集任务失败。
    if PP_CHANNEL_SOURCES:
        try:
            pp_data = await fetch_all_pp_sources()
            source_data.update(pp_data)
        except Exception as e:
            print(f"[采集] 澎湃源整体失败，已跳过（其余源照常）：{type(e).__name__}: {str(e)[:100]}")

    # 1c. 新浪滚动源：JSON 接口 + 文章页补正文提图。
    #     同样串行放在后面 —— 它内部有 8 并发的详情页抓取，
    #     和 RSS 的并发叠在一起会让瞬时连接数过高。
    #     同样包 try：新浪改版不能让整轮任务失败。
    if SINA_ROLL_SOURCES:
        try:
            sina_data = await fetch_all_sina_rolls()
            source_data.update(sina_data)
        except Exception as e:
            print(f"[采集] 新浪源整体失败，已跳过（其余源照常）：{type(e).__name__}: {str(e)[:100]}")

    # ---------- 步骤 2：分发到各分类 ----------
    plan = _build_category_plan(source_data)
    stats["candidates"] = sum(len(v) for v in plan.values())
    print("[采集] 分类分配结果：")
    for name, items in plan.items():
        print(f"     {name:<6} {len(items):>2} 条候选")

    if stats["candidates"] == 0:
        stats["message"] = "本轮没有符合条件的候选新闻"
        print("[采集] ====== 结束：无候选 ======")
        return stats

    # ---------- 步骤 3：补齐原文配图 ----------
    # 必须在入库之前做完：这样「有图的条目够不够 10 条」在写库前就是确定的，
    # 不会出现「写了 5 条才发现剩下 5 条都没图」的尴尬。
    print("[采集] 步骤3：补齐原文配图（RSS 无图的去原文网页提取）")
    img_stats = await _fill_missing_images(plan)
    stats["img_need"] = img_stats["need"]
    stats["img_filled"] = img_stats["filled"]
    stats["img_failed"] = img_stats["failed"]
    stats["img_replaced"] = img_stats.get("replaced", 0)      # 被判定为通用图而换掉的
    stats["img_dropped"] = img_stats.get("dropped_shared", 0)  # 全是通用图、直接弃用的
    if img_stats["need"]:
        print(f"[采集] 补图完成：需要 {img_stats['need']} 条，"
              f"补到 {img_stats['filled']} 条，失败 {img_stats['failed']} 条，"
              f"换掉通用图 {img_stats.get('replaced', 0)} 条")

    # ---------- 步骤 4~6：逐个分类 筛拣 → 清旧 → 入库 ----------
    # 每个分类单独 commit。好处是某个分类出问题时，
    # 不会把已经写好的其他分类一起回滚掉。
    async with AsyncSessionLocal() as db:
        for cat_name, candidates in plan.items():
            category = await news_crud.get_category_by_name(db, cat_name)
            if category is None:
                print(f"[采集] 分类「{cat_name}」不存在，跳过（请在 news_category 表里创建）")
                continue

            # ============================================================
            # 分类内分三小步：【筛】→【清】→【插】
            # ------------------------------------------------------------
            # 为什么必须「先清后插」，而不是原来更直观的「先插 → 再删白名单外的」？
            #
            # 旧的「先插后删」逻辑里有一条「库里已有同 url 就 skip」的短路分支。
            # 它的问题在改造配图策略后彻底暴露：
            #   昨天入库的某条新闻用的是占位图，今天源里又抓到同一条新闻
            #   （url 相同）→ 走 skip 分支 → 既不入库也不更新图片
            #   → 它的 url 还被记进白名单 → 清旧数据时也删不掉它。
            #   结果就是：无论重跑多少轮，那批老占位图永远赖在库里。
            # 实测残留了 50 条 picsum.photos 随机图，就是这么来的。
            #
            # 改成「先清后插」后，每轮的语义变得非常干净：
            #   本次采到几条，库里就只剩这几条，历史数据一律不带过来。
            # 代价是丢了「保留上轮数据」的兜底，所以下面加了一道保护：
            # 只有筛出至少一条合格条目，才允许执行清空 ——
            # 否则今天源全挂了却把分类清成空白，用户打开什么都看不到。
            # ============================================================

            # ---------- 步骤 4a：先在候选上筛出合格条目（此阶段完全不碰数据库） ----------
            # 把「有原文图 + 有可用正文」两道门槛的判定前置到这里。
            # 好处：清空动作只可能在「已经确定手里有货」之后发生，
            # 不会出现「先清空、再发现一条都写不进去」的空窗期。
            picked: List[Dict[str, Any]] = []
            for item in candidates:
                # 上限用 len(picked)：picked 的语义就是「本轮要写库的条目」，
                # 数是几，库里最后就是几条。不会再出现白名单被撑爆的情况。
                if len(picked) >= PER_CATEGORY_LIMIT:
                    break

                # ---- 门槛 1：必须有「来自原文」的配图 ----
                # 为什么宁可少一条也不放占位图？
                #   占位图（picsum 随机图）和这条新闻毫无关系，读者会误以为
                #   那就是报道现场的照片 —— 本质是用假信息填充版面。
                #   所以严格模式下：抓不到原文图，这条直接不入库。
                image = item.get("image")
                if not image:
                    if REQUIRE_ORIGINAL_IMAGE:
                        stats["no_image"] += 1
                        print(f"  [无图] {cat_name} | 丢弃：{item['title'][:28]}")
                        continue
                    image = FALLBACK_IMAGE        # 只有关掉严格模式才会走到这里

                # ---- 门槛 2：必须有可用的正文 ----
                # 内容默认原文摘录；开了开关才走大模型改写。
                text_fields = None
                if USE_LLM_REWRITE:
                    # 这里也要包一层 try，而不是只依赖 rewrite_news 内部
                    # 的异常处理。教训：曾经因为提示词模板里少转义一个花括号，
                    # rewrite_news 抛出 KeyError，而调用处没有兜底 →
                    # 整个采集任务当场崩掉，一条都没入库。
                    try:
                        text_fields = await rewrite_news(
                            item["title"], item["raw_text"], item["source"]
                        )
                    except Exception as e:
                        print(f"  [LLM] 改写异常，本条降级：{type(e).__name__}: {str(e)[:80]}")
                        text_fields = None
                    if text_fields:
                        stats["llm_used"] += 1
                    else:
                        text_fields = _fallback_rewrite(item)
                        stats["fallback_used"] += 1
                else:
                    # 原文摘录：一字不改地摘，零编造风险。
                    # 返回 None 说明正文剥掉署名/标题后没剩多少内容（如中新网视频新闻），
                    # 这种条目入库后详情页只有标题重复两遍，不如丢弃。
                    text_fields = build_excerpt(item)
                    if not text_fields:
                        stats["no_text"] += 1
                        print(f"  [无正文] {cat_name} | 丢弃：{item['title'][:26]}")
                        continue
                    stats["excerpt_used"] += 1

                if not text_fields:
                    stats["no_text"] += 1
                    continue

                # 把已经判定好的「图 + 正文」挂回条目本身。
                # 下一个小步只管取用，不重复做判断 —— 避免两次判定结果不一致。
                item["_image"] = image
                item["_text"] = text_fields
                picked.append(item)

            # ---------- 步骤 4b：清空该分类的全部旧数据 ----------
            if not picked:
                # 一条合格的都没筛出来（今天源全挂 / 全都没图）→ 保留旧数据不动。
                # 宁可今天不更新，也不能把分类清空成白板。
                print(f"  [跳过] {cat_name}：本轮无合格条目，保留原有数据不清理")
                stats["per_category"][cat_name] = {
                    "saved": 0,
                    "deleted": 0,
                    "final": -1,       # -1 表示「本轮未改动」，区别于 0 条
                }
                continue

            cat_deleted = 0
            if REPLACE_OLD_NEWS:
                # 传空列表 = 清空该分类（含 favorites/history 的级联清理）。
                # 它只发 DELETE 不 commit，和下面的 INSERT 处在同一个事务里，
                # 所以「清空 + 写入」要么都成功，要么一起回滚，不会半途留下空分类。
                cat_deleted = await news_crud.delete_category_news_except(
                    db, category.id, []
                )
                stats["deleted"] += cat_deleted

            # ---------- 步骤 4c：逐条写入 ----------
            cat_saved = 0
            for item in picked:
                try:
                    # begin_nested() 开 SAVEPOINT：这一条失败只回滚这一条，
                    # 不会把同批已插好的数据一起毁掉。
                    async with db.begin_nested():
                        await news_crud.create_news(
                            db,
                            title=item["_text"]["title"],
                            description=item["_text"]["description"],
                            content=item["_text"]["content"],
                            image=item["_image"],
                            author=item.get("author") or item["source"],
                            category_id=category.id,
                            views=0,
                            publish_time=item.get("publish_time") or datetime.now(),
                            url=item["url"],
                            source=item["source"],
                        )
                    stats["saved"] += 1
                    cat_saved += 1
                    stats["titles"].append(f"[{cat_name}] {item['_text']['title']}")
                    print(f"  [入库] {cat_name} | {item['_text']['title'][:32]}")
                except Exception as e:
                    stats["failed"] += 1
                    print(f"  [失败] {cat_name} | {item['title'][:24]} -> {type(e).__name__}: {str(e)[:80]}")

            await db.commit()
            stats["per_category"][cat_name] = {
                "saved": cat_saved,
                "deleted": cat_deleted,
                "final": cat_saved,
            }
            print(f"  [替换] {cat_name}：清旧 {cat_deleted} 条，写入 {cat_saved} 条")

    # ---------- 步骤 7：清理缓存 ----------
    # 必须在 commit 之后做：如果先清缓存再提交，中间这一瞬间有请求进来，
    # 会把「还没有新新闻」的旧数据重新写进缓存，新数据又要等过期才可见。
    if stats["saved"] > 0:
        stats["cleared_cache"] = await clear_news_list_cache()

    stats["elapsed"] = round((datetime.now() - started).total_seconds(), 1)
    print(
        f"[采集] ====== 结束：入库 {stats['saved']} 条 / "
        f"入库失败 {stats['failed']} 条 / 因无原文配图丢弃 {stats['no_image']} 条 / "
        f"因无可用正文丢弃 {stats['no_text']} 条 / "
        f"清理旧数据 {stats['deleted']} 条，耗时 {stats['elapsed']}s ======"
    )
    return stats
