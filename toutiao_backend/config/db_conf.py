# ============================================================
# 数据库配置：创建异步引擎、会话工厂、以及数据库会话依赖
# ------------------------------------------------------------
# 这个文件负责「和数据库建立连接」，是整个项目的底层基础设施。
# 用的是异步方案(async)，配合 FastAPI 的异步特性，处理高并发更高效。
# ============================================================

import os

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine

# ------------------------------------------------------------
# 先加载 .env —— 必须发生在下面读 os.getenv 之前
# ------------------------------------------------------------
# 为什么这里也要 load_dotenv()？
#   config/llm_conf.py 里已经调用过一次，但「谁先被导入」是不确定的：
#   下面那行 os.getenv 是**模块导入时**就执行的，如果那一刻 .env 还没加载，
#   就读不到 DATABASE_URL。（比如单独运行 scripts/scheduled_fetch.py 时，
#   它第一个 import 的就是 db_conf。）
#   而且 load_dotenv() 会从**本文件所在位置**往上找 .env，
#   跟当前工作目录无关，所以从任何目录启动服务都能找到项目根的 .env。
# 包一层 try：python-dotenv 是可选依赖，缺失时只能靠系统环境变量，
#   这时下面的检查会给出明确报错，而不是抛一个看不懂的 ImportError。
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ------------------------------------------------------------
# 数据库连接地址(URL)
# ------------------------------------------------------------
# 【为什么这里不写默认值？】
#   以前这里兜底了一个带真实密码的连接串，等于把数据库密码写进了源码 ——
#   代码一旦推送到公开仓库，密码就跟着公开了。
#   现在改成只从「环境变量 / 本地 .env」读取，读不到就直接报错。
#
#   本机开发：在项目根目录建一个 .env（已被 .gitignore 忽略），写入
#       DATABASE_URL=mysql+aiomysql://root:你的密码@localhost:3306/news_app?charset=utf8mb4
#   模板见同目录的 .env.example。
#
#   为什么要 raise 而不是给个空串？
#     连接串为空时 SQLAlchemy 会抛一个很难懂的底层错误；
#     这里 fail-fast 并给出「怎么修」的提示，排查成本低得多。
ASYNC_DATABASE_URL = os.getenv("DATABASE_URL")

if not ASYNC_DATABASE_URL:
    raise RuntimeError(
        "环境变量 DATABASE_URL 未设置。\n"
        "请在项目根目录创建 .env 文件并写入（可参考 .env.example）：\n"
        "    DATABASE_URL=mysql+aiomysql://root:你的密码@localhost:3306/news_app?charset=utf8mb4"
    )

# ------------------------------------------------------------
# 创建异步引擎(engine)
# ------------------------------------------------------------
# 引擎是 SQLAlchemy 的核心，负责实际管理数据库连接、把 SQL 发给数据库。
# create_async_engine 创建的是「异步引擎」，所有操作都要配合 await 使用。
# echo 控制「是否把每条执行的 SQL 打印到控制台」。
# 原来这里写死 True，实测代价很大：AI 新闻采集任务入库 10 条，光打印 SQL 就多花约 90 秒
# （Windows 控制台输出很慢），而且真正的业务日志会被 SQL 淹没。
# 所以改成从环境变量读，默认关闭；需要排查 SQL 时临时打开：
#     PowerShell:  $env:DB_ECHO="true"
#     CMD:         set DB_ECHO=true
async_engine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=os.getenv("DB_ECHO", "false").lower() == "true",
    pool_size=10,     # 连接池中保持的「常驻连接」数量(减少反复建立连接的开销)
    max_overflow=20   # 连接池满了之后，允许额外创建的临时连接数上限
)

# ------------------------------------------------------------
# 创建异步会话工厂(sessionmaker)
# ------------------------------------------------------------
# 会话(session)是真正执行增删改查的「工作单元」。
# 工厂(async_sessionmaker)负责批量生产会话，每次要用数据库就从这里领一个。
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,       # 绑定到上面创建的引擎
    class_=AsyncSession,     # 指定用异步会话类
    expire_on_commit=False   # 提交后不立即让对象过期，方便提交后继续读取属性
)

# ------------------------------------------------------------
# get_db：FastAPI 依赖项，用于「获取数据库会话」
# ------------------------------------------------------------
# 这是一个生成器函数，配合 FastAPI 的 Depends(get_db) 使用。
# 路由函数里写上 db: AsyncSession = Depends(get_db)，
# FastAPI 就会自动调用它，把 session 注入进来，请求结束后自动清理。
async def get_db():
    # async with 进入时创建会话，退出时自动关闭
    async with AsyncSessionLocal() as session:
        try:
            # yield session：把会话交给路由函数使用(暂停在这里等路由跑完)
            yield session
            # 路由正常结束 -> 提交事务(把修改真正写进数据库)
            await session.commit()
        except Exception:
            # 出现异常 -> 回滚(撤销这次请求里做的所有未提交修改)
            await session.rollback()
            raise   # 把异常继续往上抛，让 FastAPI 处理返回错误
        finally:
            # 无论如何都会执行：关闭会话，释放连接回连接池
            await session.close()
