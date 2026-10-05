# ============================================================
# CRUD 层：封装所有「操作数据库」的方法
# ------------------------------------------------------------
# 这一层的职责很单一：只负责跟数据库打交道(增删改查)。
# 它不关心 HTTP 请求、也不关心返回格式，只是把查到的数据返回给上层(router)。
#
# 这样分层的意义：
#   路由层(router) 只管"接请求、调方法、拼响应"
#   CRUD 层 只管"拼 SQL、查数据"
#   以后想改查询逻辑，只需要改这一个文件，路由层完全不用动。
# ============================================================
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from models.favorite import Favorite
from models.history import History
from models.news import Category, News


# ------------------------------------------------------------
# 查询新闻分类列表
# ------------------------------------------------------------
async def get_categories(db: AsyncSession, skip: int = 0, limit: int = 100):
    # select(Category) 构建一条 "SELECT * FROM news_category" 的查询语句
    # .offset(skip)  跳过前 skip 条(分页用)
    # .limit(limit)  最多取 limit 条
    stmt = select(Category).offset(skip).limit(limit)

    # db.execute(stmt) 真正把 SQL 发给数据库执行(异步，所以要 await)
    result = await db.execute(stmt)

    # result.scalars().all() 把结果里每一行转成 Category 对象，装进列表返回
    return result.scalars().all()


# ------------------------------------------------------------
# 查询某个分类下的新闻列表(分页)
# ------------------------------------------------------------
async def get_news_list(db: AsyncSession, category_id: int, skip: int = 0, limit: int = 10):
    # 先构建基础查询，再按需加过滤条件
    stmt = select(News)
    # category_id 传 0(或不传)时表示「不筛选分类」，返回全部新闻
    if category_id:
        stmt = stmt.where(News.category_id == category_id)
    stmt = stmt.offset(skip).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()


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

    # scalar_one() 取出「单个标量值」(就是那个数字)，比如 42
    return result.scalar_one()


# ------------------------------------------------------------
# 查询单条新闻详情
# ------------------------------------------------------------
async def get_news_detail(db: AsyncSession, news_id: int):
    stmt = select(News).where(News.id == news_id)
    result = await db.execute(stmt)

    # scalar_one_or_none() 返回单个对象；如果查不到就返回 None(而不是抛异常)
    return result.scalar_one_or_none()

async def increase_news_views(db: AsyncSession,news_id: int):
    stmt = update(News).where(News.id == news_id).values(views=News.views + 1)
    result = await db.execute(stmt)
    await db.commit()

    return result.rowcount > 0


#获取同类推荐
async def get_related_news(db: AsyncSession,news_id: int,category_id: int,limit: int = 5):
    #order_by 排序 > 浏览量和发布时间
    stmt = select(News).where(
        News.category_id == category_id,
        News.id != news_id
    ).order_by(News.views.desc(),#默认升序，desc表示降序
                    News.publish_time.desc()
    ).limit(limit)
    result = await db.execute(stmt)
    #return result.scalars().all()
    related_news = result.scalars().all()
    #列表推导式  推导出新闻的核心数据，然后再return
    return [{
        "id": news_detail.id,
        "title": news_detail.title,
        "description": news_detail.description,
        "content": news_detail.content,
        "image": news_detail.image,
        "author": news_detail.author,
        # 和其它地方保持一致：publish_time 转成 ISO 字符串；为空返回 None
        "publishTime": news_detail.publish_time.isoformat() if news_detail.publish_time else None,
        "categoryId": news_detail.category_id,
        "views": news_detail.views
    } for news_detail in related_news]


# ============================================================
# 以下三个函数是「采集入库」专用的写操作
# ------------------------------------------------------------
# 项目原来的 crud/news.py 只有读（select + views+1），
# AI 自动采集功能需要把抓到的新闻写进库，所以在这里补齐写入能力。
# ============================================================

async def get_news_by_url(db: AsyncSession, url: str):
    """
    按原文链接查这条新闻是否已经入库。返回新闻 id，没查到返回 None。

    为什么要先查再插？
      news.url 上已经建了唯一索引，重复插入会直接抛 IntegrityError。
      先查一遍能把「重复采集」处理成「安静跳过」而不是「报错中断」，
      让采集任务变成幂等的：跑一次和跑十次，结果一样。
    """
    stmt = select(News.id).where(News.url == url)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def create_news(db: AsyncSession, **fields) -> News:
    """
    插入一条新闻，返回带自增 id 的 News 对象。

    为什么用 **fields 而不是一长串具名参数？
      采集侧以后加字段（比如摘要、标签）时，这里不用跟着改，扩展性更好。

    为什么这里不 commit？
      提交事务交给调用方。采集是一批一批插的，由调用方在整批结束时统一 commit，
      这样能做到「要么全部成功、要么全部回滚」，不会插到一半留下脏数据。
    """
    news = News(**fields)
    db.add(news)          # 把对象加入会话（此时还没发 SQL）
    await db.flush()      # flush 才真正执行 INSERT，并回填自增 id；但事务尚未提交
    return news


async def get_category_by_name(db: AsyncSession, name: str):
    """按分类名查分类对象（采集时把新闻归到对应分类下）。"""
    stmt = select(Category).where(Category.name == name)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ============================================================
# 「每日替换」专用的删除操作
# ------------------------------------------------------------
# 需求：每个分类每天更新 10 条，旧的全部删掉。
#
# 【策略变更记录】最初用的是「先写新的，再删不是本轮写入的」，
# 因为这样一旦采集失败，旧数据还在，分类不会被清空。
# 但配图策略改成「必须有原文图」之后这个做法失效了：
#   旧记录（占位图）的 url 和今天抓到的 url 相同 → 走「已存在则跳过」
#   → 图片不会被更新，url 又被记进保留白名单 → 怎么删都删不掉。
#   实测库里因此残留了 50 条 picsum 随机图。
# 现在改为「先清后插」：调用方先确认手里有合格条目，才来这里清空，
# 清完立刻在同一个事务里写入新数据。
# 调用方（services/news_fetcher.py）用「筛不出合格条目就整段跳过」
# 来承担原来「不清空」的兜底职责。
# ============================================================

async def delete_category_news_except(
    db: AsyncSession, category_id: int, keep_urls: list
) -> int:
    """
    删除某分类下的新闻，返回删除条数。

    两种用法：
      - keep_urls 非空：只删「不属于本轮采集结果」的新闻（保留白名单制）
      - keep_urls 为空：清空该分类全部新闻（当前采集流程实际走的就是这条）

    【关键在于那个 or_ 条件，这是最容易写错的地方】
      手工导入的老数据 url 是 NULL。
      如果只写 News.url.notin_(keep_urls)，SQL 会变成
          WHERE url NOT IN ('a', 'b', ...)
      而 SQL 的三值逻辑里，任何值和 NULL 比较结果都是 NULL（不是 True），
      所以 `NULL NOT IN (...)` 求值为 NULL → 被当作假 → 老数据一条都删不掉。
      必须显式补上 News.url.is_(None) 才能把老数据也清掉。

    顺带做了「级联清理」：删新闻前先把引用它们的收藏/历史记录一起删掉，
    否则用户点开历史记录会看到一条不存在的新闻（空白页）。

    【注意】本函数只发 DELETE，不 commit —— 事务交给调用方控制，
    这样调用方能把「清空」和「写入新数据」放进同一个事务。
    """
    # 1) 找出该分类下需要删掉的新闻 id
    stmt = select(News.id).where(News.category_id == category_id)
    if keep_urls:
        stmt = stmt.where(or_(News.url.is_(None), News.url.notin_(keep_urls)))
    else:
        # 一条都不保留 = 清空该分类（当前采集流程走的就是这条）
        pass
    result = await db.execute(stmt)
    ids = list(result.scalars().all())
    if not ids:
        return 0

    # 2) 先清掉引用这些新闻的收藏记录和历史记录，避免留下悬空引用
    await db.execute(delete(Favorite).where(Favorite.news_id.in_(ids)))
    await db.execute(delete(History).where(History.news_id.in_(ids)))

    # 3) 再删新闻本身
    await db.execute(delete(News).where(News.id.in_(ids)))
    return len(ids)


async def count_news_by_category(db: AsyncSession, category_id: int) -> int:
    """统计某分类下的新闻条数（验证「每天替换」是否生效时用）。"""
    stmt = select(func.count(News.id)).where(News.category_id == category_id)
    result = await db.execute(stmt)
    return result.scalar_one()