# ============================================================
# 缓存层（cache/）：新闻相关的缓存读写方法
# ------------------------------------------------------------
# 作用：把「新闻分类」和「新闻列表」缓存到 Redis，避免每次请求都查数据库。
# 本文件只负责「拼 key」和「调用底层的 Redis 读写」，真正读写 Redis 的在 config/cache_conf.py。
#
# 两个缓存对象：
#   新闻分类 —— 数据几乎不变，缓存久一点（默认 7200 秒 = 2 小时）
#   新闻列表 —— 会随新发布/浏览量变化，缓存短一点（默认 1800 秒 = 30 分钟）
#
# 命名约定：key 用 "模块:对象:参数" 的形式，方便在 Redis 里一眼看出是什么缓存。
#   例：news:category            → 全部分类
#       news_list:1:1:10         → 分类 1、第 1 页、每页 10 条的列表
#       news_list:all:1:10       → 全部分类、第 1 页、每页 10 条的列表
# ============================================================

from typing import Dict, List, Any, Optional   # 类型注解：Dict/List 是容器；Any 任意类型；Optional 表示可为 None

from config.cache_conf import get_json_cache, set_cache, delete_cache_pattern   # 底层 Redis 读写工具


CATEGORIES_KEY = "news:category"    # 新闻分类缓存的 key（全部分类共用这一个）
NEWS_LIST_PREFIX = "news_list:"     # 新闻列表缓存的 key 前缀（后面还要拼分类/页码/每页数量）
NEWS_DETAIL_PREFIX = "news:detail:"
RELATED_NEWS_PREFIX = "news:related:"

# ------------------------------------------------------------
# 读取「新闻分类」缓存
# 用法：`data = await get_cached_categories()`
# 返回：命中缓存 → 分类列表（list[dict]）；没有缓存 → None
# ------------------------------------------------------------
async def get_cached_categories():
    # get_json_cache 内部会从 Redis 取字符串并 json.loads 还原成 list/dict，取不到返回 None
    return await get_json_cache(CATEGORIES_KEY)


# ------------------------------------------------------------
# 写入「新闻分类」缓存
# 用法：`await set_cached_categories(data)` 或 `await set_cached_categories(data, 3600)`
# 入参：
#   data   —— 要缓存的分类数据（list[dict]）
#   expire —— 过期时间（秒），默认 7200 秒 = 2 小时
# 说明：分类数据很稳定，所以过期时间给得长；「数据越稳定，缓存越持久」
# ------------------------------------------------------------
async def set_cached_categories(data: list[Dict[str, Any]], expire: int = 7200):
    await set_cache(CATEGORIES_KEY, data, expire)


# ------------------------------------------------------------
# 写入「新闻列表」缓存
# 用法：`await set_cache_news_list(category_id, page, page_size, data, 1800)`
# 入参：
#   category_id —— 按哪个分类查；传 None 表示「不筛选，全部新闻」
#   page        —— 第几页（从 1 开始）
#   page_size   —— 每页几条
#   data        —— 要缓存的列表数据（list[dict]，每个 dict 是一条新闻）
#   expire      —— 过期时间（秒），默认 1800 = 30 分钟（列表变化快，比分类短）
# 返回：True 成功 / False 失败
# ------------------------------------------------------------
async def set_cache_news_list(category_id: Optional[int], page: int, page_size: int,
                              data: List[Dict[str, Any]], expire: int = 1800):
    # 把 category_id 规范化成字符串：
    #   有值（如 1） → "1"（表示"分类=1"的缓存）
    #   None        → "all"（表示"不限分类"的缓存，比 "None" 更直观）
    category_part = category_id if category_id is not None else "all"
    # 拼出唯一 key，一组参数对应一个 key：
    #   news_list:1:1:10    → 分类1、第1页、每页10条
    #   news_list:all:1:10  → 全部分类、第1页、每页10条
    key = f"{NEWS_LIST_PREFIX}{category_part}:{page}:{page_size}"
    # 调底层写入 Redis（带过期时间）
    return await set_cache(key, data, expire)


# ------------------------------------------------------------
# 读取「新闻列表」缓存
# 用法：`data = await get_cache_news_list(category_id, page, page_size)`
# 返回：命中缓存 → 列表数据（list[dict]）；没有缓存 → None
# 注意：读取时的 key 拼接方式必须和写入时「完全一致」，否则永远读不到
# ------------------------------------------------------------
async def get_cache_news_list(category_id: Optional[int], page: int, page_size: int):
    # 与写入时同样的规范化逻辑（保证读写用同一个 key）
    category_part = category_id if category_id is not None else "all"
    key = f"{NEWS_LIST_PREFIX}{category_part}:{page}:{page_size}"
    # 从 Redis 取并还原成 list/dict，取不到返回 None
    return await get_json_cache(key)


async def get_cached_news_detail(news_id: int) -> Optional[Dict[str, Any]]:
    """
    获取缓存的新闻详情

    Args:
        news_id: 新闻ID

    Returns:
        Optional[Dict[str, Any]]: 新闻数据，不存在则返回None
    """
    key = f"{NEWS_DETAIL_PREFIX}{news_id}"
    return await get_json_cache(key)

async def cache_news_detail(news_id: int, news_data: Dict[str, Any], expire: int = 300) -> bool:
    """
    缓存新闻详情

    Args:
        news_id: 新闻ID
        news_data: 新闻数据字典
        expire: 过期时间（秒），默认5分钟

    Returns:
        bool: 缓存成功返回True
    """
    key = f"{NEWS_DETAIL_PREFIX}{news_id}"
    return await set_cache(key, news_data, expire)


# ------------------------------------------------------------
# 读取「相关推荐」缓存
# 用法：`data = await get_cached_related_news(news_id, limit)`
# 返回：命中缓存 → 推荐列表（list[dict]）；没有缓存 → None
# 说明：推荐结果和「哪条新闻 + 取几条」有关，所以 key 带上 news_id 和 limit
# ------------------------------------------------------------
async def get_cached_related_news(news_id: int, limit: int = 5) -> Optional[List[Dict[str, Any]]]:
    key = f"{RELATED_NEWS_PREFIX}{news_id}:{limit}"
    return await get_json_cache(key)


# ------------------------------------------------------------
# 写入「相关推荐」缓存
# 用法：`await cache_related_news(news_id, data, limit)`
# 入参：
#   news_id —— 是哪条新闻的相关推荐
#   data    —— 推荐列表数据（list[dict]）
#   limit   —— 取了几条（参与拼 key，必须和读取时一致）
#   expire  —— 过期时间（秒），默认 600 = 10 分钟
# 返回：True 成功 / False 失败
# ------------------------------------------------------------
async def cache_related_news(news_id: int, data: List[Dict[str, Any]], limit: int = 5,
                             expire: int = 600) -> bool:
    key = f"{RELATED_NEWS_PREFIX}{news_id}:{limit}"
    return await set_cache(key, data, expire)


# ------------------------------------------------------------
# 清理「新闻列表」和「新闻分类」缓存
# 用法：`n = await clear_news_list_cache()` → 返回清掉的 key 数量
# ------------------------------------------------------------
# 什么时候需要？
#   采集任务刚往库里写入了新新闻，但缓存里还躺着最久 30 分钟前的旧列表，
#   用户刷新页面看不到新内容。所以「写完库 → 立刻清缓存」是必要的一步，
#   这也是旁路缓存（Cache-Aside）模式的标准动作：先更新数据库，再让缓存失效。
#
# 为什么分类缓存也要清？
#   分类缓存有效期 2 小时，属于「极少变动」的数据。但新增分类时（比如采集用的
#   「人工智能」），不清就要等最长 2 小时才生效。
#
# 为什么详情缓存不用清？
#   新入库的新闻是自增 id，之前根本不存在，不可能有旧的详情缓存。
async def clear_news_list_cache() -> int:
    deleted = await delete_cache_pattern(f"{NEWS_LIST_PREFIX}*")
    deleted += await delete_cache_pattern(CATEGORIES_KEY)
    return deleted