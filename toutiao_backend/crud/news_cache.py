# ============================================================
# CRUD 层（带缓存版）：封装「操作数据库 + 读写 Redis 缓存」的方法
# ------------------------------------------------------------
# 和 crud/news.py 的区别：这一版在查库的基础上加了一层 Redis 缓存。
#
# 缓存策略（旁路缓存 Cache-Aside）：
#   1. 先读缓存 → 命中就直接返回（不查库）
#   2. 没命中 → 查数据库 → 把结果写回缓存 → 返回
#
# 读取时有个坑：缓存里存的是 JSON，datetime 会变成字符串，
# 还原成 ORM 对象时必须把字符串转回 datetime，否则上层 .isoformat() 会报错。
# ============================================================

from datetime import datetime                   # 缓存里的时间字符串要转回 datetime

from fastapi.encoders import jsonable_encoder   # 把 ORM 对象转成可 JSON 化的普通 dict
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from cache.news_cache import (
    get_cached_categories, set_cached_categories,
    get_cache_news_list, set_cache_news_list,
    get_cached_news_detail, cache_news_detail,
    get_cached_related_news, cache_related_news,
)
from models.news import Category, News
from schemas.news import RelatedNewsResponse, NewsDetailResponse


# ------------------------------------------------------------
# 工具函数：把缓存里的一条 dict 还原成 News 对象
# ------------------------------------------------------------
def _dict_to_news(item: dict) -> News:
    # 缓存里 publish_time 是 ISO 字符串（如 "2024-01-01T08:00:00"），
    # 必须转回 datetime，否则上层 _news_to_dict 里的 .isoformat() 会报 AttributeError
    item = dict(item)          # 复制一份，避免改动调用方传入的原始字典
    publish_time = item.get("publish_time")
    if publish_time:
        item["publish_time"] = datetime.fromisoformat(publish_time)
    return News(**item)


# ------------------------------------------------------------
# 查询新闻分类列表
# ------------------------------------------------------------
async def get_categories(db: AsyncSession, skip: int = 0, limit: int = 100):
    # 1) 先读缓存，命中直接返回
    cached_categories = await get_cached_categories()
    if cached_categories:
        return cached_categories

    # 2) 没命中 → 查库（select(Category) + 分页）
    stmt = select(Category).offset(skip).limit(limit)
    result = await db.execute(stmt)
    categories = result.scalars().all()

    # 3) 把结果写回缓存（jsonable_encoder 让 ORM 对象能转成 JSON）
    if categories:
        await set_cached_categories(jsonable_encoder(categories))
    return categories


# ------------------------------------------------------------
# 查询某个分类下的新闻列表(分页)
# ------------------------------------------------------------
async def get_news_list(db: AsyncSession, category_id: int, skip: int = 0, limit: int = 10):
    # 缓存 key 用「页码」拼，而这里拿到的是 offset，先换算：page = offset // limit + 1
    page = skip // limit + 1

    # 1) 读缓存，命中就把每条 dict 还原成 News 对象返回
    cached_list = await get_cache_news_list(category_id, page, limit)
    if cached_list:
        return [_dict_to_news(item) for item in cached_list]

    # 2) 没命中 → 查库
    stmt = select(News)
    # category_id 传 0(或不传)时表示「不筛选分类」，返回全部新闻
    if category_id:
        stmt = stmt.where(News.category_id == category_id)
    stmt = stmt.offset(skip).limit(limit)
    result = await db.execute(stmt)
    news_list = result.scalars().all()

    # 3) 用 jsonable_encoder 转（会带上 content 等所有字段），再写回缓存
    if news_list:
        await set_cache_news_list(category_id, page, limit, jsonable_encoder(news_list))
    return news_list


# ------------------------------------------------------------
# 统计某个分类下的新闻总数
# ------------------------------------------------------------
async def get_news_count(db: AsyncSession, category_id: int):
    # func.count(News.id) 生成 "COUNT(id)"，用来数一共有多少条
    stmt = select(func.count(News.id))
    # category_id 传 0 时统计全部新闻
    if category_id:
        stmt = stmt.where(News.category_id == category_id)
    result = await db.execute(stmt)
    return result.scalar_one()


# ------------------------------------------------------------
# 查询单条新闻详情
# ------------------------------------------------------------
async def get_news_detail(db: AsyncSession, news_id: int):
    # 1) 读缓存，命中就还原成 News 对象返回
    cached_news = await get_cached_news_detail(news_id)
    if cached_news:
        return _dict_to_news(cached_news)

    # 2) 没命中 → 查库
    stmt = select(News).where(News.id == news_id)
    result = await db.execute(stmt)
    news = result.scalar_one_or_none()

    # 3) 查到就写回缓存（排除 related_news：相关推荐单独一套缓存，不塞进详情里）
    if news:
        news_dict = NewsDetailResponse.model_validate(news).model_dump(
            mode="json", by_alias=False, exclude={"related_news"}
        )
        await cache_news_detail(news_id, news_dict)
    return news


# ------------------------------------------------------------
# 浏览量 +1
# ------------------------------------------------------------
async def increase_news_views(db: AsyncSession, news_id: int):
    stmt = update(News).where(News.id == news_id).values(views=News.views + 1)
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount > 0


# ------------------------------------------------------------
# 获取同类推荐
# ------------------------------------------------------------
async def get_related_news(db: AsyncSession, news_id: int, category_id: int, limit: int = 5):
    # 1) 读缓存，命中直接返回（推荐结果是 list[dict]，不用还原成对象）
    cached_related = await get_cached_related_news(news_id, limit)
    if cached_related:
        return cached_related

    # 2) 没命中 → 查库：同分类、排除自己，按浏览量/发布时间倒序
    stmt = select(News).where(
        News.category_id == category_id,
        News.id != news_id
    ).order_by(
        News.views.desc(),        # views 降序（热门优先）
        News.publish_time.desc()  # 浏览量相同时，新的优先
    ).limit(limit)
    result = await db.execute(stmt)
    related_news = result.scalars().all()

    # 3) 转成前端要的字典，写回缓存
    related_data = [
        RelatedNewsResponse.model_validate(item).model_dump(mode="json")
        for item in related_news
    ]
    if related_data:
        await cache_related_news(news_id, related_data, limit)
    return related_data
