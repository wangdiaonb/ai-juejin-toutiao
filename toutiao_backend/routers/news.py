# ============================================================
# 路由层：定义 HTTP 接口，接收请求、调用 CRUD、返回 JSON 响应
# ------------------------------------------------------------
# 这一层是「对外的大门」，前端/客户端请求的就是这里的接口。
# 每个 @router.xxx 装饰器都对应一个可以访问的 URL 接口。
# ============================================================

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from crud import news
from sqlalchemy.ext.asyncio import AsyncSession
from config.db_conf import get_db
from crud import news_cache
# ------------------------------------------------------------
# 创建 API 路由实例
# ------------------------------------------------------------
# APIRouter 是 FastAPI 的「路由分组工具」：把同一类接口拆到单独的 py 文件，
# 而不是全部堆在 main.py 里，让项目结构更清晰。
#   prefix="/api/news"  路由前缀。下面所有接口的路径都会自动拼上 /api/news
#   tags=["news"]       接口文档分组标签。打开 FastAPI 自动文档 /docs 时，
#                       这些接口会归类到 "news" 分组下，方便查看。
router = APIRouter(prefix="/api/news", tags=["news"])


# ------------------------------------------------------------
# 工具函数：把 News 的 ORM 对象转成前端需要的字典
# ------------------------------------------------------------
# 为什么要转？
#   SQLAlchemy 的 ORM 对象不能直接放进 JSON(会序列化报错)，
#   而且前端习惯用 camelCase 命名(如 categoryId)，数据库是 snake_case(如 category_id)。
#   这里统一做一次「字段名转换 + 时间格式化」，前端拿到的就是干净的数据。
def _news_to_dict(n):
    """把 News ORM 对象转成前端需要的 camelCase 字典"""
    return {
        "id": n.id,
        "title": n.title,
        "description": n.description,
        "content": n.content,
        "image": n.image,
        "author": n.author,
        "categoryId": n.category_id,           # snake_case -> camelCase
        "views": n.views,
        # publish_time 是 datetime 对象，转成 ISO 字符串才能进 JSON；
        # 如果为空则返回 None
        "publishTime": n.publish_time.isoformat() if n.publish_time else None,
        # 下面两个字段是「AI 新闻采集」功能新增的：
        #   url    原文链接 —— 前端可以做「查看原文」跳转
        #   source 来源媒体 —— 前端可以显示「来源：量子位」
        # 注意：手工导入的那 403 条数据这两列是 NULL，前端要做好空值判断
        "url": n.url,
        "source": n.source,
    }


# ------------------------------------------------------------
# 接口 1：获取新闻分类列表
# 访问地址：GET /api/news/categories
# ------------------------------------------------------------
@router.get("/categories")
async def get_categories(
        skip: int = 0,        # 跳过前几条(分页偏移量)
        limit: int = 100,     # 最多返回几条
        db: AsyncSession = Depends(get_db),   # Depends 注入数据库会话(见 config/db_conf.py)
):
    # 调用 CRUD 层查数据
    categories = await news_cache.get_categories(db, skip, limit)
    # 拼装统一格式的响应
    return {
        "code": 200,
        "message": "获取新闻分类成功",
        "data": categories
    }


# ------------------------------------------------------------
# 接口 2：获取新闻列表(按分类，分页)
# 访问地址：GET /api/news/list?categoryId=1&page=1&pageSize=10
# ------------------------------------------------------------
@router.get("/list")
async def get_news_list(
        # alias="categoryId" 表示这个参数从 URL 的 categoryId 字段读取；
        # 默认 0 表示「不筛选分类」，返回全部新闻（推荐/全部 场景）
        category_id: int = Query(0, alias="categoryId"),
        page: int = 1,        # 第几页，默认第 1 页
        # alias="pageSize" 让前端用 pageSize 传参；le=100 限制每页最多 100 条
        page_size: int = Query(10, alias="pageSize", le=100),
        db: AsyncSession = Depends(get_db),
):
    # 根据页码算偏移量：第 2 页、每页 10 条 -> 跳过前 10 条
    offset = (page - 1) * page_size

    # 查当前页的新闻列表 + 该分类的总数
    news_list = await news_cache.get_news_list(db, category_id, offset, page_size)
    total = await news.get_news_count(db, category_id)

    # 判断是否还有下一页：已取到的数据条数 < 总数，就说明还有更多
    has_more = (offset + len(news_list)) < total

    return {
        "code": 200,
        "message": "获取新闻列表成功",
        "data": {
            # 列表里的每个 ORM 对象都转成字典再返回
            "list": [_news_to_dict(n) for n in news_list],
            "total": total,
            "hasmore": has_more
        }
    }


# ------------------------------------------------------------
# 接口 3：获取单条新闻详情
# 访问地址：GET /api/news/detail?id=1
# ------------------------------------------------------------
@router.get("/detail")
async def get_news_detail(
        # alias="id"：URL 里用 id 传参，函数内部变量名叫 news_id
        news_id: int = Query(..., alias="id"),
        db: AsyncSession = Depends(get_db),
):
    news_item = await news_cache.get_news_detail(db, news_id)

    # 查不到 -> 返回 404
    if not news_item:
        raise HTTPException(status_code=404, detail="新闻不存在")

    views_res = await news.increase_news_views(db, news_item.id)
    if not views_res:
        raise HTTPException(status_code=404, detail="新闻不存在")

    related_news = await news_cache.get_related_news(db, news_item.id,news_item.category_id)
    # 转成字典，并附加相关新闻占位字段
    data = _news_to_dict(news_item)
    data["relatedNews"] = related_news
    return {
        "code": 200,
        "message": "获取新闻详情成功",
        "data": data
    }


# ------------------------------------------------------------
# 接口 4：手动触发一次新闻采集（调试用）
# 访问地址：POST /api/news/fetch
# ------------------------------------------------------------
# 定时任务每天会自动跑，这个接口是为了方便你随时手动验证一遍。
#
# 【为什么改成「后台执行」而不是等它跑完再返回？】
#   采集要抓 12 个源的 RSS（正文短的还要抓原文网页）、
#   再对约 90 条新闻逐条调大模型改写、最后替换旧数据 —— 整轮要 5 分钟左右。
#   如果同步等待，HTTP 请求早就超时了（浏览器、Nginx 默认都在 1 分钟内）。
#   所以用 BackgroundTasks 立刻返回「已开始」，采集在后台继续跑，
#   进度看服务端控制台的日志。
#
# 【为什么要加 _fetch_running 这个锁？】
#   一轮采集要跑好几分钟，如果用户连点两次（或者刚好和定时任务撞上），
#   两个任务会同时抓取、同时写库、同时删旧数据 —— 既浪费大模型费用，
#   也可能互相删掉对方刚写进去的数据。宁可让第二次请求直接失败。
#
# 【安全提醒】生产环境必须给这个接口加管理员鉴权，
# 否则任何人都能反复触发采集，白白消耗带宽和大模型费用。
_fetch_running = False


@router.post("/fetch")
async def trigger_fetch(background_tasks: BackgroundTasks):
    global _fetch_running
    if _fetch_running:
        raise HTTPException(
            status_code=409,
            detail="已有一轮采集任务正在运行，请等它结束后再试",
        )

    # 在函数内部导入，避免 main → routers → services → crud 的循环引用
    from services.news_fetcher import fetch_and_save

    async def _run():
        global _fetch_running
        try:
            stats = await fetch_and_save()
            print(f"[手动] 采集完成：入库 {stats.get('saved', 0)} 条，"
                  f"因无原文配图丢弃 {stats.get('no_image', 0)} 条，"
                  f"清理旧数据 {stats.get('deleted', 0)} 条")
        except Exception as e:
            print(f"[手动] 采集异常：{type(e).__name__}: {e}")
        finally:
            _fetch_running = False

    _fetch_running = True
    background_tasks.add_task(_run)
    return {
        "code": 200,
        "message": "采集任务已在后台开始，整轮约需 5 分钟，进度请看服务端控制台日志",
        "data": None,
    }
