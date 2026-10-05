# ============================================================
# 定时调度：每天定点自动跑一次「AI 新闻采集」
# ------------------------------------------------------------
# 用 APScheduler 的 AsyncIOScheduler，而不是 BackgroundScheduler：
#
#   BackgroundScheduler 会另开一个线程执行任务，而本项目用的是
#   SQLAlchemy 异步引擎 + aiomysql，它的连接是绑定在事件循环上的，
#   跨线程使用会报「attached to a different loop」之类的错误。
#
#   AsyncIOScheduler 直接跑在 FastAPI 现有的事件循环里，
#   采集任务和 HTTP 请求共享同一个循环，安全且没有额外线程开销。
#
# 启停挂在 main.py 的 lifespan 上（FastAPI 官方的应用启停钩子）。
# ============================================================

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config.fetcher_conf import FETCH_HOUR, FETCH_MINUTE

from services.news_fetcher import fetch_and_save

# timezone 显式指定东八区，否则会用操作系统时区，容易搞错「几点跑」
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

JOB_ID = "fetch_daily_news"


async def run_fetch_job() -> None:
    """
    定时器真正调用的任务函数。
    外面包一层 try/except 很关键：任务里抛出的异常如果没人接，
    会被调度器记成执行失败，攒多了任务可能被自动暂停。
    """
    try:
        stats = await fetch_and_save()
        print(f"[定时] 采集任务结束：入库 {stats.get('saved')} 条，跳过 {stats.get('skipped')} 条")
    except Exception as e:
        print(f"[定时] 采集任务异常：{type(e).__name__}: {e}")


def start_scheduler() -> None:
    """注册任务并启动调度器。在 FastAPI 启动时调用。"""
    scheduler.add_job(
        run_fetch_job,
        trigger=CronTrigger(hour=FETCH_HOUR, minute=FETCH_MINUTE),
        id=JOB_ID,
        replace_existing=True,   # 服务热重载时避免注册出重复任务
        max_instances=1,         # 上一轮还没跑完，就不重复触发（防止慢任务叠加）
        coalesce=True,           # 服务停过一段时间，错过的多次执行合并成一次
        misfire_grace_time=600,  # 允许延迟 10 分钟内补跑
    )
    scheduler.start()
    print(f"[定时] 调度器已启动：每天 {FETCH_HOUR:02d}:{FETCH_MINUTE:02d} 自动采集全部 9 个分类的新闻")


def stop_scheduler() -> None:
    """停止调度器。在 FastAPI 关闭时调用。"""
    if scheduler.running:
        # wait=False：不阻塞等待正在跑的任务结束（关闭服务时不要卡住）
        scheduler.shutdown(wait=False)
        print("[定时] 调度器已停止")
