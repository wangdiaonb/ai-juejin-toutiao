# ============================================================
# 计划任务采集入口：给 Windows「任务计划程序」调用的命令行脚本
# ------------------------------------------------------------
# 【为什么需要这个文件？】
#   项目里原本已经有一个定时器（services/scheduler.py，APScheduler），
#   但它「寄生」在 uvicorn 进程里，状态只存在于内存中：
#       · 电脑关机      → 进程没了 → 定时器一起消失
#       · 开机后没启动服务 → 定时器同样不存在
#   所以只要错过 08:00 这个点，当天就再也不会补跑。
#
#   Windows 任务计划程序是「系统级」的：任务定义存在系统里，
#   并且可以勾选「如果错过了计划时间，则尽快启动」——
#   8:00 关机、10:00 才开机，系统会在开机后立刻补跑一次。
#
#   本文件就是那个「被系统调起的入口」，它不做任何业务逻辑，
#   只负责四件事：
#       ① 把工作目录切到项目根（否则 import 和 .env 都找不到）
#       ② 写日志到 logs/scheduled_fetch.log（任务计划没有控制台）
#       ③ 判断「是不是已经采过了」，避免和后端自带的 08:00 定时重复跑
#       ④ 调 services.news_fetcher.fetch_and_save()，然后干净退出
#
# 【为什么不直接调用 POST /api/news/fetch？】
#   那个接口要求后端服务正在运行，而我们要解决的恰恰是
#   「开机后后端可能还没起来」的场景。直接跑 Python 不依赖任何进程。
#
# 【手动用法】
#   .venv\Scripts\python.exe scripts\scheduled_fetch.py            # 正常跑（间隔不足会自动跳过）
#   .venv\Scripts\python.exe scripts\scheduled_fetch.py --force    # 忽略间隔判断，强制采一轮
#   .venv\Scripts\python.exe scripts\scheduled_fetch.py --status   # 只看「上次采于何时」，不采集
#
# 【退出码约定】（任务计划程序会显示「上次运行结果」）
#   0 = 采集成功，或「刚采过 / 有别的采集在跑，主动跳过」
#   1 = 失败（数据库连不上 / 抓取过程抛异常）
#   2 = 环境有问题（找不到项目根目录）
# ============================================================

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# ------------------------------------------------------------
# 第 0 步：在 import 项目模块「之前」把路径摆正
# ------------------------------------------------------------
# 任务计划程序可能以任意工作目录启动进程（甚至是 C:\Windows\System32），
# 而项目里所有 import 都是相对项目根写的（from services.xxx import ...）；
# config/llm_conf.py 里的 load_dotenv() 也是从「当前工作目录」往上找 .env。
# 所以这两件事必须最先做：chdir 到项目根 + 把项目根塞进 sys.path。
ROOT = Path(__file__).resolve().parent.parent
if not (ROOT / "main.py").exists():
    try:
        print(f"[计划任务] 找不到项目根目录：{ROOT}", file=sys.stderr)
    except Exception:
        pass
    raise SystemExit(2)

os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ------------------------------------------------------------
# 第 1 步：接管 stdout/stderr，把日志落到文件
# ------------------------------------------------------------
# 任务计划程序不会给这个进程任何控制台，print 出来的东西默认进黑洞；
# 一旦某天采集失败，你将完全不知道发生了什么。
# 下面把 sys.stdout / sys.stderr 直接换成「日志文件对象」，
# 于是 news_fetcher 里那些 print 也一并进了日志 ——
# 不改一行业务代码，就拿到了完整运行记录。
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "scheduled_fetch.log"

# 日志超过 5MB 就滚动一次，避免长期运行后无限增大
try:
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
        backup = LOG_DIR / "scheduled_fetch.log.1"
        if backup.exists():
            backup.unlink()
        LOG_FILE.rename(backup)
except OSError:
    pass  # 滚动失败不是致命问题，继续追加写

# buffering=1 → 行缓冲：边跑边落盘，万一中途被杀也能看到最后一行
_log_fp = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
sys.stdout = _log_fp
sys.stderr = _log_fp

# ---- 到这里 stdout 已经指向日志文件，后续 import 出错也会被记录下来 ----
from sqlalchemy import func, select  # noqa: E402

from config.db_conf import AsyncSessionLocal, async_engine  # noqa: E402
from models.news import News  # noqa: E402
from services.news_fetcher import fetch_and_save  # noqa: E402

# ------------------------------------------------------------
# 参数
# ------------------------------------------------------------
# 距上次采集不足这个小时数，就认为「今天已经采过」，直接跳过。
# 为什么需要它？—— 后端自带的 APScheduler 也是 08:00 跑，
# 两个定时器只差几十分钟，不加判断就会一天采两轮、互相删对方的数据。
# 想临时调大/调小，不改代码，设环境变量即可：
#     set SCHEDULED_FETCH_MIN_INTERVAL_HOURS=2
MIN_INTERVAL_HOURS = float(os.getenv("SCHEDULED_FETCH_MIN_INTERVAL_HOURS", "6"))

# 互斥锁文件：防止「后端的定时任务」和「本脚本」在同一分钟内撞车。
# 同一进程内的 APScheduler 我们管不到，但文件锁能挡住另一个进程的
# 计划任务（比如你自己手动双击跑了一次，还没结束任务就触发了）。
LOCK_FILE = LOG_DIR / ".scheduled_fetch.lock"
LOCK_STALE_MINUTES = 30


def log(msg: str) -> None:
    """带时间戳的输出。因为 stdout 已被换成日志文件，print 就等于写日志。"""
    print(f"[计划任务] {datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


async def get_last_saved_at():
    """
    查 news 表里最近一次「入库时间」。
    created_at 由 ORM 在插入时自动填（见 models/news.py 的 Base），
    所以它代表的是「上一轮采集是什么时候写的库」，不是新闻的发布时间。
    """
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(func.max(News.created_at)))).scalar()


def lock_is_fresh() -> bool:
    """锁文件是否仍然有效（说明另有一轮采集正在跑）。"""
    if not LOCK_FILE.exists():
        return False
    age_minutes = (datetime.now().timestamp() - LOCK_FILE.stat().st_mtime) / 60
    if age_minutes < LOCK_STALE_MINUTES:
        return True
    log(f"发现过期锁文件（{age_minutes:.0f} 分钟前，可能是上次被强杀留下的），忽略并继续")
    return False


async def main(force: bool = False, status_only: bool = False) -> int:
    """
    外层包一层：保证无论走哪条分支（跳过 / 失败 / 成功），
    都在「事件循环还活着」的时候释放数据库连接池。
    如果留到循环关闭之后再释放，aiomysql 会在 GC 时抛
    「RuntimeError: Event loop is closed」——无害但每次都会刷屏。
    """
    try:
        return await _run(force=force, status_only=status_only)
    finally:
        try:
            await async_engine.dispose()
        except Exception:
            pass


async def _run(force: bool = False, status_only: bool = False) -> int:
    log("=" * 64)
    log(f"启动（工作目录 {ROOT}，间隔阈值 {MIN_INTERVAL_HOURS} 小时）")

    # ---------- 判断 1：是否有别的采集正在跑 ----------
    if not status_only and lock_is_fresh():
        log("检测到另一轮采集正在进行（锁文件未过期）→ 本次跳过")
        return 0

    # ---------- 判断 2：距上次入库多久了 ----------
    try:
        last = await get_last_saved_at()
    except Exception as e:
        log(f"读取数据库失败：{type(e).__name__}: {e}")
        log(traceback.format_exc())
        return 1

    if last is None:
        log("news 表还没有任何记录 → 需要采集")
    else:
        gap = datetime.now() - last
        log(f"最近一次入库：{last:%Y-%m-%d %H:%M:%S}（{gap.total_seconds() / 3600:.1f} 小时前）")
        if status_only:
            return 0
        if not force and gap < timedelta(hours=MIN_INTERVAL_HOURS):
            log(f"距上次采集不足 {MIN_INTERVAL_HOURS} 小时 → 判定为「已经采过」，本次跳过")
            return 0

    if status_only:
        return 0

    # ---------- 真正采集 ----------
    LOCK_FILE.write_text(f"{os.getpid()}\n{datetime.now().isoformat()}\n", encoding="utf-8")
    try:
        log("开始采集（整轮约 3~5 分钟，详细过程见下方日志）")
        stats = await fetch_and_save()
    except Exception as e:
        log(f"采集失败：{type(e).__name__}: {e}")
        log(traceback.format_exc())
        return 1
    finally:
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    log(
        f"采集成功：入库 {stats.get('saved', 0)} 条 / 清理旧数据 {stats.get('deleted', 0)} 条 / "
        f"因无原文配图丢弃 {stats.get('no_image', 0)} 条 / 耗时 {stats.get('elapsed', '?')} 秒"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="计划任务用的采集入口")
    parser.add_argument("--force", action="store_true", help="忽略间隔判断，强制采一轮")
    parser.add_argument("--status", action="store_true", help="只打印上次采集时间，不采集")
    args = parser.parse_args()

    code = 1
    try:
        code = asyncio.run(main(force=args.force, status_only=args.status))
    except Exception:
        log("主流程未捕获异常：")
        log(traceback.format_exc())
    finally:
        log(f"结束，退出码 {code}")
        try:
            _log_fp.flush()
            _log_fp.close()
        except Exception:
            pass
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__

    raise SystemExit(code)
